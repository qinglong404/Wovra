"""V4 成品验证：在会话副本上**真跑一轮**，打印这条新链路做了什么。

验的是四件事：① 水位处**攒批结算**（一批轮一次调用，未到水位不结算）；② 换档
（水位到了把超龄轮换成段落）；③ 分裂按活性文件（只有需要时才跑）；④ 装配里确实是
"段落档 ＋ 近期原文"。

用法（项目根执行）：

    .venv/bin/python scripts/probe_v4_live.py <会话ID> [--watermark 5000] [--keep 2] [--input "回一句：验证。"]

约定：**只动副本**（`/tmp/wovra-v4-probe/tasks/`），原会话一个字节不碰。脚本不删（§0.3）。
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from wovra.agent import MODE_MANAGED              # noqa: E402
from wovra.cli import _build_agent                # noqa: E402
from wovra import task as task_module             # noqa: E402

ROOT = Path("/tmp/wovra-v4-probe/tasks")


def _fresh(args) -> int:
    """空会话真跑 N 轮：到水位攒批结算 → 换档 → 装配里出现段落档。"""
    ROOT.mkdir(parents=True, exist_ok=True)
    task_module.TASKS_ROOT = ROOT
    task = task_module.Task.create(goal="验证 V4：攒批结算与换档")
    agent = _build_agent(task, mode=MODE_MANAGED, async_organization=False)
    agent._org_watermark = args.watermark
    agent._org_grace = 0
    agent._org_cooldown = 0
    agent._fold_keep = args.keep
    agent._fold_target = args.fold_target
    for i in range(1, args.fresh + 1):
        text = agent.run(f"第 {i} 个问题：只回一句「第 {i} 答」，不要做别的。")
        round_ = task.rounds[-1]
        print(f"R{i}: note_state={round_.get('note_state')!r} folded={bool(round_.get('folded'))}"
              f"　一句话：{str((round_.get('note') or {}).get('sentence'))[:80]}")
        print(f"     答：{str(text)[:60]}")
    notes = [r["seq"] for r in task.rounds if r.get("note_state") == "done"]
    calls = [h for h in task.history if h.get("kind") == "llm_call"
             and "[note]" in str(h.get("detail"))]
    print(f"\n结算调用 {len(calls)} 次，覆盖轮：{notes}（水位 {args.watermark:,}）")
    print("\n--- 装配里各轮的形态 ---")
    for m in agent._assemble_messages()[-6:]:
        body = str(m.get("content") or "").replace("\n", " ")
        print(f"  {m.get('role'):<9} {body[:150]}")
    print(f"\nfolded 的轮：{[r['seq'] for r in task.rounds if r.get('folded')]}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="V4 成品验证（会话副本上真跑一轮）")
    ap.add_argument("session_id")
    ap.add_argument("--watermark", type=int, default=5000, help="调小水位，好让换档真的发生")
    ap.add_argument("--keep", type=int, default=2, help="换档后保留原文的最近轮数")
    ap.add_argument("--fold-target", type=float, default=0.6,
                    help="折到水位的这个比例之下（一次折够，越大折得越少）")
    ap.add_argument("--input", default="只回一句：验证。", help="本轮用户输入")
    ap.add_argument("--fresh", type=int, default=0,
                    help="新建一个空会话真跑 N 轮（验证'每轮一段话 → 换档 → 段落进装配'整条链）")
    args = ap.parse_args()

    os.environ["WOVRA_V4"] = "1"          # 取消重组 ＋ 整理停用 ＋ 分裂按活性文件
    if args.fresh:
        return _fresh(args)
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

    agent = _build_agent(task, mode=task.mode or MODE_MANAGED, async_organization=False)
    agent.rounds = task.rounds
    agent._org_watermark = args.watermark
    agent._org_grace = 0
    agent._org_cooldown = 0
    agent._fold_keep = args.keep
    before = len(task.history or [])

    print(f"副本：{dst}")
    print(f"V4=1　水位={args.watermark}　原文窗口={args.keep}　输入：{args.input!r}")
    print("跑一轮…", flush=True)
    answer = agent.run(args.input)
    print(f"\n回答：{str(answer)[:200]}")

    round_ = task.rounds[-1]
    print(f"\n本轮：R{round_['seq']}　note_state={round_.get('note_state')!r}"
          f"　folded={bool(round_.get('folded'))}")
    note = round_.get("note") or {}
    if note:
        print(f"  一句话〔{note.get('executor')}〕：{note.get('sentence')}")
    print("\n本次留痕：")
    for h in (task.history or [])[before:]:
        kind = str(h.get("kind"))
        if kind in ("note", "fold", "split", "llm_call", "usage"):
            print(f"  [{kind}] {str(h.get('detail'))[:180]}")

    print("\n装配尾部（看段落档与信封）：")
    for m in agent._assemble_messages()[-4:]:
        body = str(m.get("content") or "").replace("\n", " ")
        print(f"  {m.get('role'):<9} {body[:160]}")
    print(f"\nfolded 的轮：{[r['seq'] for r in task.rounds if r.get('folded')]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
