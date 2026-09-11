"""汇总多轮受控实验：会话级对比 + 逐轮明细。

读取 experiments/runs/*/meta.json、对应会话的 task.json、
acceptance-R*.json 验收快照。用法：uv run python experiments/collect.py
"""

import json
import re
import statistics
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from wovra.task import Task  # noqa: E402

RUNS = HERE / "runs"


def _int(pattern: str, text: str) -> int:
    m = re.search(pattern, text)
    return int(m.group(1).replace(",", "")) if m else 0


def session_metrics(run_dir: Path) -> dict | None:
    meta_path = run_dir / "meta.json"
    if not meta_path.is_file():
        return None
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    task = Task.load(run_dir.name)

    # 逐轮：usage 行（steps/total/等效输入，按记录顺序即轮次顺序）
    per_round = []
    for e in task.history:
        if e["kind"] != "usage":
            continue
        detail = e["detail"]
        per_round.append({
            "steps": _int(r"steps=([\d,]+)", detail),
            "context": _int(r"context=([\d,]+)", detail),
            "total": _int(r"total=([\d,]+)", detail),
            "effective": _int(r"等效输入 ([\d,]+) tok", detail),
        })

    # 行为计数：read_file 明细（同路径同区间重复 = 重读）、expand_history
    reads: dict[tuple, int] = {}
    expand_calls = 0
    for r in task.rounds:
        for e in r.get("events", []):
            if e["type"] != "tool_call":
                continue
            for tc in e["message"].get("tool_calls", []) or []:
                fn = tc.get("function", {})
                if fn.get("name") == "read_file":
                    try:
                        a = json.loads(fn.get("arguments") or "{}")
                        key = (a.get("path"), a.get("start_line"), a.get("num_lines"))
                        reads[key] = reads.get(key, 0) + 1
                    except ValueError:
                        pass
                elif fn.get("name") == "expand_history":
                    expand_calls += 1

    # 验收快照：每轮闭合后的累计通过
    snapshots = {}
    for p in sorted(run_dir.glob("acceptance-R*.json")):
        snap = json.loads(p.read_text(encoding="utf-8"))
        if snap.get("round") is not None:
            snapshots[snap["round"]] = snap["passed"]
    final_snap = run_dir / "acceptance-final.json"
    final_passed = (json.loads(final_snap.read_text(encoding="utf-8"))["passed"]
                    if final_snap.is_file() else max(snapshots.values(), default=0))
    regressions = sum(
        1 for a, b in zip(sorted(snapshots), sorted(snapshots)[1:])
        if snapshots[b] < snapshots[a]
    )

    return {
        "task_id": run_dir.name,
        "mode": meta["mode"],
        "index": meta["index"],
        "rounds": len(task.rounds),
        "per_round": per_round,
        "steps": sum(p["steps"] for p in per_round),
        "total": sum(p["total"] for p in per_round),
        "effective": sum(p["effective"] for p in per_round),
        "context_peak": max((p["context"] for p in per_round), default=0),
        "reads": sum(reads.values()),
        "re_reads": sum(c - 1 for c in reads.values() if c > 1),
        "expand_calls": expand_calls,
        "snapshots": snapshots,
        "final_passed": final_passed,
        # 功能总数来自实验自己的 meta.json["criteria"]（原先读
        # Task.acceptance_criteria，该字段 2026-09-11 遗产整治已删；
        # 旧运行记录没有 criteria 时回退为 10——当年的清单长度）
        "criteria_total": len(meta.get("criteria") or []) or 10,
        "regressions": regressions,
        "cost_per_feature": (sum(p["effective"] for p in per_round) / final_passed
                             if final_passed else None),
    }


def main() -> None:
    run_dirs = sorted(p for p in RUNS.iterdir() if p.is_dir()) if RUNS.is_dir() else []
    sessions = [m for d in run_dirs if (m := session_metrics(d)) is not None]
    if not sessions:
        print("experiments/runs/ 下还没有运行记录。先用 new_run.py 建会话。")
        return

    for s in sorted(sessions, key=lambda x: (x["mode"], x["index"])):
        label = f"{s['mode']}-{s['index']}"
        print(f"\n=== 会话 {label}（{s['task_id']}，{s['rounds']} 轮）===")
        print(f"{'轮':>3} {'步数':>4} {'上下文':>10} {'名义total':>10} {'等效输入':>10} {'累计通过':>6}")
        cum = 0
        for i, pr in enumerate(s["per_round"], 1):
            cum = s["snapshots"].get(i, cum)
            print(f"R{i:<2} {pr['steps']:>4} {pr['context']:>10,} {pr['total']:>10,} "
                  f"{pr['effective']:>10,} {cum:>4}/{s['criteria_total']}")
        print(f"  步数合计 {s['steps']:,} | 名义合计 {s['total']:,} | "
              f"等效合计 {s['effective']:,} | 上下文峰值 {s['context_peak']:,} | "
              f"重读 {s['re_reads']} | expand {s['expand_calls']} | 回归 {s['regressions']} 次 | "
              f"最终通过 {s['final_passed']}/{s['criteria_total']}")

    print("\n=== 模式汇总（会话中位数）===")
    for mode in ("managed", "baseline"):
        group = [s for s in sessions if s["mode"] == mode]
        if not group:
            continue
        med = statistics.median
        print(f"[{mode}] n={len(group)} 条会话 | "
              f"等效合计 {med(s['effective'] for s in group):,.0f} | "
              f"步数 {med(s['steps'] for s in group):.0f} | "
              f"重读 {med(s['re_reads'] for s in group):.0f} | "
              f"回归 {med(s['regressions'] for s in group):.0f} | "
              f"最终通过 {med(s['final_passed'] for s in group):.0f}")
        valid = [s for s in group if s["cost_per_feature"]]
        if valid:
            print(f"  主指标（累计等效输入/通过功能数）中位数："
                  f"{med(s['cost_per_feature'] for s in valid):,.0f} tok/功能")


if __name__ == "__main__":
    main()
