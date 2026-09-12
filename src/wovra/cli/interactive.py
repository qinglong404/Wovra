"""交互模式：cmd_chat 主循环、本地命令（\\bg / \\undo / \\c）、输入读取、
帮助文本。本地命令零模型成本——永远不进对话。
"""
import argparse
import os
import sys

from .. import ui
from ..task import Task
from ..tools import (
    check_background,
    list_background,
    stop_background,
    stop_session_backgrounds,
)

from .prompt import _build_agent
from .session import _acquire_session_lock, _child_summaries, _load_task, _record_leftover_maintenance, _release_session_lock, _resolve_mode, _resume_command
from .render import _drain_status, _replay_history, _run_turn

def _flush_stdin() -> None:
    """清空终端输入缓冲。

    流式输出/工具执行的几十秒里用户往往已经开始敲键——这些按键
    会留在终端输入缓冲里，等下一次读输入时被瞬间吞掉当成提交。
    Windows 没有 termios，此前这里是静默 no-op，等待期的回车会
    堆积成连串空输入；Windows 用 msvcrt 逐个取走缓冲事件。
    宁可让用户重打这几个字，也不误发半句话。
    """
    if os.name == "nt":
        import msvcrt

        for _ in range(1000):  # 上限防病态循环
            if not msvcrt.kbhit():
                break
            msvcrt.getwch()  # 宽字符版：中文/功能键也一并丢弃
        return
    try:
        import termios

        termios.tcflush(sys.stdin, termios.TCIFLUSH)
    except Exception:  # noqa: BLE001——非 POSIX 平台没有 termios，跳过即可
        pass

def _toolbar_text(task: Task) -> str:
    """输入行底栏：当前阶段/工作项 + 快捷键提示。

    纯文本——prompt_toolkit 的 bottom_toolbar 不解析裸 ANSI 转义，
    ui.paint 的着色码会原样显示，所以这里不套颜色（着色归终端主题）。
    """
    return f" {task.todo_summary_line()}   ｜  F2 报告 · F3 阶段 · \\help 命令 "

def _make_prompt_session(task: Task, input=None, output=None):  # noqa: A002
    """构造带底栏与快捷键的输入会话；非 TTY 或无 prompt_toolkit 返回 None。

    F2/F3 不在终端内直接打印，而是让 prompt() 返回对应本地命令——长
    文本渲染仍走 cmd_chat 的打印路径，绕开在 PromptSession 内部打印
    与 patch_stdout / 后台线程抢终端的坑。

    input/output 仅供测试注入 prompt_toolkit 的管道输入/哑输出；
    生产路径保持默认（真实终端）。
    """
    if not sys.stdin.isatty():
        return None
    try:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.formatted_text import HTML
        from prompt_toolkit.key_binding import KeyBindings
    except Exception:  # noqa: BLE001——缺依赖时静默退化
        return None
    bindings = KeyBindings()

    @bindings.add("f2")
    def _show_report(event) -> None:
        event.app.exit(result="\\report")

    @bindings.add("f3")
    def _show_todo(event) -> None:
        event.app.exit(result="\\todo")

    return PromptSession(
        message=HTML("<ansibrightcyan><b>你&gt; </b></ansibrightcyan>"),
        bottom_toolbar=lambda: _toolbar_text(task),
        key_bindings=bindings,
        input=input,
        output=output,
    )

def _read_input(session=None) -> str:
    """读取一行用户输入。

    交互终端用 prompt_toolkit：它按显示宽度（wcwidth）处理光标，
    中文/emoji 的退格编辑不会错位；session 复用同一条输入历史
    （上箭头翻历史），并在输入行下方常驻底栏。管道/重定向等非终端
    场景退化为普通 input()。
    """
    if session is None:
        return input(ui.user_prompt()) if sys.stdin.isatty() else input()
    try:
        from prompt_toolkit.patch_stdout import patch_stdout

        # patch_stdout：任何系统侧打印（后台任务收尾提示等）都在
        # 用户半行输入的下方干净重绘，不打碎输入行
        with patch_stdout():
            return session.prompt()
    except KeyboardInterrupt:
        raise
    except Exception:  # noqa: BLE001——prompt_toolkit 不可用时退回 input()
        return input(ui.user_prompt())

