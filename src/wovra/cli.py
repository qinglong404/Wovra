"""Wovra 的统一命令行入口。

设计动机（为什么需要 CLI）：核心层（Task/Agent）已经具备
"任务常驻磁盘、人随时介入" 的能力，但此前唯一的入口是 examples——
每个都是一次性的硬编码剧本，导致"会话"和"磁盘上的任务"对不上号。
CLI 补上这扇门：

    wovra chat                  进入交互模式，多轮对话，实时落盘
                                （不带 id 自动新建会话）
    wovra run   <id> [指令]      对既有任务执行一轮
    wovra list                  列出所有会话（带数字编号）
    wovra delete <id>           删除会话及其全部本地数据（不可恢复）

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
import os
import shutil
import sys
import threading
import time

from . import task as task_module
from . import tools as tools_module
from . import ui
from .agent import (
    MODE_BASELINE,
    MODE_MANAGED,
    Agent,
    ask_user,
    check_background,
    edit_file,
    get_current_time,
    glob_files,
    list_background,
    list_files,
    read_file,
    run_background,
    run_command,
    search_files,
    stop_background,
    web_fetch,
    web_search,
    write_file,
)
from .llm import LLMConfigError
from .task import Task
from .tools import PROJECT_ROOT, stop_session_backgrounds, user_input_pending


def _session_lock_path(task: Task):
    return task_module.TASKS_ROOT / task.id / ".lock"


def _process_alive(pid: int) -> bool:
    """跨平台探测进程是否存活（无法确认时保守视为存活）。

    Windows 不支持 os.kill(pid, 0)——直接报 WinError 87，只能走
    OpenProcess：打不开句柄且错误码为 ERROR_ACCESS_DENIED 说明
    进程存在但无权查询，同样算存活。
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        ERROR_ACCESS_DENIED = 5

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return ctypes.get_last_error() == ERROR_ACCESS_DENIED
        try:
            exit_code = wintypes.DWORD()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return exit_code.value == STILL_ACTIVE
            return True
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        pass  # 进程存在但非本人所有（PermissionError）等，保守视为存活
    return True


def _acquire_session_lock(task: Task) -> None:
    """会话锁：同一会话同一时刻只允许一个进程操作（V2 单写者假设的显式防护）。

    用 O_CREAT|O_EXCL 原子创建锁文件（写内容不能用覆盖写——那永远
    不会报"已存在"，锁就形同虚设）；锁文件记录持有者 PID，
    持锁进程已死亡时视为陈旧锁并清除。
    """
    import time as _time

    lock = _session_lock_path(task)
    for _ in range(2):
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return
        except FileExistsError:
            pass
        except OSError:
            return  # 文件系统不支持时降级为无锁
        pid = 0
        try:
            pid = int(lock.read_text(encoding="utf-8").strip() or 0)
        except Exception:  # noqa: BLE001
            pass
        # 探测持锁进程是否存活：已退出 = 陈旧锁，清除后重试；
        # 存活（含无权查询、无法排除存活的情形）= 拒绝
        if not _process_alive(pid):
            try:
                lock.unlink()  # 陈旧锁（持锁进程已退出）
                continue
            except OSError:
                pass
        else:
            raise SystemExit(
                ui.error(f"会话 {task.id} 正在另一个进程中使用（pid {pid}），请先关闭该会话。")
            )
    raise SystemExit(ui.error(f"会话 {task.id} 的锁无法获取。"))


def _release_session_lock(task: Task) -> None:
    try:
        _session_lock_path(task).unlink()
    except OSError:
        pass


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


def _read_input() -> str:
    """读取一行用户输入。

    交互终端用 prompt_toolkit：它按显示宽度（wcwidth）处理光标，
    中文/emoji 的退格编辑不会错位；同时自带输入历史（上箭头翻历史）。
    管道/重定向等非终端场景退化为普通 input()。
    """
    if not sys.stdin.isatty():
        return input()
    try:
        from prompt_toolkit import prompt
        from prompt_toolkit.formatted_text import HTML
        from prompt_toolkit.patch_stdout import patch_stdout

        # patch_stdout：任何系统侧打印（后台任务收尾提示等）都在
        # 用户半行输入的下方干净重绘，不打碎输入行
        with patch_stdout():
            return prompt(HTML("<ansibrightcyan><b>你&gt; </b></ansibrightcyan>"))
    except KeyboardInterrupt:
        raise
    except Exception:  # noqa: BLE001——prompt_toolkit 不可用时退回 input()
        return input(ui.user_prompt())


