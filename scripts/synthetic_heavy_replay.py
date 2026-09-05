"""合成重度轨迹回放：把真实会话的事件模板扩展成数百轮长会话，
三种上下文策略逐轮回放装配并累计账目，观察多次逼近/突破窗口时的
成本曲线（模型不参与，零噪声）。

策略：
  managed-current  现行实现：每轮闭合即逐轮整理
  managed-v3       设计目标：水位（100K 未整理）触发的批量整理（反事实）
  baseline         全量回放 + 80% 水位压缩（对照组）

口径与简化：
  * 边界上下文：managed 走真实 _assemble_messages + 快速估算
    （ASCII 合成内容，len/4 ≈ tiktoken）；baseline = 摘要 + 未压缩
    事件之和。
  * 轮内增长：线性叠加本轮事件质量，窗口保底 90% 封顶（触及时
    计入 emergency fold 次数）。
  * 每轮计费 ≈ 步数 × (起 + 止) / 2；步数由本轮质量推定（2-31 步）。
  * 整理成本：截断索引/摘要的估算体量 + 固定开销（全价，不计折扣）。
"""

import json
import random
import sys
from types import SimpleNamespace

from wovra.agent import Agent

LIMIT = 1_000_000
GUARD = int(LIMIT * 0.9)          # managed/baseline 的窗口保底线
BASELINE_COMPACT = int(LIMIT * 0.8)  # baseline 压缩触发线
V3_WATERMARK = 100_000            # managed-v3 的未整理水位阈值
MILESTONES = (50_000_000, 100_000_000, 200_000_000, 300_000_000)


def fast_est(text: str) -> int:
    """合成内容全 ASCII，len/4 ≈ tiktoken 估算（快几个量级）。"""
    return len(text) // 4


def synth_rounds(n: int, rng: random.Random) -> list[dict]:
    """生成合成轨迹：常规轮 + 周期性大输出轮 + 偶发超大输出轮。"""
    rounds = []
    for k in range(1, n + 1):
        events = [{"id": f"R{k}-E01", "type": "user", "status": "",
                   "truncated": f"任务{k}",
                   "message": {"role": "user", "content": f"实现功能 {k}，并验证。"}}]
        pairs = 3 + (k % 4)
        for i in range(pairs):
            size = 6_000
            if i == 0 and k % 4 == 0:
                size = 250_000          # 周期性大工具输出（读大文件）
            if i == 1 and k % 9 == 0:
                size = 700_000          # 偶发超大输出（压测窗口保底）
            call_msg = {"role": "assistant", "content": "",
                        "tool_calls": [{"id": f"c{i}", "type": "function",
                                        "function": {"name": "read_file",
                                                     "arguments": json.dumps({"path": f"gen/{k}_{i}.log"})}}]}
            result_msg = {"role": "tool", "tool_call_id": f"c{i}",
                          "content": f"log {k}-{i} " + rng.choice("abcdefghij") * size}
            events.append({"id": f"R{k}-E{len(events)+1:02d}", "type": "tool_call",
                           "status": "", "truncated": f"read_file gen/{k}_{i}.log",
                           "message": call_msg})
            events.append({"id": f"R{k}-E{len(events)+1:02d}", "type": "tool_result",
                           "status": "ok", "truncated": "成功（截断行）",
                           "message": result_msg})
        events.append({"id": f"R{k}-E{len(events)+1:02d}", "type": "final_answer",
                       "status": "", "truncated": "完成",
                       "message": {"role": "assistant",
                                   "content": f"功能 {k} 完成。" + "细节" * 400}})
        rounds.append({"seq": k, "events": events,
                       "user_input": {"original": f"任务{k}", "normalized": ""}})
    return rounds


def round_mass(events: list[dict]) -> int:
    return sum(fast_est(str(e["message"].get("content") or "")) for e in events)


