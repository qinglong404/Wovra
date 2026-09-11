"""终端渲染与轮次执行：_run_turn（流式回调 + 看门狗）、历史回放、状态行。
"""
import argparse
import json
import os
import shutil
import sys
import threading
import time
from pathlib import Path

from .. import task as task_module
from .. import tools as tools_module
from .. import ui
from ..agent import Agent, MODE_BASELINE, MODE_MANAGED
from ..llm import LLMConfigError
from ..task import Task
from ..tools import (
    PROJECT_ROOT,
    ask_user,
    check_background,
    delete_file,
    edit_file,
    get_current_time,
    glob_files,
    list_background,
    list_files,
    move_file,
    read_file,
    replace_lines,
    restore_file,
    run_background,
    run_command,
    search_files,
    stop_background,
    stop_session_backgrounds,
    user_input_pending,
    web_fetch,
    web_search,
    write_file,
)

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

def _drain_status(agent: Agent) -> None:
    """取走并打印后台整理管线投递的状态消息（主线程打印，线程安全）。"""
    for line in agent.drain_status():
        print(ui.status_line(line), flush=True)

def _run_turn(agent: Agent, instruction: str | None) -> str:
    """执行一轮流式对话并负责全部展示。

    instruction=None 表示 \\c：不注入新的用户消息，直接续上开放轮。

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
    think_buf = []     # 思考累积（单行滚动显示，防污染窗口/防视角切换）

    def _break_line() -> None:
        nonlocal line_open
        if line_open:
            print(flush=True)
            line_open = False

    def on_thinking(text: str) -> None:
        # 思考单行化（2026-09-08 用户拍板）：\r 原地刷新一行，不再整段
        # 滚动——防止污染窗口、防止思考/回答来回切换视角
        nonlocal phase, line_open
        if phase != "thinking":
            _break_line()
            phase = "thinking"
        think_buf.append(text)
        line = ui.thinking_line(ui.thinking_head("".join(think_buf)))
        sys.stdout.write("\r\x1b[2K" + line)
        sys.stdout.flush()
        line_open = True

    def on_answer_delta(text: str) -> None:
        nonlocal phase
        if phase != "answer":
            _break_line()
            phase = "answer"
            think_buf.clear()
            print(ui.rule("回答"), flush=True)
            ui.answer_live_start()
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
        # 秒表节奏：工具执行期间终端静默，每 10 秒提醒一次"还在干活"
        step = 10

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
        if instruction is None:
            # \继续：不注入新消息，直接续上开放轮（无新信息，不造噪音）
            answer = agent.resume(
                on_thinking=on_thinking, on_answer_delta=on_answer_delta
            )
        else:
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
        print(ui.info("本轮保持开放：\\c 直接接着干，或发新消息补充信息。"))
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
