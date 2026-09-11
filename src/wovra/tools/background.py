"""后台任务：启动、列举、增量查看、停止（按会话归属管理）。

控制句柄仅存活于本进程——Wovra 退出后进程仍在运行，日志保留在
.wovra-background/ 下。`list_background` 归此模块（读后台注册表）。
"""

import itertools
import os
import subprocess

from . import safety
from .shell import _kill_process_tree


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
_BACKGROUND_LOG_DIR = safety.PROJECT_ROOT / ".wovra-background"


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
    escape = safety._command_escape(command)
    if escape:
        return (
            f"已拒绝执行：命令试图{escape}（{command[:120]}）。"
            f"后台命令同样限定在工作区 {safety.PROJECT_ROOT} 内运行。"
        )
    reason = safety._confirm_reason(command)
    if reason and not safety._ask_yes_no(
        f"后台命令包含敏感操作（命中 `{reason}`），是否允许启动？\n  {command[:200]}"
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
    _BACKGROUND_LOG_DIR.mkdir(parents=True, exist_ok=True)
    task_id = f"bg-{next(_BACKGROUND_SEQ)}"
    log_path = _BACKGROUND_LOG_DIR / f"{task_id}.log"
    env = dict(os.environ, PYTHONUTF8="1")
    with open(log_path, "wb") as log_file:
        proc = subprocess.Popen(
            command,
            shell=True,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            cwd=safety.PROJECT_ROOT,  # 固定工作目录：相对路径都在项目内
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
        new_text = data.decode("utf-8", errors="replace")
    status = "运行中" if running else f"已退出（exit_code={proc.returncode}）"
    body = new_text.strip() or "（无新输出）"
    return f"[{task_id}] {status}\n{body[-2000:]}"


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