def simulate_managed(rounds, v3_watermark: bool):
    """managed：边界走真实装配；逐轮 org 或 V3 水位批量 org。"""
    agent = Agent(llm=SimpleNamespace(), tools=[], task=None, context_mode="managed")
    cum = 0
    milestones = {}
    pending_mass = 0
    pending_events = 0
    org_total = 0
    folds = 0
    rows = []
    prev_boundary = 0
    for k in range(1, len(rounds) + 1):
        agent.rounds = rounds[:k]
        agent.current_round = None
        agent.messages = []
        boundary = sum(len(str(m.get("content") or "")) // 4
                       for m in agent._assemble_messages())
        mass = round_mass(rounds[k - 1]["events"])
        steps = max(2, min(31, 2 + mass // 60_000))
        end = min(prev_boundary + mass, GUARD)
        if prev_boundary + mass > GUARD:
            folds += 1
        billing = steps * (prev_boundary + end) / 2
        # 整理成本
        n_events = len(rounds[k - 1]["events"])
        if v3_watermark:
            pending_mass += mass
            pending_events += n_events
            if pending_mass >= V3_WATERMARK:
                org = min(3_000 + pending_events * 150, 30_000)
                org_total += org
                cum += org
                pending_mass = 0
                pending_events = 0
        else:
            org = min(3_000 + n_events * 150, 20_000)
            org_total += org
            cum += org
        cum += billing
        for m in MILESTONES:
            if cum >= m and m not in milestones:
                milestones[m] = k
        rows.append((k, boundary, mass, cum))
        prev_boundary = boundary
    return {"cum": cum, "milestones": milestones, "rows": rows,
            "org_total": org_total, "folds": folds}


def simulate_baseline(rounds):
    """baseline：全量回放，水位 ≥ 80% 窗口时压缩较早轮次（保留最近 2 轮）。"""
    cum = 0
    milestones = {}
    compactions = 0
    kept = []            # 每个未压缩轮的事件 token 总量
    summary_tok = 0      # 压缩摘要的占位体量
    rows = []
    for k in range(1, len(rounds) + 1):
        mass = round_mass(rounds[k - 1]["events"])
        steps = max(2, min(31, 2 + mass // 60_000))
        base = summary_tok + sum(kept)
        start = base
        end = min(start + mass, GUARD)
        billing = steps * (start + end) / 2
        cum += billing
        kept.append(mass)
        # 轮闭合：水位 ≥ 80% → 压缩较早轮次（保留最近 2 轮）
        watermark = summary_tok + sum(kept)
        if watermark >= BASELINE_COMPACT and len(kept) > 2:
            compactions += 1
            cum += sum(kept[:-2]) * 0.3 + 2_000  # 压缩调用的账目
            summary_tok += 2_000          # 摘要占位
            kept = kept[-2:]              # 保留最近 2 轮原文
        boundary = summary_tok + sum(kept)
        for m in MILESTONES:
            if cum >= m and m not in milestones:
                milestones[m] = k
        rows.append((k, boundary, mass, cum))
    return {"cum": cum, "milestones": milestones, "rows": rows,
            "compactions": compactions}


def main() -> None:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 120
    rng = random.Random(20260906)
    rounds = synth_rounds(n, rng)

    cur = simulate_managed(rounds, v3_watermark=False)
    v3 = simulate_managed(rounds, v3_watermark=True)
    base = simulate_baseline(rounds)

    print(f"合成轨迹：{n} 轮（周期性大输出 250K / 偶发 700K 字符）\n")
    print(f"{'轮':>4} {'轮质量tok':>12} {'managed边界':>12} {'baseline边界':>12}")
    for idx in range(0, n, 10):
        m_row = cur["rows"][idx]
        b_row = base["rows"][idx]
        print(f"R{idx+1:<3} {m_row[2]:>12,} {m_row[1]:>12,} {b_row[1]:>12,}")
    print()

    print("=== 累计计费里程碑（达到该累计量所需的轮数）===")
    print(f"{'里程碑':>12} {'managed现行':>12} {'managed-v3':>12} {'baseline':>10}")
    for m in MILESTONES:
        row = [p["milestones"].get(m, "—") for p in (cur, v3, base)]
        print(f"{m:>12,} {str(row[0]):>12} {str(row[1]):>12} {str(row[2]):>10}")

    print()
    print("=== 终态 ===")
    print(f"managed 现行 : 累计 {cur['cum']:,.0f} tok | 整理 {cur['org_total']:,} | "
          f"窗口保底触发 {cur['folds']} 次")
    print(f"managed v3   : 累计 {v3['cum']:,.0f} tok | 整理 {v3['org_total']:,} | "
          f"窗口保底触发 {v3['folds']} 次")
    print(f"baseline     : 累计 {base['cum']:,.0f} tok | 压缩 {base['compactions']} 次")
    print()
    print(f"累计计费 baseline / managed现行 = {base['cum'] / cur['cum']:.2f}x"
          f" | / managed-v3 = {base['cum'] / v3['cum']:.2f}x")


if __name__ == "__main__":
    main()
