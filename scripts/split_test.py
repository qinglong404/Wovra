"""分裂管线真实测试：R1-R28（第一次水位触发点）跑 org → split 串行。

用法：.venv/bin/python scripts/split_test.py
观察点：
  - split 是否骑上 org 前缀缓存（cached 应接近 org 的 prompt）
  - thoughts（保底块归属：独立思想归主 agent）
  - domains 层级（语义聚合、description、parent）
  - split_assessment（本会话单一文件域 → 预期 splittable=false）
"""

import json
import shutil

from wovra import task as task_module
from wovra.agent import MODE_MANAGED
from wovra.cli import _build_agent

SRC_ID = "20260909-181052-643355"
DST_ID = "20260909-181052-orgtest"


def main() -> None:
    root = task_module.TASKS_ROOT
    src, dst = root / SRC_ID, root / DST_ID
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)
    task = task_module.Task.load(DST_ID)
    task.id = DST_ID
    agent = _build_agent(task, mode=MODE_MANAGED, async_organization=False)
    rounds = [r for r in agent.rounds if r["seq"] <= 28]
    print(f"轮数: {len(rounds)}（R1-R28，第一次水位触发点）")

    base = agent._assemble_messages()
    org_ok, exchange = agent._organize_rounds(rounds, base)
    print("org:", org_ok)
    if not org_ok:
        return
    split_ok = agent._split_rounds(rounds, exchange, base)
    print("split:", split_ok)

    pending = rounds[0].get("pending_org") or {}
    print("\n===== thoughts（保底块归属）=====")
    for t in task.rounds[0].get("pending_org", {}).get("thoughts") or []:
        print(json.dumps(t, ensure_ascii=False))
    print("\n===== domains =====")
    print(json.dumps(pending.get("domains"), ensure_ascii=False, indent=1)[:3000])
    print("\n===== split_assessment =====")
    print(json.dumps(pending.get("split_assessment"), ensure_ascii=False, indent=1))
    print("\n===== unassigned =====")
    print(json.dumps(pending.get("unassigned"), ensure_ascii=False)[:300])

    print("\n===== LLM 调用记录 =====")
    for h in task.history:
        if h["kind"] == "llm_call" and (
            "organization" in h["detail"] or "split" in h["detail"]
        ):
            print(h["detail"])


if __name__ == "__main__":
    main()
