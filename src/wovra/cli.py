"""Wovra 的统一命令行入口。

设计动机（为什么需要 CLI）：核心层（Task/Agent）已经具备
"任务常驻磁盘、人随时介入" 的能力，但此前唯一的入口是 examples——
每个都是一次性的硬编码剧本，导致"会话"和"磁盘上的任务"对不上号。
CLI 补上这扇门：

    wovra new   <目标>          新建会话（新任务）
    wovra run   <id> [指令]      对既有任务执行一轮
    wovra chat  <id>            进入交互模式，多轮对话，实时落盘
    wovra list                  列出所有会话（带数字编号）
    wovra show  <id>            查看某会话的报告（人类视角）

编号与短 id：list 按更新时间倒序给每个会话编号（1、2、3…），
run / chat / show 的 <id> 既接受完整任务 id，也接受这个数字编号——
数字是给人输入用的，完整 id 是给脚本和记录用的。

实现说明：
    * 只用标准库 argparse + ANSI 转义（见 ui.py），不引入第三方依赖
    * 这里只做参数解析和输出，所有逻辑都在 Task/Agent 里
    * 工具集暂时是内置的安全工具；"按任务声明工具"留给阶段 4
"""

import argparse
import json

from . import task as task_module
from . import ui
from .agent import (
    Agent,
    edit_file,
    get_current_time,
    list_files,
    read_file,
    run_command,
    search_files,
    write_file,
)
from .task import Task


def _build_agent(task: Task) -> Agent:
    """为任务构造一个带默认工具集的 Agent。

    工具分两类：只读（时间/列目录/读文件/搜索）与变更类
    （写文件/改文件/执行命令，均有审计记录与破坏性防护，见 tools.py）。
    """
    return Agent(
        system_prompt=(
            "你是 Wovra，一个长时运行任务的管理执行助手。"
            "需要时使用提供的工具获取真实信息：找内容用 search_files，"
            "读大文件用 start_line 分段；可以创建、修改项目内的文件，"
            "运行安全的 shell 命令来完成任务。回答保持简洁。"
        ),
        tools=[
            get_current_time,
            list_files,
            read_file,
            search_files,
            write_file,
            edit_file,
            run_command,
        ],
        task=task,
        on_tool_call=lambda name, args: print(ui.tool_call(name, args)),
        on_tool_result=lambda name, result: print(ui.tool_result(name, result)),
    )


def _all_tasks() -> list[dict]:
    """按更新时间倒序读出所有任务（与 list 的展示顺序一致）。

    编号就是这份倒序列表的下标——因此"最近更新的任务永远是 1"，
    编号会随任务活跃程度变化，完整 id 才是稳定标识。
    """
    root = task_module.TASKS_ROOT
    if not root.exists():
        return []
    tasks = []
    for directory in sorted(root.iterdir()):
        state_file = directory / "task.json"
        if not state_file.is_file():
            continue  # 跳过非任务目录
        tasks.append(json.loads(state_file.read_text(encoding="utf-8")))
    tasks.sort(key=lambda t: t["updated_at"], reverse=True)
    return tasks


def _resolve_task_id(ref: str) -> str:
    """把用户输入的编号（如 "1"）或完整任务 id 解析成任务 id。

    纯数字 → 按当前 list 顺序取第 N 个；其余按完整 id 处理。
    """
    if ref.isdigit():
        tasks = _all_tasks()
        index = int(ref)
        if not 1 <= index <= len(tasks):
            raise SystemExit(
                ui.error(f"编号 {index} 不存在，有效范围是 1~{len(tasks)}。用 `wovra list` 查看。")
            )
        return tasks[index - 1]["id"]
    return ref


def _load_task(ref: str) -> Task:
    """加载任务；不存在时给出友好报错而不是堆栈。"""
    task_id = _resolve_task_id(ref)
    if not (task_module.TASKS_ROOT / task_id / "task.json").exists():
        raise SystemExit(ui.error(f"任务不存在: {ref}（用 `wovra list` 查看现有任务）"))
    return Task.load(task_id)


