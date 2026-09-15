"""后台任务：启动、列举、增量查看、停止（按会话归属管理）。

控制句柄仅存活于本进程——Wovra 退出后进程仍在运行，日志保留在
.wovra-background/ 下。`list_background` 归此模块（读后台注册表）。
"""

import itertools
import os
import re
import subprocess
from pathlib import Path

from . import limits, safety
from .shell import _decode_output, _kill_process_tree


def list_background() -> str:
    """列出本会话启动的所有后台任务及其状态。"""
    if not _BACKGROUND_TASKS:
        return "当前没有后台任务。"
    lines = []
    for tid, entry in _BACKGROUND_TASKS.items():
        running = entry["proc"].poll() is None
        state = "运行中" if running else f"已退出（exit_code={entry['proc'].returncode}）"
        owner = entry.get("session") or "无主"
        alive = " [常驻]" if entry.get("keep_alive") else ""
        lines.append(f"{tid}  [{owner}] {state}{alive}  {entry['command'][:60]}")
    return "后台任务：\n" + "\n".join(lines)

# ---- 后台任务 ----------------------------------------------------------------
# 长驻进程（开发服务器、watcher、长安装）的后台运行与控制：启动即返回，
# 输出落日志文件，增量查看，整树强杀。控制句柄仅存活于本进程——
# Wovra 退出后进程仍在运行，日志保留在 .wovra-background/ 下。

_BACKGROUND_TASKS: dict[str, dict] = {}
_BACKGROUND_SEQ = itertools.count(1)
_CURRENT_SESSION: str | None = None


def set_current_session(task_id: str | None) -> None:
    """标记当前会话：后台任务按会话归属，防止跨会话误管。"""
    global _CURRENT_SESSION
    _CURRENT_SESSION = task_id


def _log_dir() -> Path:
    """后台日志目录（`.wovra-background/`）——**现算**，不 import 期定死。

    2026-09-15：工作区改成线程绑定后，"进程启动目录"不再等于"干活的工作区"
    （serve 里每个轮/预览各自绑自己的会话）。import 期算出的常量会把日志
    写到启动目录去，故改成每次现取有效工作区。
    """
    return safety.workspace_root() / ".wovra-background"


def run_background(command: str, keep_alive: bool = False) -> str:
    """后台启动一条 shell 命令（服务器、监听、长安装等），立即返回任务 ID。

    输出写入日志文件；用 check_background 查看增量输出，
    stop_background 停止（整树强杀）。黑名单与 run_command 相同。
    任务归属启动它的会话：会话退出时默认一并关闭——需要跨会话存活的
    常驻进程（如长期开发服务器）设 keep_alive=True。
    """
    safety._audit(f"[run_background] {command}")
    for pattern in safety._DENIED_PATTERNS:
        if pattern in command:
            return (
                f"已拒绝执行危险命令：包含被禁止的模式 `{pattern}`。"
                f"如需完成类似效果，请使用更安全的替代方案。"
            )
    # 禁写区（tasks/ 只读，不可授权）——与 run_command 同一条判定（2026-09-15）
    readonly_denied = safety.readonly_command_denial(command)
    if readonly_denied:
        safety._audit(f"[run_background][禁写区拒绝] {command}")
        return readonly_denied
    escape = safety._command_escape_targets(command)
    if escape:
        reason, targets = escape
        if targets and safety._request_path_authorization(targets, "run_background"):
            safety._audit(f"[run_background][越界已授权] {command}")
        else:
            return (
                f"已拒绝执行：命令试图{reason}（{command[:120]}）。"
                f"后台命令同样默认限定在工作区 {safety.workspace_root()} 内运行；"
                f"越界访问需用户授权一次（授权后自动放行）。"
            )
    reason = safety._confirm_reason(command)
    if reason and not safety._ask_yes_no(
        f"后台命令包含敏感操作（命中 `{reason}`），是否允许启动？\n  {command[:200]}"
        f"{safety._confirm_hint(command)}"
    ):
        safety._audit(f"[run_background][用户拒绝] {command}")
        return (
            "用户拒绝了该命令的启动。请换一种无副作用的做法，"
            "或向用户说明为什么需要它。"
        )
    task_id, proc = _launch_background(command, keep_alive=keep_alive)
    tag = "（常驻，会话退出后继续运行）" if keep_alive else ""
    return (
        f"后台任务 {task_id} 已启动（PID {proc.pid}）{tag}：{command[:120]}\n"
        f'查看输出: check_background(task_id="{task_id}") | '
        f'停止: stop_background(task_id="{task_id}")'
    )


def _launch_background(command: str, keep_alive: bool) -> tuple[str, subprocess.Popen]:
    """后台启动一条 shell 命令并登记，返回 (task_id, proc)。

    输出重定向到日志文件（增量查看用）；进程自成进程组，退出时整树强杀。
    """
    log_dir = _log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    task_id = f"bg-{next(_BACKGROUND_SEQ)}"
    log_path = log_dir / f"{task_id}.log"
    env = dict(os.environ, PYTHONUTF8="1")
    # 同 run_command：后台子进程显式声明非交互（worklog-20260911.md §7）
    env[safety.NONINTERACTIVE_ENV] = "1"
    with open(log_path, "wb") as log_file:
        # stdin 接 DEVNULL（同 run_command，见 worklog-20260911.md §5-P2）：
        # 后台命令不该悬在"用户看不见的确认提示"上等输入——那就成了
        # 启动即假死、且等待期按键可能被误当授权。
        proc = subprocess.Popen(
            command,
            shell=True,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            cwd=safety.workspace_root(),  # 固定工作目录：相对路径都在项目内
            env=env,
            start_new_session=os.name != "nt",  # 与 run_command 同一套树杀约定
            # Windows：新进程组免疫 Ctrl+C——用户中断主会话不能带走
            # 后台子任务（实测 0xC000013A 全军覆没的教训）
            creationflags=(
                subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
            ),
        )
    _BACKGROUND_TASKS[task_id] = {
        "proc": proc, "log": log_path, "pos": 0,
        "command": command, "session": _CURRENT_SESSION, "keep_alive": keep_alive,
    }
    return task_id, proc


