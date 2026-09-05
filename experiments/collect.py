"""汇总受控实验：逐轮指标表 + 两模式中位数对比。

读取 experiments/runs/*/meta.json、对应会话的 task.json 与静态验收
结果。用法：uv run python experiments/collect.py
"""

import json
import re
import statistics
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from check_acceptance import check  # noqa: E402
from wovra.task import Task  # noqa: E402

RUNS = HERE / "runs"


def _int(pattern: str, text: str) -> int:
    m = re.search(pattern, text)
    return int(m.group(1).replace(",", "")) if m else 0


def run_metrics(run_dir: Path) -> dict | None:
    meta_path = run_dir / "meta.json"
    if not meta_path.is_file():
        return None
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    task = Task.load(run_dir.name)

    total = prompt = completion = effective = steps = 0
    for e in task.history:
        if e["kind"] != "usage":
            continue
        detail = e["detail"]
        total += _int(r"total=([\d,]+)", detail)
        prompt += _int(r"prompt=([\d,]+)", detail)
        completion += _int(r"completion=([\d,]+)", detail)
        steps += _int(r"steps=([\d,]+)", detail)
        effective += _int(r"等效输入 ([\d,]+) tok", detail)

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
    re_reads = sum(c - 1 for c in reads.values() if c > 1)

    # 静态验收：运行目录里最新的 html（起始文件之外的就是产出）
    produced = [
        p for p in sorted(run_dir.rglob("*.html"))
        if p.name != "chat.html" or (run_dir / "chat.html").stat().st_size
        != (HERE / "fixtures" / "chat-page.html").stat().st_size
    ]
    html_path = produced[-1] if produced else run_dir / "chat.html"
    results = check(html_path.read_text(encoding="utf-8", errors="replace"))
    passed = sum(results.values())
    criteria_total = len(results)

    return {
        "task_id": run_dir.name,
        "mode": meta["mode"],
        "index": meta["index"],
        "steps": steps,
        "reads": sum(reads.values()),
        "re_reads": re_reads,
        "expand_calls": expand_calls,
        "total": total,
        "effective": effective,
        "passed": passed,
        "criteria_total": criteria_total,
        "cost_per_passed": (effective / passed) if passed else None,
    }


def main() -> None:
    run_dirs = sorted(p for p in RUNS.iterdir() if p.is_dir()) if RUNS.is_dir() else []
    rows = [m for d in run_dirs if (m := run_metrics(d)) is not None]
    if not rows:
        print("experiments/runs/ 下还没有运行记录。先用 new_run.py 建轮。")
        return

    header = (f"{'运行':<24} {'模式':<9} {'步数':>4} {'read':>5} {'重读':>4} "
              f"{'expand':>6} {'名义total':>10} {'等效输入':>10} {'验收':>5} {'等效/条':>9}")
    print(header)
    for row in sorted(rows, key=lambda r: (r["mode"], r["index"])):
        cpp = f"{row['cost_per_passed']:,.0f}" if row["cost_per_passed"] else "∞"
        print(f"{row['task_id']:<24} {row['mode']:<9} {row['steps']:>4} {row['reads']:>5} "
              f"{row['re_reads']:>4} {row['expand_calls']:>6} {row['total']:>10,} "
              f"{row['effective']:>10,} {row['passed']}/{row['criteria_total']:>3} {cpp:>9}")

    for mode in ("managed", "baseline"):
        group = [r for r in rows if r["mode"] == mode]
        if not group:
            continue
        med = statistics.median
        print(f"\n[{mode}] n={len(group)} 中位数："
              f"步数 {med(r['steps'] for r in group):.0f} | "
              f"重读 {med(r['re_reads'] for r in group):.0f} | "
              f"等效输入 {med(r['effective'] for r in group):,.0f} | "
              f"验收通过 {med(r['passed'] for r in group):.0f}")
        valid = [r for r in group if r["cost_per_passed"]]
        if valid:
            print(f"  主指标（等效输入/通过条数）中位数："
                  f"{med(r['cost_per_passed'] for r in valid):,.0f} tok")


if __name__ == "__main__":
    main()
