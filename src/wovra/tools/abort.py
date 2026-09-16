"""协作式中断：让**正在阻塞的工具**也能响应"停止本轮"（2026-09-16）。

背景（用户实测）：web 前端的停止按钮只是把 `job["cancel"]` 置真，而 agent 只在
**步骤之间**查它（`cancel_check`）；一条 `run_command` 卡在里面时没人看这个标记，
于是界面一直显示"停止中"，非得等命令自己跑完（默认 60s、最长 600s）才停。

做法：工具层拿一个**按调用作用域绑定**的检查函数（thread-local，与
`safety` 的工作区绑定、`permissions.guard_scope` 是同一套思路），阻塞等待的工具
在自己的轮询里问它；拿到 True 就收摊（如整树强杀子进程）。

边界（刻意如此）：
* 只管**当前这一步**——不碰后台任务（那是 `\\bg stop` 的活，它们是独立的），
  也不动别的线程/别的会话。
* 没绑检查函数时一律返回 False：脚本、测试、直接调工具的路径照旧无感。
"""

import threading
from contextlib import contextmanager


_ABORT_CHECK = threading.local()


def bind_abort(check) -> None:
    """给**当前线程**绑一个检查函数（`callable() -> bool`）。"""
    _ABORT_CHECK.check = check


def unbind_abort() -> None:
    _ABORT_CHECK.check = None


def abort_requested() -> bool:
    """当前这一步是否已被要求停止（没绑检查函数时恒为 False）。"""
    check = getattr(_ABORT_CHECK, "check", None)
    if check is None:
        return False
    try:
        return bool(check())
    except Exception:  # noqa: BLE001——检查函数本身出错不该让工具崩掉
        return False


@contextmanager
def abort_scope(check):
    """在 `with` 块内生效；出栈还原（嵌套时还原到外层的检查函数）。"""
    previous = getattr(_ABORT_CHECK, "check", None)
    _ABORT_CHECK.check = check
    try:
        yield
    finally:
        _ABORT_CHECK.check = previous
