"""真跑一次「每轮一段话」的结算（会话副本，真 LLM）。

验三件事：① 轮闭合时装配快照可用（尾部协议完整）；② 结算调用是不是**尾部追加**、
只付"指令 ＋ 产物"（读 `llm_call` 的 prompt/cached）；③ 产物落 `round["note"]`
与账本增量、`note_state=done`。

用法（项目根执行）：

    .venv/bin/python scripts/probe_round_note_live.py <会话ID> [--round N]

约定：**只动副本**（`/tmp/wovra-round-note/tasks/`），原会话一个字节不碰。脚本不删（§0.3）。
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from wovra.agent import MODE_MANAGED              # noqa: E402
from wovra.cli import _build_agent                # noqa: E402
from wovra import task as task_module             # noqa: E402

ROOT = Path("/tmp/wovra-round-note/tasks")


def main() -> int:
    ap = argparse.ArgumentParser(description="真跑一次每轮一段话的结算")
    ap.add_argument("session_id")
    ap.add_argument("--round", type=int, default=0, help="结算哪一轮（默认最后一个已闭合轮）")
    args = ap.parse_args()

    src = task_module.TASKS_ROOT / args.session_id
    if not (src / "task.json").is_file():
        raise SystemExit(f"会话不存在：{src}")
    ROOT.mkdir(parents=True, exist_ok=True)
    dst = ROOT / args.session_id
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)
    task_module.TASKS_ROOT = ROOT
    task = task_module.Task.load(args.session_id)

    rounds = [r for r in task.rounds if str(r.get("end_state")) == "completed"]
    if args.round:
        target = next((r for r in rounds if int(r["seq"]) == args.round), None)
    else:
        target = rounds[-1] if rounds else None
    if target is None:
        raise SystemExit("没有可结算的已闭合轮")
    if str(target.get("note_state") or "") == "done":
        target.pop("note", None)
    target["note_state"] = ""

    agent = _build_agent(task, mode=task.mode or MODE_MANAGED, async_organization=False)
    agent.rounds = task.rounds
    agent.current_round = target
    before = len(task.history or [])
    print(f"副本：{dst}")
    print(f"结算轮：R{target['seq']}（事件 {len(target.get('events') or [])} 条）")
    print("跑一次结算…", flush=True)
    agent.close_round()

    print(f"\nnote_state = {target.get('note_state')!r}")
    note = target.get("note") or {}
    if note:
        print(f"执行者（代码盖章）：{note.get('executor')}")
        print(f"一句话：{note.get('sentence')}")
        for item in note.get("failures") or []:
            ev = item.get("evidence") or ""
            print(f"  ⚠ {item.get('text')}" + (f"　〔{ev}〕" if ev else ""))
        print(f"账本增量：{note.get('ledger_append')}")
    print("\n本次留痕与用量：")
    for h in (task.history or [])[before:]:
        kind = str(h.get("kind"))
        detail = str(h.get("detail"))
        if kind in ("note", "llm_call"):
            print(f"  [{kind}] {detail[:170]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
