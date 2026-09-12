"""取证：隔离否定断言报的「泄漏轮号 R2」是真泄漏还是断言假阳性。

用法：uv run --no-sync python output/_dbg_leak.py [task_id]
产物只打结论（≤20 行）：命中行原文 + 判定。
"""
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from wovra import task as task_module  # noqa: E402
from wovra import views as views_module  # noqa: E402
from wovra.task import Task  # noqa: E402

REAL = REPO / "tasks"


def main() -> int:
    args = sys.argv[1:]
    if args:
        task_id = args[0]
    else:
        cands = [p for p in REAL.glob("*/task.json") if p.parent.name]
        task_id = max(cands, key=lambda p: p.stat().st_mtime).parent.name
    with tempfile.TemporaryDirectory(prefix="wovra-leak-") as td:
        tmp = Path(td)
        shutil.copytree(REAL / task_id, tmp / task_id, dirs_exist_ok=True)
        task_module.TASKS_ROOT = tmp
        task = Task.load(task_id)
        built = views_module.build_views(
            task.rounds, task.get_state(), registry=task.registry
        )
        index = built["index"]
        hits = 0
        for name, view in (built.get("views") or {}).items():
            if name == views_module.MAIN_AGENT_ID:
                continue
            text = str(view.get("text") or "")
            own = {str(b) for b in (view.get("own_ids") or [])}
            hit_seqs = {
                int(item["seq"]) for bid, item in index.items()
                if str(bid) in own and item.get("seq")
            }
            for r in task.rounds:
                seq = r.get("seq")
                if not seq or int(seq) in hit_seqs:
                    continue
                token = f"[R{seq}]"
                if token not in text:
                    continue
                hits += 1
                for i, line in enumerate(text.splitlines(), start=1):
                    if token in line:
                        print(f"[{name}] 命中行 {i}：{line[:160]}")
                        break
                print(f"    该视图命中轮 seq = {sorted(hit_seqs)}")
                print(f"    该轮 merged_anchor = {r.get('merged_anchor')!r}"
                      f"　merged_skip = {r.get('merged_skip')!r}")
        print(f"结论：真实命中 {hits} 处")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