def _system_prompt(mode: str) -> str:
    """按模式生成系统提示词：只写已实现的能力，并附带当前运行环境。

    写实约束：模型会把提示词当成"已有能力清单"，写了没实现的功能
    它就会真的去调用——所以每个模式只描述自己实际的行为。
    环境信息（OS/Shell）防止在 Windows 上跑类 Unix 命令
    （ls/rm/apt），白白浪费一步工具调用。
    """
    import platform

    # 按 platform.system() 分流而不是 os.name：macOS 和 Linux 同属
    # posix，os.name 分不出来——模型若把 macOS 当 Linux，会用上
    # apt/systemctl 这类 Linux 专属命令，白白浪费一步工具调用
    system = platform.system()
    if system == "Windows":
        env = (
            f"当前环境：Windows {platform.release()}，shell 是 cmd.exe——"
            "命令用 Windows 语法（dir/copy/del/start），没有 ls/rm/grep/apt。"
        )
    elif system == "Darwin":
        env = (
            f"当前环境：macOS（Darwin {platform.release()}），shell 是 "
            "zsh/Bash——命令用 Unix 语法，但它是 BSD 底层，没有 apt/"
            "systemctl，装软件用 brew。"
        )
    else:
        env = "当前环境：Linux，shell 是 POSIX sh。"

    common = (
        f"你是 Wovra 的执行助手。工作区：{PROJECT_ROOT}——所有文件工具与"
        "命令都限定并执行于此目录内。会话持久化在磁盘上：每轮结束自动"
        "保存，用户可随时退出、下次接着做。需要时使用工具获取真实信息或"
        "完成任务：找内容用 search_files，按文件名找文件用 glob_files，"
        "读大文件用 read_file 的 start_line 分段；可以创建、修改项目内的"
        "文件，运行安全的 shell 命令。查技术资料用 web_search，抓取已知"
        "网页用 web_fetch；需求或细节有分歧时用 ask_user 向用户确认。"
        "回答保持简洁。"
        "环境配置可直接用 uv / pip / conda 等命令（如 uv sync、pip install）；"
        "敏感操作（安装、提交、删除等）会先请求用户确认——被拒绝时换一种"
        "做法，不要重试原命令；耗时长的安装或服务器进程用 run_background "
        "后台执行，check_background 查看输出。"
    )
    if mode == MODE_MANAGED:
        extra = (
            "上下文由 Runtime 分层管理：最近几轮全量保留，更早的轮次被"
            "降档成一行索引；需要更早轮次的细节时，用 expand_history 工具"
            "按 ID（如 R3 或 R3-E02）展开，不要凭记忆猜测。"
        )
    else:
        extra = (
            "上下文为全量回放，接近窗口上限时较早轮次会自动压缩成摘要"
            "（baseline 对照模式，行为与常见 Agent 一致）。"
        )
    return f"{common}{extra}{env}"


