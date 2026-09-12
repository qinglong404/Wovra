"""轮/步归谁：账本对账探针（只读：不加载 Task、不写盘）。

口径（2026-09-12 用户拍板，worklog §56）——**轮数统一，不按 agent 重算**：

* **轮**：`R{n}` 是会话级唯一序列（用户输入 → 闭环），只增、不重编。一轮
  **恰好归一个**落点 = "它被附加到哪个 agent 的上下文"（轮上的 `active_view`）。
  实时落定、**只记新轮**；第一次分裂之前的那一段整段记「已压缩」，不拆给
  任何 agent。故 **Σ各 agent 名下轮 + 已压缩段 = 会话总轮数 = 最大 R 号**。
* **步**：一次模型调用 = 一步，归**执行它的那个 agent**（同轮可分属两家）。
* **显示**：一律按**总轮（R 号）**——号清单就是展开入口（`R{n}-E{m}`）。

本探针打三样：
1. 每个 agent 名下的号区间与步数；
2. 不变量①：轮账配平（Σ名下 + 已压缩 == 最大 R 号）；
3. 不变量②：Σ步 vs 轮上累计 `steps_used` 的差额（差额 = 空响应重试；
   旧机制留下的"检查点轮"幻影步数也已查清，见 §55.4）。

用法：`uv run --no-sync python scripts/probe_agent_ledger_divergence.py [task_id]`
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from wovra import registry as reg_mod  # noqa: E402
from wovra import views  # noqa: E402

TASKS = Path(__file__).resolve().parents[1] / "tasks"
SHOW = 3


def sessions(task_id: str | None) -> list[Path]:
    if task_id:
        return [TASKS / task_id / "task.json"]
    return sorted(TASKS.glob("*/task.json"), key=lambda p: p.stat().st_mtime, reverse=True)


def unique(ledger: dict) -> list[dict]:
    """账本按名字与 ID 双键，收敛成一份（跳过"已压缩段"哨兵）。"""
    out: list[dict] = []
    ids = set()
    for key, rec in ledger.items():
        if key == "__unassigned__" or id(rec) in ids:
            continue
        ids.add(id(rec))
        out.append(rec)
    return out


def audit(path: Path) -> dict | None:
    data = json.loads(path.read_text(encoding="utf-8"))
    rounds = [r for r in (data.get("rounds") or []) if isinstance(r, dict)]
    if not rounds:
        return None
    domains = reg_mod.latest_domains(rounds)
    ledger = views.agent_ledger(rounds, domains, data.get("registry") or [])
    account = views.round_account(rounds, domains, data.get("registry") or [])
    derived_steps = sum(rec["steps"] for rec in unique(ledger))
    return {
        "id": data.get("id") or path.parent.name,
        "ledger": unique(ledger),
        "account": account,
        "span": account["compressed_span"],
        "derived_steps": derived_steps,
        "used_steps": sum(int(r.get("steps_used") or 0) for r in rounds),
        "domains": len(domains),
    }


def main() -> None:
    rows = [r for p in sessions(sys.argv[1] if len(sys.argv) > 1 else None)
            if (r := audit(p)) is not None]
    if not rows:
        print("没有会话——无账可对。")
        return
    print(f"会话 {len(rows)} 个（口径：views.agent_ledger——轮按落点、步按执行者，一律总轮 R 号）")
    unbalanced = 0
    for row in rows[:SHOW]:
        acc, span = row["account"], row["span"]
        print(
            f"\n[{row['id']}] 总 {acc['total']} 轮 = 已压缩 {acc['compressed']} 轮"
            f"（R{span['first']}–R{span['last']}）+ 名下活轮 {acc['attributed']}"
            f" + 无落点 {acc['unassigned']}　域 {row['domains']} 个"
            f"　{'✓ 配平' if acc['balanced'] else '✗ 不配平'}"
        )
        if span.get("by_agent"):
            snap = "、".join(
                f"{k} {len(v)}" for k, v in sorted(span["by_agent"].items())
            )
            print(f"    已压缩段当时的归属快照（只作追溯）：{snap}")
        if not acc["balanced"]:
            unbalanced += 1
        for rec in sorted(row["ledger"], key=lambda x: -x["rounds"]):
            if not (rec["rounds"] or rec["steps"]):
                continue
            seqs = rec["seqs"]
            head = "、".join(f"R{s}" for s in seqs[:6]) + ("…" if len(seqs) > 6 else "")
            print(
                f"    {rec['name']:<28} {rec['rounds']:>3} 轮"
                f"（R{rec['first']}–R{rec['last']}）　步 {rec['steps']:<5}"
                f"　转出 {rec['handoffs']}　{head}"
            )
        delta = row["used_steps"] - row["derived_steps"]
        note = ("在飞/未结算（轮上累计还没落账）" if delta < 0
                else "空响应重试；旧检查点轮的幻影步数已单独查清，见 §55.4")
        print(f"    步数对账：派生（执行步）{row['derived_steps']} vs 轮上累计 "
              f"{row['used_steps']}　差 {delta}（{note}）")
    if len(rows) > SHOW:
        print(f"\n（仅展示最近 {SHOW} 个会话，共 {len(rows)} 个）")
    print(f"\n不变量①：Σ名下轮 + 已压缩 == 总轮数 —— "
          f"{'全部配平' if not unbalanced else f'{unbalanced} 个会话不配平'}")


if __name__ == "__main__":
    main()
