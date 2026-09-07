"""把 tasks/ 历史会话按 Block 结构化渲染出来（机制一 · 离线检查）。

segment_round 是事件流的纯函数——历史 task.json 无需迁移即可结构化。
用法：
    uv run python scripts/render_blocks.py              # 全部任务
    uv run python scripts/render_blocks.py <task-id>    # 指定任务
只读分析，不修改 task.json。
"""

import sys

from wovra import blocks
from wovra import task as task_module
from wovra.task import Task


def main() -> None:
    root = task_module.TASKS_ROOT
    ids = sys.argv[1:]
    if not ids:
        ids = sorted(
            p.name for p in root.iterdir() if (p / "task.json").exists()
        )
    total_rounds = total_blocks = env_blocks = 0
    for tid in ids:
        try:
            task = Task.load(tid)
        except Exception as error:  # noqa: BLE001——坏档跳过，不挡整体
            print(f"!! {tid}: {error}")
            continue
        rounds = [r for r in task.rounds if r.get("events")]
        if not rounds:
            continue
        goal = (task.goal or "").strip()
        print(f"\n{'=' * 62}\n{tid} · 目标：{goal} · {len(rounds)} 轮")
        for r in rounds:
            bs = blocks.segment_round(r)  # 算一次，渲染与统计共用
            text = blocks.render_round(r, bs)
            if not text:
                continue
            total_rounds += 1
            total_blocks += len(bs)
            env_blocks += sum(1 for b in bs if b["kind"] == "environment")
            print(text)
    print(f"\n合计：{total_rounds} 轮 → {total_blocks} 块（其中环境块 {env_blocks} 个）")


if __name__ == "__main__":
    main()