def _local_command(command: str, task: Task, agent=None) -> None:
    """聊天输入行的本地命令（\\ 或 / 前缀）：纯本地查看与操作，零模型成本。

    这是人机协同"随时剥开"的入口——看后台、看子任务不必等主 agent
    转达，也不花一分钱。前缀消息永远不会作为对话发给模型。页面内
    Ctrl+C 等于返回，不退出会话。
    """
    parts = command[1:].strip().split()
    try:
        if not parts:
            print(_LOCAL_HELP)
            return
        cmd = parts[0].lower()
        if cmd in ("help", "h", "?", "帮助"):
            print(_LOCAL_HELP)
        elif cmd in ("bg", "后台"):
            args_ = parts[1:]
            if args_ and args_[0].isdigit():
                args_[0] = f"bg-{args_[0]}"  # bg 1 == bg bg-1
            if len(args_) >= 2 and args_[0].lower() == "stop":
                target = args_[1]
                if target.isdigit():
                    target = f"bg-{target}"  # bg stop 1 == bg stop bg-1
                print(stop_background(target))
            elif len(args_) == 1:
                print(check_background(args_[0]))
            else:
                print(list_background())
        elif cmd in ("report", "报告"):
            # 与 `wovra report` 同一份机械渲染：会话内直接看，不用另开终端
            print(ui.report_view(task, _child_summaries(task.id)))
        elif cmd in ("maint", "维护", "进度"):
            # 与 `wovra maint` 同一份机械渲染：会话内看整理/分裂进度
            print(ui.maint_view(task))
        elif cmd in ("todo", "阶段", "计划"):
            print("\n".join(task.todo_lines()))
        elif cmd in ("undo", "撤销"):
            if agent is None or not agent.rounds:
                print("（没有可撤销的轮次）")
                return
            last = agent.rounds[-1]
            if last.get("end_state") != "open":
                print("最后一轮已闭合（有最终回答）——只撤销开放中的轮次。")
                return
            n = len(last.get("events") or [])
            agent.rounds.pop()
            agent._persist_rounds()
            print(ui.info(f"已撤销最近一条开放轮（含 {n} 条事件；成本记录保留）。"))
        else:
            print(f"未知本地命令：{parts[0]}（help 查看全部）")
    except KeyboardInterrupt:
        # 页面内 Ctrl+C：返回对话，不退出会话
        print()

