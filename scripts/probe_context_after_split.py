"""查"分裂后重组的上下文"（只读，零 LLM）——**用 Task 对象走仪器的同一条调用**。

教训：第一版我拿原始 dict 去喂 `views.build_views`，形状不对、结果不可信 ✗。
现在照 `scripts/maint_health.py` 的原样调：`build_views(task.rounds, task.get_state(),
registry=task.registry)`。

用法：`uv run --no-sync python scripts/probe_context_after_split.py <会话ID>`
"""
from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path("src")))
from wovra import views as V  # noqa: E402
from wovra.task import Task  # noqa: E402


def main() -> int:
    if len(sys.argv) < 2:
        print("用法：probe_context_after_split.py <会话ID>")
        return 2
    sid = sys.argv[1]
    task = Task.load(sid)
    raw = json.loads((pathlib.Path("tasks") / sid / "task.json").read_text(encoding="utf-8"))
    rounds = task.rounds

    print(f"=== 会话 {sid}：轮 {len(rounds)}　域 {len(task.registry)} ===")
    built = V.build_views(rounds, task.get_state(), registry=task.registry)
    idx = built.get("index") or {}
    gap = V.coverage_gap(built.get("domains"), rounds,
                         index=idx, owners=built.get("owners"))
    uncovered = list(gap.get("uncovered") or [])

    by_file: dict[str, list[str]] = {}
    for r in rounds:
        for b in (r.get("blocks") or []):
            if str(b.get("kind")) != "file":
                continue
            f = str(b.get("file") or "").replace("\\", "/")
            by_file.setdefault(f, []).append(f"R{r.get('seq')}-{b.get('id') or '?'}")

    print(f"\n=== 未覆盖文件 {len(uncovered)} 个（不属于任何域 → 全落进主 agent 桶）===")
    rows = sorted(((f, len(by_file.get(str(f).replace(chr(92), '/'), []))) for f in uncovered),
                  key=lambda x: -x[1])
    for f, n in rows[:18]:
        print(f"  {n:>3} 块  {f}")
    if len(rows) > 18:
        print(f"  …另有 {len(rows)-18} 个")

    print("\n=== 一轮命中 ≥2 个域（分域过细/父子重叠的信号）===")
    counts = gap.get("round_domain_counts") or {}
    ov = gap.get("overlapped_rounds") or []
    for r in ov:
        print(f"  R{r} → {counts.get(str(r)) or counts.get(r)} 域同时命中")
    if not ov:
        print("  无")

    print("\n=== 可疑路径（工作区之外的绝对路径）===")
    sus = [f for f in by_file if (":" in f or f.startswith("/") or f.startswith(".."))]
    for f in sus:
        print(f"  {f}  ← {by_file[f][:4]}")
        # 这一块是空的还是有内容？事件里有没有提过这个路径？
        for r in rounds:
            for b in (r.get("blocks") or []):
                if str(b.get("file") or "").replace("\\", "/") == f:
                    print(f"      轮 {r.get('seq')} 块 {b.get('id')} items="
                          f"{len(b.get('items') or [])}")
        hits = []
        for r in rounds:
            blob = json.dumps(r.get("events") or [], ensure_ascii=False)
            if f in blob or f.split("/")[-1] in blob:
                hits.append(f"R{r.get('seq')}")
        print(f"      事件里提到它的轮：{hits or '无（看着像标注模型自己填的路径）'}")
    if not sus:
        print("  无")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