def _replay_history(task: Task, last_n: int = 12) -> None:
    """进入 chat 时回放之前的会话记录，让"继续对话"有上下文感。

    只回放对话性事件（用户输入、回答、工具活动）；task_context_loaded
    这类系统事件对人没有信息量，跳过。完整历史永远在 task.json 里。
    """
    kinds = ("user_input", "final_answer", "tool_call", "tool_result")
    dialogue = [e for e in task.history if e["kind"] in kinds]
    if not dialogue:
        print(ui.info("（这是新会话，还没有历史记录）"))
        return

    print(ui.rule("之前的会话记录"))
    for event in dialogue[-last_n:]:
        if event["kind"] == "user_input":
            print(ui.user(event["detail"]))
        elif event["kind"] == "final_answer":
            ui.assistant_markdown(event["detail"])
        elif event["kind"] == "tool_call":
            name, call_args = _split_call(event["detail"])
            # 折叠换行：工具参数/结果可能多行，回放里必须保持单行
            print(ui.tool_call(name, " ".join(call_args.split())))
        else:  # tool_result
            name, _, result = event["detail"].partition(" -> ")
            print(ui.tool_result(name, " ".join(result.split())))
    if len(dialogue) > last_n:
        print(ui.info(f"（仅显示最近 {last_n} 条，完整记录见 report.md）"))
    print(ui.rule())


def _split_call(detail: str) -> tuple[str, str]:
    """把 "tool_name({...})" 形式的工具调用 detail 拆回名字和参数。"""
    name, _, arguments = detail.partition("(")
    return name, arguments.removesuffix(")")


# ---- 子命令实现 -----------------------------------------------------------


def _run_turn(agent: Agent, instruction: str) -> str:
    """执行一轮流式对话并负责全部展示。

    思考内容暗紫流式输出、正式回答默认色打字机输出，二者用横幅
    分隔；结束后打印成本核算行。状态字典在闭包间共享，用来保证
    各横幅只打印一次。
    """
    state = {"thinking": False, "answer": False}

    def on_thinking(text: str) -> None:
        if not state["thinking"]:
            print(ui.rule("思考过程"))
            state["thinking"] = True
        print(ui.thinking_delta(text), end="", flush=True)

    def on_answer_delta(text: str) -> None:
        if state["thinking"] and not state["answer"]:
            print("\n" + ui.rule("回答"))
            state["answer"] = True
        print(text, end="", flush=True)

    answer = agent.run(
        instruction, on_thinking=on_thinking, on_answer_delta=on_answer_delta
    )
    print()
    print(ui.usage_line(agent.last_stats))
    return answer


def cmd_new(args: argparse.Namespace) -> None:
    """wovra new：新建一个会话（任务）。

    目标参数是可选的：目标不是开工的前提，而是对话的产物——
    AI 会在每轮对话后重新评估并更新它。
    """
    task = Task.create(goal=args.goal or "")
    task.save()
    print(ui.success(f"已创建新会话: {task.id}"))
    if task.goal:
        print(f"初始意图: {task.goal}")
    print(ui.info(f"直接 `wovra chat {task.id}` 开始对话，目标会随对话自动成形。"))


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
    _run_turn(agent, instruction)


def _chat_help() -> None:
    """chat 模式内的帮助。"""
    print(ui.rule("chat 模式帮助"))
    print("直接输入文字即可对话，每轮结束自动保存到磁盘。")
    print(f"  {ui.paint('help / 帮助', 'bold')}      显示本帮助")
    print(f"  {ui.paint('exit / quit / 退出', 'bold')}  保存并离开会话")
    print(f"  {ui.paint('Ctrl+C / Ctrl+D', 'bold')}  同 exit")
    print(ui.rule())


def cmd_chat(args: argparse.Namespace) -> None:
    """wovra chat：交互模式，多轮对话。

    不带 id 时自动新建会话——想聊就直接聊，不需要先想清楚目标；
    带 id（或编号）则续上既有会话。每一轮结束后状态都已落盘，
    Ctrl+C / exit 随时离开，下次从磁盘接着来。
    """
    if args.task_id:
        task = _load_task(args.task_id)
    else:
        task = Task.create(goal="")
        task.save()
        print(ui.info(f"已创建新会话 {task.id}"))
    agent = _build_agent(task)

    print(ui.rule("Wovra 会话"))
    print(f"{ui.paint('任务', 'bold')}  {task.id}")
    print(f"{ui.paint('目标', 'bold')}  {task.goal or '（未定，将随对话成形）'}")
    print(f"{ui.paint('状态', 'bold')}  {ui.status(task.status)}")
    print(ui.rule())

    _replay_history(task)
    print(ui.info("输入指令开始对话，help 查看帮助，exit 退出。\n"))

    while True:
        try:
            user_input = input(ui.user_prompt()).strip()
        except (EOFError, KeyboardInterrupt):
            # Ctrl+C / Ctrl+D：正常离开。状态在每轮结束时就已落盘
            print(f"\n{ui.success(f'会话已保存。下次继续: wovra chat {task.id}')}")
            break
        if not user_input:
            continue
        if user_input.lower() in ("exit", "quit", "退出"):
            print(ui.success(f"会话已保存。下次继续: wovra chat {task.id}"))
            break
        if user_input.lower() in ("help", "帮助"):
            _chat_help()
            continue

        answer = _run_turn(agent, user_input)
        if answer:
            print()