def _build_agent(task: Task, mode: str = MODE_MANAGED, async_organization: bool = False) -> Agent:
    """为任务构造一个带默认工具集的 Agent（展示回调在 _run_turn 注入）。

    工具分两类：只读（时间/列目录/读文件/搜索）与变更类
    （写文件/改文件/执行命令，均有审计记录与破坏性防护，见 tools.py）。
    mode 决定上下文策略：managed（分层上下文，默认）或
    baseline（全量回放 + 阈值压缩的对照组）。
    async_organization：整理是否后台异步执行（chat 模式开，
    run 模式关——一次性进程退出前必须同步完成）。
    """
    return Agent(
        system_prompt=_system_prompt(mode),
        tools=[
            get_current_time,
            list_files,
            glob_files,
            read_file,
            search_files,
            write_file,
            edit_file,
            run_command,
            run_background,
            check_background,
            stop_background,
            list_background,
            web_fetch,
            web_search,
            ask_user,
        ],
        task=task,
        context_mode=mode,
        async_organization=async_organization,
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


def _resolve_mode(args_mode: str | None, task: Task) -> str:
    """模式解析：显式 --mode > 会话记录的 > 默认 managed。

    结果写回会话——baseline 会话恢复时自动沿用 baseline，实验数据
    不会因忘记带 --mode 而串味。"""
    mode = args_mode or task.mode or MODE_MANAGED
    if task.mode != mode:
        task.mode = mode
        task.save()
    return mode


def _resume_command(task: Task) -> str:
    """续用命令：baseline 会话带上 --mode，避免恢复时静默切回 managed。"""
    command = f"wovra chat {task.id}"
    if task.mode and task.mode != MODE_MANAGED:
        command += f" --mode {task.mode}"
    return command


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
    events = dialogue[-last_n:]
    i = 0
    while i < len(events):
        event = events[i]
        kind = event["kind"]
        if kind == "user_input":
            print(ui.user(event["detail"]))
        elif kind == "final_answer":
            ui.assistant_markdown(event["detail"])
        elif kind == "tool_call":
            # 与相邻结果配对成一行：调用了什么、成没成。失败只留
            # 首行原因，不漏 stdout 原文（细节在 task.json 可查）
            name = _split_call(event["detail"])
            nxt = events[i + 1] if i + 1 < len(events) else None
            if nxt is not None and nxt["kind"] == "tool_result":
                result_detail = nxt["detail"].partition(" -> ")[2]
                print(ui.tool_pair(name, result_detail))
                i += 2
                continue
            print(ui.tool_call(name))
        else:  # 孤立的 tool_result（配对窗口切在中间）
            print(ui.tool_result(event["detail"].partition(" -> ")[2]))
        i += 1
    if len(dialogue) > last_n:
        print(ui.info(f"（仅显示最近 {last_n} 条，完整记录见 report.md）"))
    print(ui.rule())


def _split_call(detail: str) -> str:
    """从 "tool_name({...})" 形式的工具调用 detail 里取工具名。"""
    return detail.partition("(")[0]


# ---- 子命令实现 -----------------------------------------------------------


def _drain_status(agent: Agent) -> None:
    """取走并打印后台整理管线投递的状态消息（主线程打印，线程安全）。"""
    for line in agent.drain_status():
        print(ui.status_line(line), flush=True)


def _run_turn(agent: Agent, instruction: str) -> str:
    """执行一轮流式对话并负责全部展示。

    行纪律（解决"思考与回答混在一起、事件行粘连"的问题）：
    * line_open 记录当前终端行是否被流式输出占着——任何事件行
      （工具调用/结果）打印前，先补一个换行把流式行断开；
    * 每一次 LLM 调用都可能重新进入思考阶段，所以思考横幅按
      "进入思考" 事件打印，而不是整个 Turn 只打印一次；
    * 工具行只写"调用了什么 + 成功/失败"，不展开参数与输出；
    * 后台整理的状态不实时打印（后台线程打印会打碎输入行），
      由 _drain_status 在安全时机统一显示。

    异常/中断时收尾当前 Round（保持开放），保证历史与状态一致。
    """
    line_open = False  # 流式输出（思考/回答）是否有未换行的半行
    phase = ""         # 当前流式阶段：thinking / answer

    def _break_line() -> None:
        nonlocal line_open
        if line_open:
            print(flush=True)
            line_open = False

    def on_thinking(text: str) -> None:
        nonlocal line_open, phase
        if phase != "thinking":
            _break_line()
            print(ui.rule("思考过程"), flush=True)
            phase = "thinking"
        print(ui.thinking_delta(text), end="", flush=True)
        line_open = True

    def on_answer_delta(text: str) -> None:
        nonlocal phase
        if phase != "answer":
            _break_line()
            print(ui.rule("回答"), flush=True)
            ui.answer_live_start()
            phase = "answer"
        # TTY 下进 rich Live 实时渲染 Markdown（与回放观感一致）；
        # 非 TTY 自动退化为纯文本流
        ui.answer_live_append(text)

    # 工具执行看门狗：超过一步工具的等待里终端完全静默，用户分不清
    # "在干活"和"卡死"。执行窗口内主线程阻塞在工具里、流式输出和
    # 输入行都不活跃，这个看门狗线程是唯一安全的打印者
    tool_watch_stop = threading.Event()
    tool_watch_thread: threading.Thread | None = None
    # 最近一次进展行的时间（on_progress 与看门狗线程共享；float 赋值
    # 在 GIL 下原子）。子任务有进展行时它本身就是心跳，秒表闭嘴
    last_progress_at = [0.0]

    def _start_tool_watch(name: str) -> None:
        nonlocal tool_watch_thread
        tool_watch_stop.clear()

        def _tick() -> None:
            waited = 0
            while not tool_watch_stop.wait(step):
                # 工具在等用户确认/回答：思考时间不算执行时长，
                # 也不打印——否则会追尾在确认提示行上
                if user_input_pending():
                    continue
                waited += step
                print(ui.wait_hint(f"{name} 已执行 {waited} 秒…"), flush=True)

        tool_watch_thread = threading.Thread(target=_tick, daemon=True)
        tool_watch_thread.start()

    def _stop_tool_watch() -> None:
        tool_watch_stop.set()
        if tool_watch_thread is not None:
            tool_watch_thread.join(timeout=1)  # 等待中的 tick 自然退出，不会补打

    def on_tool_call(name: str, arguments: str) -> None:
        nonlocal line_open, phase
        ui.answer_live_stop()  # 停掉回答渲染，交出终端输出权
        _break_line()
        phase = ""
        print(ui.tool_call(name), flush=True)
        _start_tool_watch(name)

    def on_tool_result(name: str, result: str) -> None:
        nonlocal line_open, phase
        _stop_tool_watch()
        _break_line()
        phase = ""
        print(ui.tool_result(result), flush=True)

    def on_progress(text: str) -> None:
        # 同步回调（主线程执行）：模型响应等待中、工具动作进行中的即时提示。
        # "正在写入文件中…"在模型刚报出工具名时就显示，不等参数输完
        nonlocal line_open, phase
        last_progress_at[0] = time.monotonic()
        ui.answer_live_stop()
        _break_line()
        phase = ""
        print(ui.wait_hint(text), flush=True)

    agent.on_tool_call = on_tool_call
    agent.on_tool_result = on_tool_result
    agent.on_progress = on_progress

    try:
        answer = agent.run(
            instruction, on_thinking=on_thinking, on_answer_delta=on_answer_delta
        )
    except KeyboardInterrupt:
        ui.answer_live_stop()
        agent.finalize_round("open")  # 中断不闭合轮次，事件并入开放轮
        raise
    except LLMConfigError:
        ui.answer_live_stop()
        # 配置错误（端点/模型/密钥）在会话内修不了：收尾轮次后原样上抛，
        # 由 main 统一打印提示并退出。绝不能落进下面的 RuntimeError 分支——
        # 那会把"配置错了"误报成"步数超限，继续对话即可"
        agent.finalize_round("open")
        raise
    except RuntimeError as error:
        ui.answer_live_stop()
        # 步数超限：不是故障，是"本轮干了很多活还没干完"——
        # 轮次保持开放，用户继续对话即可接着干
        agent.finalize_round("open")
        _break_line()
        print(ui.error(str(error)))
        print(ui.info("本轮保持开放，直接继续对话即可接着干。"))
        _drain_status(agent)
        print(ui.usage_line(agent.last_stats, maint=agent.last_maint,
                            context=agent.last_context_estimate,
                            window=agent.context_limit))
        return ""
    except Exception:
        ui.answer_live_stop()
        agent.finalize_round("open")  # 其他异常同理；失败尝试并入本轮
        raise
    ui.answer_live_stop()  # 正常结束：收掉 Live 渲染（异常路径在各分支已收）
    _break_line()
    _drain_status(agent)  # 后台整理/压缩的完成消息，排在成本行之前
    print(ui.usage_line(agent.last_stats, maint=agent.last_maint,
                        context=agent.last_context_estimate,
                        window=agent.context_limit))
    return answer


def _record_leftover_maintenance(agent, task, note: str) -> None:
    """收尾期完成的整理/压缩成本补记。

    最后一次 usage 记账发生在最终回答时刻，之后的维护调用（异步整理、
    收尾补整理）不补记就会漏掉——管理机制自身也要被完整审计。
    """
    leftover = agent.drain_maintenance_usage()
    if leftover["organization"]["total"] or leftover["compaction"]["total"]:
        task.record(
            "usage",
            f"[{agent.context_mode}] org={leftover['organization']['total']:,} "
            f"compaction={leftover['compaction']['total']:,}（{note}）",
        )
        task.save()


def cmd_run(args: argparse.Namespace) -> None:
    """wovra run：对既有任务执行一轮。

    不给指令时，让模型根据任务状态和最近事件自主决定下一步——
    这是"长时任务自主推进"的雏形。
    """
    task = _load_task(args.task_id)
    _acquire_session_lock(task)
    try:
        # 一次性进程：整理同步执行，退出前结果必须落盘。
        # 指令优先级：命令行 > 父任务派发时写入的 pending_instruction
        #（子任务异步派发的决策回传通道）> 自主推进
        mode = _resolve_mode(args.mode, task)
        agent = _build_agent(task, mode=mode, async_organization=False)
        instruction = args.instruction or task.pending_instruction.strip() or (
            "请根据任务状态和最近事件，自主决定下一步并继续推进。"
            "如果任务已无法推进，说明原因。"
        )
        if task.pending_instruction:
            task.pending_instruction = ""
            task.save()
        _run_turn(agent, instruction)
        # 一次性进程收尾：水位未触发的剩余未整理轮补一批整理——
        # TaskState 跟上进度，下一次自主推进才有准确的"任务状态"
        agent.organize_backlog()
        _record_leftover_maintenance(agent, task, "收尾补整理记账")
    finally:
        # 一次性进程：会话结束，其后台任务一并收掉
        stop_session_backgrounds()
        _release_session_lock(task)


def _child_summaries(parent_id: str) -> list[dict]:
    """子任务摘要（委托给 task.find_children，供 report/\\sub 共用）。"""
    return task_module.find_children(parent_id)


def cmd_report(args: argparse.Namespace) -> None:
    """wovra report：人机协同报告（TaskState 机械渲染，零模型成本）。

    机械渲染 TaskState 与轮次——零模型成本，随时可看。大局默认，
    细节对主 agent 说"展开 R{k}-E{nn}"按事件 ID 剥开。
    """
    task = _load_task(args.task_id)
    print(ui.report_view(task, _child_summaries(task.id)))


_LOCAL_HELP = """\
本地命令（\\ 或 / 前缀，纯本地执行：零模型成本、不用等轮次结束）：
  bg                  后台进程列表
  bg <任务id>          查看某后台进程的增量输出
  bg stop <任务id>     强制停止后台进程
  undo                撤销最近一条开放轮（打错字/误发送的后悔药）
  help                本帮助
任务 id 可只写末尾短串或 bg 编号（如 bg 1）。
Ctrl+C：页面内 = 返回对话；输入行 = 退出会话。"""


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
                print(stop_background(args_[1]))
            elif len(args_) == 1:
                print(check_background(args_[0]))
            else:
                print(list_background())
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
    print(f"  {ui.paint('bg / undo', 'bold')}  本地命令（\\help 看全部；零模型成本）")
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

        while True:
            try:
                _drain_status(agent)  # 后台整理的状态行（出现在输入行上方）
                _flush_stdin()  # 丢弃流式输出期间敲进缓冲的按键，防止误提交
                user_input = _read_input().strip()
            except (EOFError, KeyboardInterrupt):
                # Ctrl+C / Ctrl+D：正常离开。状态在每轮结束时就已落盘
                print(f"\n{ui.success(f'会话已保存。下次继续: {_resume_command(task)}')}")
                break
            if not user_input:
                continue
            if user_input[:1] in ("\\", "/"):
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


def cmd_list(args: argparse.Namespace) -> None:
    """wovra list：列出所有任务（按更新时间倒序，带数字编号）。"""
    tasks = _all_tasks()
    if not tasks:
        print(ui.info("还没有任何任务。直接 `wovra chat` 开始第一个会话。"))
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


def cmd_delete(args: argparse.Namespace) -> None:
    """wovra delete：删除一个会话及其全部本地数据（不可恢复）。

    支持 list 里的编号或完整 id。正被另一个进程持锁使用的会话
    拒绝删除——否则那个进程的每一步落盘都会把半份数据写回来；
    陈旧锁随目录一起清掉。删除前确认，脚本化使用加 --force。
    """
    task_id = _resolve_task_id(args.task_id)
    directory = task_module.TASKS_ROOT / task_id
    if not directory.exists():
        raise SystemExit(
            ui.error(f"任务不存在: {args.task_id}（用 `wovra list` 查看现有任务）")
        )

    # 展示字段尽力而为地读：半途损坏的 task.json 也是合法的删除对象
    try:
        data = json.loads((directory / "task.json").read_text(encoding="utf-8"))
        goal = data.get("goal") or "（目标待明确）"
        updated = data.get("updated_at", "").replace("T", " ")[:16]
    except (OSError, ValueError):
        goal, updated = "（task.json 损坏或缺失）", "未知"

    lock = directory / ".lock"
    if lock.exists():
        pid = 0
        try:
            pid = int(lock.read_text(encoding="utf-8").strip() or 0)
        except (OSError, ValueError):
            pass
        # 活进程持锁 → 拒绝；读不出的锁视为陈旧，随目录一起删掉
        if _process_alive(pid):
            raise SystemExit(
                ui.error(
                    f"会话 {task_id} 正在另一个进程中使用（pid {pid}），"
                    "请先关闭该会话再删除。"
                )
            )

    if not args.force:
        try:
            answer = input(
                f"确认删除会话 {task_id}（目标：{goal}，更新于 {updated}）？"
                "删除后不可恢复 [y/N] "
            )
        except EOFError:
            print(ui.info("无交互输入可用，已取消。确定删除请加 --force。"))
            return
        if answer.strip().lower() not in ("y", "yes"):
            print(ui.info("已取消，未删除。"))
            return

    shutil.rmtree(directory)
    print(ui.success(f"已删除会话: {task_id}"))


# ---- 参数解析与入口 ---------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="wovra",
        description=ui.paint("Wovra：面向结构化、长时运行 AI 工作的运行时。", "bold"),
        epilog=(
            "示例：\n"
            "  wovra chat                  开一个新会话直接聊，目标随对话成形\n"
            "  wovra list                  查看所有会话（带编号）\n"
            "  wovra chat 1                用编号续上某个会话\n"
            "  wovra delete 1              删除某个会话（不可恢复）\n"
            "\n"
            "<id> 位置既可用编号（list 里的 1、2、3…），也可用完整任务 id。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command")

    p_run = sub.add_parser("run", help="对任务执行一轮")
    p_run.add_argument("task_id", help="任务编号或完整任务 id")
    p_run.add_argument("instruction", nargs="?", help="本轮指令；省略则由 AI 自主继续")
    p_run.add_argument("--mode", choices=[MODE_MANAGED, MODE_BASELINE], default=None,
                       help="上下文策略：省略则沿用会话记录（无记录则 managed）")
    p_run.set_defaults(func=cmd_run)

    p_chat = sub.add_parser("chat", help="交互式多轮对话（不带 id 则新建会话）")
    p_chat.add_argument("task_id", nargs="?", default="", help="任务编号或完整任务 id；省略则新建")
    p_chat.add_argument("--mode", choices=[MODE_MANAGED, MODE_BASELINE], default=None,
                        help="上下文策略：省略则沿用会话记录（无记录则 managed）")
    p_chat.set_defaults(func=cmd_chat)

    p_list = sub.add_parser("list", help="列出所有任务（带编号）")
    p_list.set_defaults(func=cmd_list)

    p_delete = sub.add_parser("delete", help="删除会话及其全部本地数据（不可恢复）")
    p_delete.add_argument("task_id", help="任务编号或完整任务 id")
    p_delete.add_argument(
        "-f", "--force", action="store_true", help="跳过确认提示（脚本化使用）"
    )
    p_delete.set_defaults(func=cmd_delete)

    p_report = sub.add_parser("report", help="人机协同报告（机械渲染，零模型成本）")
    p_report.add_argument("task_id", help="任务 id 或列表编号")
    p_report.set_defaults(func=cmd_report)

    sub.add_parser("help", help="显示帮助")

    args = parser.parse_args(argv)

    # 裸 `wovra` / `wovra help`：打印帮助而不是报错
    if args.command is None or args.command == "help":
        parser.print_help()
        return

    try:
        args.func(args)
    except LLMConfigError as error:
        # 配置错误用户可自行修复：一条人话 + 指向 .env 的检查项，
        # 不打 traceback（服务端原始信息已附在提示里）
        print(ui.error(str(error)))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
