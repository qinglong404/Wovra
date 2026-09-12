"""per-agent 账本一致性探针（只读：不加载 Task、不写盘）。

背景（2026-09-12 用户提问）：轮/步是不是该由每个 agent 各自维护，
分裂/重组之后要不要重算、压缩为什么不用？

回答落在代码上：账本**派生、不落盘**（`views.agent_ledger`）——
承载轮与装配同一套块归属判据，答复轮/步数走事件流按 `route_to` 转交点分段，
`ctx_cur`/`ctx_peak`/`window` 仍存（观测，不是投影）。

本探针做两件事：
1. 并排打出「每个 agent 承载了多少 vs 答复了多少 vs 执行了几步」；
2. 自校验两条不变量：
   * Σ答复轮 == 真的产出过 `final_answer` 的轮数（**必须精确相等**）；
   * 拆散轮比例（一轮的块分属多个 agent）—— 这是"轮被拆散"的实测规模，
     也是"per-agent 数字禁止求和"的理由。

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


def audit(path: Path) -> dict | None:
    data = json.loads(path.read_text(encoding="utf-8"))
    rounds = [r for r in (data.get("rounds") or []) if isinstance(r, dict)]
    domains = reg_mod.latest_domains(rounds)
    if not domains or not rounds:
        return None
    index = views.block_index(rounds)
    owners = views.ownership(domains, index)
    ledger = views.agent_ledger(rounds, domains, data.get("registry") or [])

    # 自校验一：答复轮总数必须等于真的产出过 final_answer 的轮数
    answered = sum(1 for r in rounds if views._has_final_answer(r))
    got = sum(rec["answer_rounds"] for rec in _unique(ledger))
    # 自校验二：拆散轮（一轮的块分属多个 agent）
    per_round: dict[int, set[str]] = {}
    for bid, owner in owners.items():
        item = index.get(bid)
        if item is None:
            continue
        per_round.setdefault(int(item.get("seq") or 0), set()).add(owner)
    split_rounds = sorted(s for s, o in per_round.items() if len(o) > 1)

    # 自校验三：派生步数 vs 轮上累计的 steps_used（前者只会略少：空响应重试
    # 不产生事件）
    derived_steps = sum(rec["steps"] for rec in _unique(ledger))
    used_steps = sum(int(r.get("steps_used") or 0) for r in rounds)
    return {
        "id": data.get("id") or path.parent.name,
        "rounds": len(rounds),
        "answered": answered,
        "got_answers": got,
        "split_rounds": split_rounds,
        "ledger": _unique(ledger),
        "derived_steps": derived_steps,
        "used_steps": used_steps,
        "scheme": int(data.get("agent_id_scheme") or 1),
    }


def _unique(ledger: dict) -> list[dict]:
    """账本按名字与 ID 双键，收敛成一份（同一条目只取一次）。"""
    seen: list[dict] = []
    ids = set()
    for rec in ledger.values():
        if id(rec) in ids:
            continue
        ids.add(id(rec))
        seen.append(rec)
    return seen


def main() -> None:
    rows = [r for p in sessions(sys.argv[1] if len(sys.argv) > 1 else None)
            if (r := audit(p)) is not None]
    if not rows:
        print("没有带分裂产物的会话——无偏差可测。")
        return
    print(f"有分裂产物的会话 {len(rows)} 个（账本口径：views.agent_ledger，派生不落盘）")
    bad = 0
    for row in rows[:SHOW]:
        ok = "✓" if row["answered"] == row["got_answers"] else "✗"
        print(
            f"\n[{row['id']}] 会话 {row['rounds']} 轮（有答复 {row['answered']}）　"
            f"ID 体系 v{row['scheme']}　Σ答复轮 {row['got_answers']} {ok}"
        )
        print(f"  拆散轮（块分属多个 agent）：{len(row['split_rounds'])} 个 "
              f"{row['split_rounds'][:8]}{'…' if len(row['split_rounds']) > 8 else ''}"
              f"　派生步数 {row['derived_steps']} vs 轮上累计 {row['used_steps']}")
        if row["answered"] != row["got_answers"]:
            bad += 1
        for rec in sorted(row["ledger"], key=lambda x: -x["carrier_blocks"]):
            print(
                f"    {rec['name']:<28} 答复 {rec['answer_rounds']:<3} | "
                f"承载 {rec['carrier_rounds']:<3}（{rec['carrier_blocks']:<4} 块） | "
                f"步 {rec['steps']:<5} | 转出 {rec['handoffs']}"
            )
    if len(rows) > SHOW:
        print(f"\n（仅展示最近 {SHOW} 个会话，共 {len(rows)} 个）")
    print(f"\n不变量：Σ答复轮 = 有答复的轮数 —— {'全部通过' if not bad else f'{bad} 个会话不通过'}")
    print("注：承载轮之和 ≠ 会话轮数（一个轮的块可分给两个 agent），禁止求和。")


if __name__ == "__main__":
    main()