def cmd_list(args: argparse.Namespace) -> None:
    """wovra list：列出所有任务（按更新时间倒序，带数字编号）。"""
    tasks = _all_tasks()
    if not tasks:
        print(ui.info('还没有任何任务。用 `wovra new "目标"` 创建第一个。'))
        return

    print()
    # 列对齐必须"先 pad 再 paint"（见 ui.pad 的说明），否则着色码
    # 会破坏 f-string 的补齐计算，列会粘在一起
    header = (
        ui.paint(ui.pad("编号", 6), "bold")
        + ui.paint(ui.pad("任务 id", 26), "bold")
        + ui.paint(ui.pad("状态", 10), "bold")
        + ui.paint(ui.pad("更新时间", 18), "bold")
        + ui.paint("目标", "bold")
    )
    print(header)
    for number, t in enumerate(tasks, start=1):
        goal = t["goal"] or "（目标待明确）"
        goal = goal if len(goal) <= 36 else goal[:36] + "…"
        updated = t["updated_at"].replace("T", " ")[:16]
        print(
            ui.paint(ui.pad(str(number), 6), "cyan")
            + ui.pad(t["id"], 26)
            + ui.status(t["status"], width=10)
            + ui.pad(updated, 18)
            + goal
        )
    print(ui.info("\n编号按最近更新排序，run/chat/show 可直接用编号作为 <id>。"))


def cmd_show(args: argparse.Namespace) -> None:
    """wovra show：打印某任务的人类可读报告。"""
    task = _load_task(args.task_id)
    report = task_module.TASKS_ROOT / task.id / "report.md"
    print(report.read_text(encoding="utf-8"))


# ---- 参数解析与入口 ---------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="wovra",
        description=ui.paint("Wovra：面向结构化、长时运行 AI 工作的运行时。", "bold"),
        epilog=(
            "示例：\n"
            "  wovra chat                  开一个新会话直接聊，目标随对话成形\n"
            '  wovra new "调研某主题"       带初始意图新建会话（也可以不填）\n'
            "  wovra list                  查看所有会话（带编号）\n"
            "  wovra chat 1                用编号续上某个会话\n"
            "  wovra show 1                查看会话报告\n"
            "\n"
            "<id> 位置既可用编号（list 里的 1、2、3…），也可用完整任务 id。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command")

    p_new = sub.add_parser("new", help="新建会话（目标可不填，随对话成形）")
    p_new.add_argument("goal", nargs="?", default="", help="可选的初始意图")
    p_new.set_defaults(func=cmd_new)

    p_run = sub.add_parser("run", help="对任务执行一轮")
    p_run.add_argument("task_id", help="任务编号或完整任务 id")
    p_run.add_argument("instruction", nargs="?", help="本轮指令；省略则由 AI 自主继续")
    p_run.set_defaults(func=cmd_run)

    p_chat = sub.add_parser("chat", help="交互式多轮对话（不带 id 则新建会话）")
    p_chat.add_argument("task_id", nargs="?", default="", help="任务编号或完整任务 id；省略则新建")
    p_chat.set_defaults(func=cmd_chat)

    p_list = sub.add_parser("list", help="列出所有任务（带编号）")
    p_list.set_defaults(func=cmd_list)

    p_show = sub.add_parser("show", help="查看任务报告")
    p_show.add_argument("task_id", help="任务编号或完整任务 id")
    p_show.set_defaults(func=cmd_show)

    sub.add_parser("help", help="显示帮助")

    args = parser.parse_args(argv)

    # 裸 `wovra` / `wovra help`：打印帮助而不是报错
    if args.command is None or args.command == "help":
        parser.print_help()
        return

    args.func(args)


if __name__ == "__main__":
    main()
