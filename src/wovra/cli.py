"""Wovra 的统一命令行入口。

设计动机（为什么需要 CLI）：核心层（Task/Agent）已经具备
"任务常驻磁盘、人随时介入" 的能力，但此前唯一的入口是 examples——
每个都是一次性的硬编码剧本，导致"会话"和"磁盘上的任务"对不上号。
CLI 补上这扇门：

    wovra new   <目标>          新建会话（新任务）
    wovra run   <id> [指令]      对既有任务执行一轮
    wovra chat  <id>            进入交互模式，多轮对话，实时落盘
    wovra list                  列出所有会话
    wovra show  <id>            查看某会话的报告（人类视角）

每个命令都围绕磁盘上的 task.json 工作，因此"继续对话"不需要任何
额外机制——新进程从磁盘加载就是继续。

实现说明：
    * 只用标准库 argparse，不引入第三方 CLI 框架（低成本优先）
    * 这里只做参数解析和输出，所有逻辑都在 Task/Agent 里
    * 工具集暂时是内置的安全工具；"按任务声明工具"留给阶段 4
"""

import argparse
import json

from . import task as task_module
from .agent import Agent, get_current_time, list_files, read_file
from .task import Task


def _build_agent(task: Task) -> Agent:
    """为任务构造一个带默认工具集的 Agent。"""
    return Agent(
        system_prompt=(
            "你是 Wovra，一个长时运行任务的管理执行助手。"
            "需要时使用提供的工具获取真实信息，回答保持简洁。"
        ),
        tools=[get_current_time, list_files, read_file],
        task=task,
        on_tool_call=lambda name, args: print(f"  [调用工具] {name}({args})"),
        on_tool_result=lambda name, result: print(f"  [工具结果] {name} -> {result[:80]}"),
    )


def _load_task(task_id: str) -> Task:
    """加载任务；不存在时给出友好报错而不是堆栈。"""
    if not (task_module.TASKS_ROOT / task_id / "task.json").exists():
        raise SystemExit(f"任务不存在: {task_id}（用 `wovra list` 查看现有任务）")
    return Task.load(task_id)


# ---- 子命令实现 -----------------------------------------------------------


def cmd_new(args: argparse.Namespace) -> None:
    """wovra new：新建一个会话（任务）。"""
    task = Task.create(
        goal=args.goal,
        requirements=args.req or [],
        acceptance_criteria=args.criteria or [],
    )
    task.save()
    print(f"已创建任务: {task.id}")
    print(f"目标: {task.goal}")
    print(f"下一步: `wovra run {task.id} \"指令\"` 或 `wovra chat {task.id}`")


def cmd_run(args: argparse.Namespace) -> None:
    """wovra run：对既有任务执行一轮。

    不给指令时，让模型根据目标、摘要和最近事件自主决定下一步——
    这是"长时任务自主推进"的雏形。
    """
    task = _load_task(args.task_id)
    agent = _build_agent(task)
    instruction = args.instruction or (
        "请根据任务目标、之前的进展摘要和最近事件，自主决定下一步并继续推进。"
        "如果任务已无法推进，说明原因。"
    )
    answer = agent.run(instruction)
    print(f"\n助手> {answer}")


def cmd_chat(args: argparse.Namespace) -> None:
    """wovra chat：交互模式，多轮对话。

    与 run 的区别：一次进程内可以连续对话（内存里的 messages 保持
    完整），每一轮结束后状态都已落盘，Ctrl+C / exit 随时离开，
    下次 `wovra chat` 同一个 id 从磁盘接着来。
    """
    task = _load_task(args.task_id)
    agent = _build_agent(task)
    print(f"进入会话 {task.id}（目标：{task.goal}）")
    print("输入指令开始对话，exit / Ctrl+C 退出。\n")

    while True:
        try:
            user_input = input("你> ").strip()
        except (EOFError, KeyboardInterrupt):
            # Ctrl+C / Ctrl+D：正常离开。状态在每轮结束时就已落盘
            print(f"\n会话已保存。下次继续: wovra chat {task.id}")
            break
        if not user_input:
            continue
        if user_input.lower() in ("exit", "quit", "退出"):
            print(f"会话已保存。下次继续: wovra chat {task.id}")
            break

        answer = agent.run(user_input)
        print(f"\n助手> {answer}\n")


def cmd_list(args: argparse.Namespace) -> None:
    """wovra list：列出所有任务（按更新时间倒序）。"""
    root = task_module.TASKS_ROOT
    if not root.exists():
        print("还没有任何任务。用 `wovra new \"目标\"` 创建第一个。")
        return

    tasks = []
    for directory in sorted(root.iterdir()):
        state_file = directory / "task.json"
        if not state_file.is_file():
            continue  # 跳过非任务目录
        data = json.loads(state_file.read_text(encoding="utf-8"))
        tasks.append(data)

    if not tasks:
        print("还没有任何任务。用 `wovra new \"目标\"` 创建第一个。")
        return

    tasks.sort(key=lambda t: t["updated_at"], reverse=True)
    print(f"{'任务 id':<28}{'状态':<14}{'更新时间':<22}目标")
    for t in tasks:
        goal = t["goal"] if len(t["goal"]) <= 40 else t["goal"][:40] + "…"
        print(f"{t['id']:<28}{t['status']:<14}{t['updated_at']:<22}{goal}")


def cmd_show(args: argparse.Namespace) -> None:
    """wovra show：打印某任务的人类可读报告。"""
    task = _load_task(args.task_id)
    report = task_module.TASKS_ROOT / task.id / "report.md"
    print(report.read_text(encoding="utf-8"))


# ---- 参数解析与入口 ---------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="wovra",
        description="Wovra：面向结构化、长时运行 AI 工作的运行时。",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_new = sub.add_parser("new", help="新建任务（新会话）")
    p_new.add_argument("goal", help="任务目标")
    p_new.add_argument("--req", action="append", help="需求约束，可多次指定")
    p_new.add_argument("--criteria", action="append", help="验收标准，可多次指定")
    p_new.set_defaults(func=cmd_new)

    p_run = sub.add_parser("run", help="对任务执行一轮")
    p_run.add_argument("task_id", help="任务 id")
    p_run.add_argument("instruction", nargs="?", help="本轮指令；省略则由 AI 自主继续")
    p_run.set_defaults(func=cmd_run)

    p_chat = sub.add_parser("chat", help="进入交互式多轮对话")
    p_chat.add_argument("task_id", help="任务 id")
    p_chat.set_defaults(func=cmd_chat)

    p_list = sub.add_parser("list", help="列出所有任务")
    p_list.set_defaults(func=cmd_list)

    p_show = sub.add_parser("show", help="查看任务报告")
    p_show.add_argument("task_id", help="任务 id")
    p_show.set_defaults(func=cmd_show)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
