"""会话管线：锁文件、任务解析（编号/id）、模式解析、残留维护用量记账。

锁语义：O_EXCL 创建 + 陈旧锁恢复（pid 不存活即接管），防同一会话双开。
"""
import json
import os

from .. import task as task_module
from .. import ui
from ..agent import MODE_MANAGED
from ..task import Task

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

def _child_summaries(parent_id: str) -> list[dict]:
    """子任务摘要（委托给 task.find_children，供 report/\\sub 共用）。"""
    return task_module.find_children(parent_id)
