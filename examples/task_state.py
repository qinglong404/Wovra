"""演示任务状态持久化：停止-恢复（路线图阶段 2 的核心验收点）。

运行两次，观察第二次如何"接着上次干"：

    # 第一次：创建任务并干一部分活
    uv run python examples/task_state.py start

    # （此时进程已退出——注意没有任何东西留在内存里）

    # 第二次：从磁盘加载同一个任务继续干
    uv run python examples/task_state.py resume

两次运行之间唯一的桥梁是 tasks/<id>/ 下的 task.json 和 report.md。
第二次运行时可以打开 report.md 看：AI 之前做了什么、总结是什么，
全都是第一次运行写下来的。
"""

import sys

from wovra.agent import Agent, get_current_time, list_files, read_file
from wovra.llm import LLM
from wovra.task import Task

# 固定任务 id：两次运行通过它找到同一个任务
TASK_ID = "demo-task-state"

GOAL = "了解 examples 目录下的示例文件，并给每个示例写一句话用途说明。"


def make_agent(task: Task) -> Agent:
    """构造一个绑定任务的 Agent。"""
    return Agent(
        system_prompt="你是一个简洁的助手，需要时使用提供的工具获取真实信息。",
        tools=[get_current_time, list_files, read_file],
        task=task,
        on_tool_call=lambda name, args: print(f"[调用工具] {name}({args})"),
        on_tool_result=lambda name, result: print(f"[工具结果] {name} -> {result[:100]}"),
    )


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "start"

    if mode == "start":
        task = Task.load_or_create(TASK_ID, goal=GOAL)
        agent = make_agent(task)
        print(f"=== 新任务 {task.id} ===\n")
        answer = agent.run(
            "请先看看 examples 目录下有哪些文件，读取其中两个 .py 的开头，"
            "记住它们各自是干什么的。"
        )
        print(f"\n=== 第一次运行的回答 ===\n{answer}")

    elif mode == "resume":
        # 新进程、新 Agent，唯一的记忆来源是磁盘上的 task.json
        task = Task.load_or_create(TASK_ID, goal=GOAL)
        agent = make_agent(task)
        print(f"=== 恢复任务 {task.id}（历史事件 {len(task.history)} 条）===\n")
        answer = agent.run("继续这个任务：基于已有的进展，把每个示例的一句话用途说明完整给出。")
        print(f"\n=== 恢复后的回答 ===\n{answer}")

    else:
        raise SystemExit(f"未知模式: {mode}，请用 start 或 resume")

    print(f"\n任务状态已保存到 tasks/{TASK_ID}/（report.md 可直接打开查看）")


if __name__ == "__main__":
    main()
