"""域视图「材料体量 vs 装配体量」分桶法医（2026-09-12，常驻诊断）。

背景（会话 20260912-110034-98d42c）：`scripts/maint_health.py` 的
「视图装配 A/B」报 `A-2 上下文维护与整理分裂管线 90,979 tok（旧全量 42%）`，
而同一域的**材料体量**（`views.view_watermarks` / `wovra views`）只有
5,144 tok——同一份材料两个口径差 17 倍。分裂的「拆不拆」判断建立在这两个
体量读数上（经济判据 `(B − B′) × N_future − C_split`），口径互相矛盾时
读数不可信，故差额必须落到**具体消息**上，而不是靠推测。

本脚本只读（复制会话到临时目录后分析），输出每视图的装配总量与 top 消息；
用于回答「多出来的 tok 是哪条消息带来的」。

用法：
    uv run --no-sync python scripts/diag_view_assembly_buckets.py [task_id]
    省略 task_id 取最近更新的会话。
"""
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from wovra import task as task_module  # noqa: E402
from wovra import tokens as tokens_module  # noqa: E402
from wovra import views as views_module  # noqa: E402
from wovra.agent import Agent  # noqa: E402
from wovra.task import Task  # noqa: E402

TOP_VIEWS = 4
TOP_MSGS = 3


def _latest_task_id() -> str:
    root = REPO / "tasks"
    dirs = [d for d in root.iterdir() if d.is_dir() and (d / "task.json").exists()]
    return max(dirs, key=lambda d: (d / "task.json").stat().st_mtime).name


def main() -> int:
    task_id = sys.argv[1] if len(sys.argv) > 1 else _latest_task_id()
    with tempfile.TemporaryDirectory(prefix="wovra-diag-") as td:
        tmp = Path(td)
        shutil.copytree(REPO / "tasks" / task_id, tmp / task_id, dirs_exist_ok=True)
        task_module.TASKS_ROOT = tmp
        task: Task = Task.load(task_id)

        agent = Agent(llm=SimpleNamespace(model="stub"), tools=[], task=task)
        try:
            from wovra.cli.prompt import _system_prompt

            agent.system_prompt = _system_prompt("managed")
        except Exception:  # noqa: BLE001——提示词不可得不影响本诊断
            pass

        state = task.get_state()
        built = views_module.build_views(task.rounds, state, registry=task.registry)
        views = built.get("views") or {}
        marks = views_module.view_watermarks(
            task.rounds, state, registry=task.registry
        )
        organized = [r for r in task.rounds if r.get("org_state") == "done"]
        raw = [r for r in task.rounds if r.get("org_state") != "done"]
        raw_tok = Agent._estimate_messages(
            [e["message"] for r in raw for e in r.get("events") or []]
        ) if raw else 0
        print(f"会话 {task_id}　轮 {len(task.rounds)}（已整理 {len(organized)}／"
              f"未整理 {len(raw)}，未整理原文 {raw_tok:,} tok）")
        print(f"装配总量（主 agent 视图）{int(agent.last_context_estimate or 0):,} tok"
              f"　当前开放轮 {len(agent.current_round.get('events') or []) if agent.current_round else 0} 事件")
        ranked = sorted(
            views, key=lambda n: -int((views[n] or {}).get("est_tokens") or 0)
        )[:TOP_VIEWS]
        for name in ranked:
            material = int((marks.get(name) or {}).get("tokens") or 0)
            msgs = agent._assemble_view_messages(name, agent.rounds)
            if not msgs:
                print(f"  {name}：材料 {material:,} tok → 装配「无产物降级」")
                continue
            total = Agent._estimate_messages(msgs)
            ratio = f"{total / material:.1f}×" if material else "—"
            print(f"  {name}：材料 {material:,} tok → 装配 {total:,} tok（{ratio}）"
                  f"　消息 {len(msgs)} 条")
            for est, m in sorted(
                ((Agent._estimate_messages([m]), m) for m in msgs),
                key=lambda x: -x[0],
            )[:TOP_MSGS]:
                head = str(m.get("content") or "")[:58].replace("\n", " ")
                print(f"      {est:>7,} tok  {str(m.get('role')):<9} {head}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