def check_background(task_id: str) -> str:
    """查看后台任务的运行状态与增量输出（自上次查看以来的新增部分）。"""
    entry = _BACKGROUND_TASKS.get(task_id)
    if entry is None:
        return f"未找到后台任务: {task_id}（控制句柄仅在本会话内有效）。"
    if (error := _ownership_error(task_id, entry)):
        return error
    proc = entry["proc"]
    running = proc.poll() is None
    new_text = ""
    if entry["log"].exists():
        with open(entry["log"], "rb") as f:
            f.seek(entry["pos"])
            data = f.read()
        entry["pos"] += len(data)
        new_text = _decode_output(data)
    status = "运行中" if running else f"已退出（exit_code={proc.returncode}）"
    body = new_text.strip() or "（无新输出）"
    # 增量输出默认全量返回（2026-09-11 放开，worklog §25）：原先切末 2000
    # 字符，日志开头被静默丢掉——而"什么时候开始报错的"恰恰在开头。
    # 超限时指明**日志原文**（不是副本）：它能被 read_file(pattern=...) 定位，
    # 也能被 search_files 搜——大日志不必全量加载，但随时可检索、可全取。
    try:
        log_ref = entry["log"].relative_to(safety.workspace_root()).as_posix()
    except ValueError:
        log_ref = str(entry["log"])
    note = _exit_mismatch_note(proc, running, new_text)
    return (f"[{task_id}] {status}{note}\n"
            f"{limits.clip(body, f'bg-{task_id}', source=log_ref)}")


def _exit_mismatch_note(proc: subprocess.Popen, running: bool, text: str) -> str:
    """退出码为 0 但输出像报错 → 明确标出（TOOLING_REVIEW.md §4.5）。

    实测坑：`uv add pyserial` 后台跑完 `exit_code=0`，而它内部其实报
    "No `pyproject.toml` found" —— 调用方只看返回头会以为成功，全靠主动
    check 才看到。这里不猜语义，只做两件确定性的事：退出码为 0 时若输出
    命中已知错误形态，就在头一行标 "⚠ 输出含报错字样"；日志为空也点出来。
    """
    if running:
        return ""
    if proc.returncode != 0:
        return ""
    hits = _ERRORISH_RE.findall(text or "")
    if hits:
        seen = sorted({h.strip() for h in hits})[:2]
        return f"　⚠ 退出码为 0，但输出含报错字样（{'、'.join(seen)}）——请核对日志"
    if not (text or "").strip():
        return "　（本次查看无新输出；完整日志见下方路径，或 read_file 取回）"
    return ""


# 已知"看起来像失败"的输出形态：不猜语义，只做词面命中——命中的一律标出，
# 让调用方去看日志。宁可是"其实没问题"的一次提醒，也不要静默成功。
_ERRORISH_RE = re.compile(
    r"(?:error|failed|failure|not found|no such file|traceback|exception"
    r"|cannot|unable|denied|refused|invalid|fatal)\b|"
    r"(?:错误|失败|找不到|不存在|异常|拒绝|无法)",
    re.I,
)


def _ownership_error(task_id: str, entry: dict) -> str | None:
    """跨会话管理防护：任务只归启动它的会话管。"""
    owner = entry.get("session")
    if owner and owner != _CURRENT_SESSION:
        return f"后台任务 {task_id} 由会话 {owner} 启动，请在那个会话中管理。"
    return None


def stop_session_backgrounds() -> int:
    """会话退出时停止当前会话启动的所有后台任务（keep_alive 除外）。

    返回停止的数量。进程崩溃时这些子进程会成为孤儿——日志仍在
    .wovra-background/ 下，可手动查看与清理。"""
    stopped = 0
    for tid, entry in list(_BACKGROUND_TASKS.items()):
        if entry.get("session") != _CURRENT_SESSION or entry.get("keep_alive"):
            continue
        proc = entry["proc"]
        if proc.poll() is None:
            _kill_process_tree(proc.pid)
        _BACKGROUND_TASKS.pop(tid, None)
        stopped += 1
    return stopped


def stop_background(task_id: str) -> str:
    """停止后台任务（整树强杀），返回退出状态。"""
    entry = _BACKGROUND_TASKS.get(task_id)
    if entry is None:
        return f"未找到后台任务: {task_id}（控制句柄仅在本会话内有效）。"
    if (error := _ownership_error(task_id, entry)):
        return error
    proc = entry["proc"]
    if proc.poll() is None:
        _kill_process_tree(proc.pid)
    try:
        code = proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        return f"后台任务 {task_id} 已发送终止信号但未退出，请稍后用 check_background 确认。"
    return f"后台任务 {task_id} 已停止（exit_code={code}）。"
