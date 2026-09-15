"""分裂练兵场：在**会话副本**上跑一次真实维护（整理 + 分裂），看产物落没落地。

为什么要有它（2026-09-15 用户连着两次报"分裂又失败了"）：分裂失败的方式很安静
——产物被拒收只会落一条 history，页面上只剩一个小徽标。要回答"这次到底成没成、
树长什么样、文件都归到谁头上"，此前只能人工翻 task.json。这台仪器把链路钉成一条
命令：**副本上真跑**（真 LLM）→ 打印产物、注册表、覆盖率与全部拒收/归位留痕。

用法（项目根执行）：

    .venv/bin/python scripts/split_rehearsal.py <会话ID> --dry   # 只出硬数据+指令
    .venv/bin/python scripts/split_rehearsal.py <会话ID>         # 真跑一次维护

约定（与 AGENTS.md 一致）：**只动副本**——任务目录拷到
`/tmp/wovra-split-rehearsal/tasks/`，原会话一个字节不碰；脚本不删。
"""

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from wovra import registry as registry_module          # noqa: E402
from wovra import task as task_module                  # noqa: E402
from wovra.agent import MODE_MANAGED                   # noqa: E402
from wovra.cli import _build_agent                     # noqa: E402

REHEARSAL_ROOT = Path("/tmp/wovra-split-rehearsal/tasks")


def _stage_copy(src_id: str) -> Path:
    src = Path(task_module.TASKS_ROOT) / src_id
    if not (src / "task.json").is_file():
        raise SystemExit(f"会话不存在：{src}")
    REHEARSAL_ROOT.mkdir(parents=True, exist_ok=True)
    dst = REHEARSAL_ROOT / src_id
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)
    return dst


def _print_plan(agent, rounds) -> None:
    lines, n = agent._split_hard_data(rounds)
    print(f"\n=== 硬数据（{n} 个活性文件）===")
    for line in lines[:60]:
        print(line[:200])
    if len(lines) > 60:
        print(f"…（另 {len(lines) - 60} 行）")


def _print_result(agent, task, since_time: str = "") -> None:
    """打印本次练兵的**增量**（副本自带原会话历史，不过滤会看着像"又失败一次"）。"""
    rounds = task.rounds or []
    print("\n=== 轮状态 ===")
    for r in rounds:
        print(f"  R{r.get('seq')}: end_state={r.get('end_state')!r} "
              f"org_state={r.get('org_state')!r} split_state={r.get('split_state')!r}")
    doms = next((r.get("domains") for r in rounds if r.get("domains")), None)
    src = "已落地（r['domains']）"
    if not doms:
        # 还没到轮边界 → 产物在暂存区（`promote` 要下一轮开启才落 `domains`）
        doms = next((r["pending_org"]["domains"] for r in rounds
                     if isinstance(r.get("pending_org"), dict)
                     and r["pending_org"].get("domains")), None)
        src = "暂存中（pending_org，等轮边界生效）"
    print(f"\n=== 结构树（{src}）===")
    if not doms:
        print("  （没有产物——看下面的留痕）")
    else:
        for d in doms:
            mark = " [runtime_auto]" if d.get("runtime_auto") else ""
            file_n = len(d.get("files") or [])
            print(f"  - {d.get('name')}{mark}  parent={d.get('parent') or '（顶层）'}"
                  f"  文件 {file_n}  history {len(d.get('history_files') or [])}")
            desc = str(d.get("description") or "").replace("\n", " ")
            if desc:
                print(f"      desc: {desc[:90]}")
    print("\n=== 注册表 ===")
    for e in task.registry or []:
        print(f"  {e.get('id')}  {e.get('name')}  文件 {len(e.get('files') or [])}"
              f"  状态 {e.get('status')}")
    defects = registry_module.registry_defects(task.registry or [])
    print(f"  跨条目互斥体检：{'干净' if not defects else defects}")
    print(f"\n=== 本次练兵的留痕（只看 {since_time} 之后的）===")
    shown = 0
    for h in task.history or []:
        if since_time and str(h.get("time") or "") < since_time:
            continue          # 副本继承的历史不算本次结果
        kind = str(h.get("kind") or "")
        if kind.startswith("split") or (kind == "maintenance"
                                       and "split" in str(h.get("detail") or "")):
            print(f"  [{kind}] {str(h.get('detail'))[:180]}")
            shown += 1
    if not shown:
        print("  （没有维护留痕——注意：维护走的是后台线程/同步批，可能还没落）")
    print("\n=== 本次新增的 llm_call ===")
    for h in task.history or []:
        if since_time and str(h.get("time") or "") < since_time:
            continue
        if str(h.get("kind")) == "llm_call" and "[split]" in str(h.get("detail")):
            print(f"  {h.get('detail')[:160]}")


def main() -> int:
    ap = argparse.ArgumentParser(description="在会话副本上真跑一次维护（整理+分裂）")
    ap.add_argument("session_id")
    ap.add_argument("--dry", action="store_true", help="只出硬数据与指令，不调 LLM")
    args = ap.parse_args()

    dst = _stage_copy(args.session_id)
    task_module.TASKS_ROOT = dst.parent
    task = task_module.Task.load(args.session_id)
    if not task.rounds:
        raise SystemExit("会话没有轮，没什么可分")
    # 让全部已闭合轮进入本批（原会话它们多半已是 done——练兵的语义就是重做一次）
    for r in task.rounds:
        if str(r.get("end_state") or "") == "completed":
            r["org_state"] = ""
    print(f"副本：{dst}")
    print(f"轮数：{len(task.rounds)}  模型：{os.environ.get('Wovra_MODEL', '?')}")

    agent = _build_agent(task, mode=task.mode or MODE_MANAGED,
                         async_organization=False)   # 同步：跑完再打印
    agent.rounds = task.rounds
    if args.dry:
        _print_plan(agent, task.rounds)
        return 0

    agent.org_watermark = 0        # 强制本批（练的是分裂，不是水位）
    agent.org_grace_rounds = 0
    agent.org_cooldown_rounds = 0
    agent.last_context_estimate = max(
        int(getattr(agent, "last_context_estimate", 0) or 0), 10 ** 6)
    from datetime import datetime as _dt
    started_at = _dt.now().isoformat(timespec="seconds")
    print("\n跑维护（整理 → 分裂，真 LLM，约 1~2 分钟）…", flush=True)
    agent._maybe_organize_batch()
    task = task_module.Task.load(args.session_id)      # 重读：看落盘结果
    _print_result(agent, task, since_time=started_at)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
