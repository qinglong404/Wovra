"""真实整理测试：181052 会话前 28 轮用标签提示词跑一次 org。

用法（项目根执行）：
    .venv/bin/python scripts/org_test_28.py --dry   # 只生成地图+指令，不调用 LLM
    .venv/bin/python scripts/org_test_28.py         # 真实调用一次整理

产物写入副本任务 20260909-181052-orgtest（不污染原会话），
分析结果打印到 stdout。
"""

import argparse
import os
import shutil

from wovra import blocks, lifecycle, task as task_module
from wovra.agent import MODE_MANAGED
from wovra.cli import _build_agent

SRC_ID = "20260909-181052-643355"
DST_ID = "20260909-181052-orgtest"


def _status_card(agent, rounds) -> str:
    """当前状态卡：续跑 agent 的顶部快照（Runtime 数据 + 整理产物组装）。"""
    ledger = lifecycle.FileLedger()
    for r in rounds:
        ledger.update(r, blocks=blocks.segment_round_by_file(r))
    live = sorted(
        p for p, e in ledger.entries().items()
        if e["state"] == "live" and p.endswith(".md")
    )
    lines = ["[当前状态卡]"]
    lines.append(f"- 当前轮：R{rounds[-1]['seq']}（本次整理 {len(rounds)} 轮）")
    lines.append(f"- 模型：{os.environ.get('Wovra_MODEL', '?')}")
    constraints = []
    for r in rounds:
        po = r.get("pending_org") or {}
        if po.get("key_constraints"):
            constraints.append(f"  - [R{r['seq']}] {po['key_constraints']}")
    if constraints:
        lines.append("- 关键约束（含来源轮次）：")
        lines += constraints
    sp = (rounds[0].get("pending_org") or {}).get("state_patch") or {}
    todos = []
    for key in ("current_status", "open_questions"):
        val = sp.get(key)
        if val:
            todos.append(f"  - {key}: {val if isinstance(val, str) else '；'.join(val)}")
    if todos:
        lines.append("- 状态/待办：")
        lines += todos
    if live:
        lines.append("- 当前存活的 markdown 产物：")
        lines += [f"  - {p}" for p in live]
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="只生成分块地图，不调 LLM")
    ap.add_argument("--to", type=int, default=28, help="最大轮号（默认 28）")
    ap.add_argument("--from", dest="from_seq", type=int, default=1,
                    help="起始轮号（默认 1）")
    args = ap.parse_args()

    root = task_module.TASKS_ROOT
    src, dst = root / SRC_ID, root / DST_ID
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)

    task = task_module.Task.load(DST_ID)
    task.id = DST_ID  # 副本强制用副本 id：Task.save 按 self.id 定位目录，
                      # 不改会写回 SRC（2026-09-10 实测污染原始任务）
    agent = _build_agent(task, mode=MODE_MANAGED, async_organization=False)
    rounds = [r for r in agent.rounds
              if args.from_seq <= r["seq"] <= args.to]
    print(f"轮数: {len(rounds)}（R{rounds[0]['seq']}~R{rounds[-1]['seq']}）")

    round_blocks, map_lines, _merged = agent._block_map_lines(rounds)
    n_blocks = sum(len(v) for v in round_blocks.values())
    print(f"块数: {n_blocks}")
    if args.dry:
        print("\n".join(map_lines))
        return

    # 捕获 org 调用的原始输出（截断的 JSON 也要能分析）
    captured: dict[str, list[str]] = {}
    orig_call = agent._stream_call

    def wrap(messages, **kw):
        content, ordered, usage = orig_call(messages, **kw)
        captured.setdefault(kw.get("purpose", "working"), []).append(content or "")
        return content, ordered, usage

    agent._stream_call = wrap

    ok = agent._organize_rounds(rounds)
    print("整理成功:", ok)
    print(_status_card(agent, rounds))
    for seq, content in enumerate(captured.get("organization", []), 1):
        with open(f"/tmp/org_content_{seq}.txt", "w", encoding="utf-8") as fh:
            fh.write(content)
        print(f"\n===== org 调用 {seq} 原始输出（{len(content)} 字符）=====")
        print(content[:1500])
        if len(content) > 1500:
            print(f"…（后 {len(content) - 1500} 字符已存 /tmp/org_content_{seq}.txt）")
    for r in rounds:
        po = r.get("pending_org") or {}
        summaries = po.get("block_summaries") or {}
        print(f"\n===== R{r['seq']} =====")
        ui = r.get("user_input") or {}
        if ui.get("original"):
            print("👤", ui["original"])
        if po.get("normalized"):
            print("🎯", po["normalized"])
        if po.get("key_constraints"):
            print("📌", po["key_constraints"])
        for bid, s in summaries.items():
            print(f"  {bid}: {s}")

    print("\n===== LLM 调用记录 =====")
    for h in task.history:
        if h["kind"] == "llm_call":
            print(h["detail"])


if __name__ == "__main__":
    main()
