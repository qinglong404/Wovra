"""真跑一次**水位处的攒批结算**（会话副本，真 LLM）。

验四件事：① 一批轮**一次**调用结算（不是一个轮一次）；② 结算调用是不是**尾部追加**、
只付"指令 ＋ 锚 ＋ 产物"（读 `llm_call` 的 prompt/cached）；③ 产物按 seq 落到各轮的
`round["note"]`、`note_state=done`；④ 账本增量带来源戳（这批的末轮）。

用法（项目根执行）：

    .venv/bin/python scripts/probe_round_note_live.py <会话ID> [--batch N]

`--batch N` = 只结算最近 N 个还没有产物的轮（默认全收）。水位闸门被脚本显式打开
（水位 0、无宽限/冷却）——脚本要验的是"结算本身"，不是闸门。

约定：**只动副本**（`/tmp/wovra-round-note/tasks/`），原会话一个字节不碰。脚本不删（§0.3）。
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import os                                        # noqa: E402

from wovra.agent import MODE_MANAGED              # noqa: E402
from wovra.agent import note as note_module        # noqa: E402
from wovra.cli import _build_agent                # noqa: E402
from wovra import task as task_module             # noqa: E402

ROOT = Path("/tmp/wovra-round-note/tasks")


def main() -> int:
    ap = argparse.ArgumentParser(description="真跑一次水位处的攒批结算")
    ap.add_argument("session_id")
    ap.add_argument("--batch", type=int, default=0,
                    help="单批最多结算几轮（设的是 `_note_batch_max`；0 = 用默认 12）")
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

    os.environ["WOVRA_V4"] = "1"
    os.environ["WOVRA_ROUND_NOTE"] = "1"
    agent = _build_agent(task, mode=task.mode or MODE_MANAGED, async_organization=False)
    agent.rounds = task.rounds
    agent._org_watermark = 0        # 闸门打开：本脚本验的是结算本身
    agent._org_grace = 0
    agent._org_cooldown = 0

    pending = agent._note_pending_rounds()
    if not pending:
        raise SystemExit("没有可结算的轮（都已有产物）")
    if args.batch:
        agent._note_batch_max = args.batch
    seqs = [int(r["seq"]) for r in pending]
    print(f"副本：{dst}")
    print(f"待结算 {len(pending)} 轮（{note_module.seq_span(seqs)}），单批上限 "
          f"{agent._note_batch_max}，逐轮事件数："
          + "、".join(f"R{r['seq']}={len(r.get('events') or [])}" for r in pending))
    before = len(task.history or [])

    # 走闸门那条真路：`_maybe_organize_batch` → `_settle_round_notes`
    agent.last_context_estimate = agent._org_watermark
    agent._maybe_organize_batch()

    calls = [str(h.get("detail")) for h in (task.history or [])[before:]
             if h.get("kind") == "llm_call" and "[note]" in str(h.get("detail"))]
    print(f"\n结算调用 {len(calls)} 次（每批一次）：")
    for line in calls:
        print(f"  {line}")
    for r in pending:
        note = r.get("note") or {}
        print(f"\n── R{r['seq']}　note_state={r.get('note_state')!r}")
        if not note:
            continue
        print(f"   执行者（代码盖章）：{note.get('executor')}")
        print(f"   一句话：{note.get('sentence')}")
        for item in note.get("failures") or []:
            ev = item.get("evidence") or ""
            print(f"     ⚠ {item.get('text')}" + (f"　〔{ev}〕" if ev else ""))
    print("\n本次留痕与用量：")
    for h in (task.history or [])[before:]:
        kind = str(h.get("kind"))
        detail = str(h.get("detail"))
        if kind in ("note", "llm_call"):
            print(f"  [{kind}] {detail[:170]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