def _chat_help() -> None:
    """chat 模式内的帮助。"""
    print(ui.rule("chat 模式帮助"))
    print("直接输入文字即可对话，每轮结束自动保存到磁盘。")
    print(f"  {ui.paint('help / 帮助', 'bold')}      显示本帮助")
    print(f"  {ui.paint('report / maint / todo', 'bold')}  会话内看报告 / 整理分裂进度 / 当前阶段与工作项（快捷键 F2 / F3）")
    print(f"  {ui.paint('bg / undo / c', 'bold')}  本地命令（\\help 看全部；零模型成本）")
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
    _acquire_session_lock(task)
    try:
        # 交互模式：整理异步后台执行，不阻塞对话；退出时限时等待收尾
        mode = _resolve_mode(args.mode, task)
        agent = _build_agent(task, mode=mode, async_organization=True)

        print(ui.rule("Wovra 会话"))
        print(f"{ui.paint('任务', 'bold')}  {task.id}")
        print(f"{ui.paint('模式', 'bold')}  {mode}")
        task.record("session", f"启动（mode={mode}）")
        task.save()
        print(f"{ui.paint('目标', 'bold')}  {task.goal or '（未定，将随对话成形）'}")
        print(f"{ui.paint('状态', 'bold')}  {ui.status(task.status)}")
        print(ui.rule())

        _replay_history(task)
        print(ui.info("输入指令开始对话；\\help 看本地命令，help 查看帮助，exit 退出。\n"))

        # 输入会话建一次：跨轮复用输入历史，底栏常驻显示当前阶段/工作项
        prompt_session = _make_prompt_session(task)
        while True:
            try:
                _drain_status(agent)  # 后台整理的状态行（出现在输入行上方）
                _flush_stdin()  # 丢弃流式输出期间敲进缓冲的按键，防止误提交
                user_input = _read_input(prompt_session).strip()
            except (EOFError, KeyboardInterrupt):
                # Ctrl+C / Ctrl+D：正常离开。状态在每轮结束时就已落盘
                print(f"\n{ui.success(f'会话已保存。下次继续: {_resume_command(task)}')}")
                break
            if not user_input:
                continue
            if user_input[:1] in ("\\", "/"):
                body = user_input[1:].strip().lower()
                if body == "":
                    # 裸前缀：不猜意图，直接给完整命令列表
                    print(_LOCAL_HELP)
                    continue
                if body in ("c", "continue"):
                    # \继续：步数超限/中断后的标准恢复方式——没有新信息
                    # 就不该造一条"继续"用户消息，本地直接续上开放轮
                    has_open = agent.rounds and agent.rounds[-1].get(
                        "end_state"
                    ) in ("", "open")
                    if not has_open:
                        print(ui.info("没有可继续的开放轮。"))
                        continue
                    try:
                        answer = _run_turn(agent, None)
                    except KeyboardInterrupt:
                        # 与正常轮一致：中断保持开放，回到输入行
                        print(
                            ui.info("本轮已中断，进度已保存（轮未闭合）。\\c 可接着干。")
                        )
                        continue
                    if answer:
                        print()
                    continue
                # 本地命令：前缀保留给终端本身——不进模型、零模型成本
                _local_command(user_input, task, agent)
                continue
            if user_input.lower() in ("exit", "quit", "退出"):
                print(ui.success(f"会话已保存。下次继续: {_resume_command(task)}"))
                break
            if user_input.lower() in ("help", "帮助"):
                _chat_help()
                continue

            try:
                answer = _run_turn(agent, user_input)
            except KeyboardInterrupt:
                # Ctrl+C 中断本轮：轮已保持开放并记账，回到输入行继续。
                # 打断的是"这一步"，不是整个会话；再次 Ctrl+C 走正常退出
                print(
                    f"\n{ui.info('本轮已中断，进度已保存（轮未闭合）。继续输入可接着干，再次 Ctrl+C 退出。')}"
                )
                continue
            if answer:
                print()

        # 退出前等待后台整理收尾（最多 10 秒）；没赶上的轮次留在
        # 整理水位里，下次触发时批量整理（V3 水位机制，不逐轮补跑）
        if not agent.flush_organization(timeout=10.0):
            print(ui.info("仍有批量整理未完成；相关轮次已计入整理水位，下次触发时一并整理。"))

        # 收尾期完成的整理/压缩成本补记（共用 helper）
        _record_leftover_maintenance(agent, task, "会话退出收尾补记")
    finally:
        # 会话退出：该会话启动的后台进程一并关闭（keep_alive 常驻任务除外）
        stopped = stop_session_backgrounds()
        if stopped:
            print(ui.info(f"已停止本会话启动的 {stopped} 个后台任务。"))
        _release_session_lock(task)

_LOCAL_HELP = """\
本地命令（\\ 或 / 前缀，纯本地执行：零模型成本、不用等轮次结束）：
  c / continue       续上开放轮（步数用尽/中断后接着干，不注入新消息）
  （只输 \\ 或 / 也可以：直接显示这份命令列表）
  bg                  后台进程列表
  bg <任务id>          查看某后台进程的增量输出
  bg stop <任务id>     强制停止后台进程
  report              会话内看人视图报告（同 `wovra report`；快捷键 F2）
  maint               会话内看整理/分裂进度（同 `wovra maint`）
  todo                会话内看当前阶段与工作项（快捷键 F3）
  undo                撤销最近一条开放轮（打错字/误发送的后悔药）
  help                本帮助
任务 id 可只写末尾短串或 bg 编号（如 bg 1）。
Ctrl+C：页面内 = 返回对话；输入行 = 退出会话。"""
