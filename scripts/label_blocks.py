"""机制二离线试跑：对真实会话的块做 LLM 语义标注（路由式摘要 + 大类归类）。

用法：uv run python scripts/label_blocks.py <task-id>
一次真实 LLM 调用（purpose=organization 计入维护账本），不落盘。
"""

import sys

from wovra.agent import Agent
from wovra.task import Task


def main() -> None:
    if len(sys.argv) < 2:
        print("用法: uv run python scripts/label_blocks.py <task-id>")
        return
    task = Task.load(sys.argv[1])
    agent = Agent(task=task)
    result = agent.label_blocks()

    cats = {c.get("id"): c for c in result["categories"]}
    print("== 大类 ==")
    for cid, c in cats.items():
        print(f"  {cid}. {c.get('name', '?')} — {c.get('description', '')}")
    counts: dict[str, int] = {}
    print("\n== 块标注（按轮）==")
    for r in task.rounds:
        bs = r.get("blocks") or []
        if not bs:
            continue
        print(f"R{r['seq']}:")
        for b in bs:
            lab = result["labels"].get(b["id"]) or {}
            cat = lab.get("category", "?")
            counts[cat] = counts.get(cat, 0) + 1
            print(f"  {b['id']} [{cat}] {lab.get('summary', '（无标注）')}")
    print("\n== 各类块数 ==")
    for cat, n in sorted(counts.items()):
        name = cats.get(cat, {}).get("name", "?")
        print(f"  [{cat}] {name}: {n} 块")
    unlabeled = sum(
        1 for r in task.rounds for b in r.get("blocks") or []
        if b["id"] not in result["labels"]
    )
    if unlabeled:
        print(f"  （⚠ {unlabeled} 块未获得标注）")


if __name__ == "__main__":
    main()
