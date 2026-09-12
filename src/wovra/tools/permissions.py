"""文件权限守卫（**工具层强制**）：自己的文件全权，别人的文件只读。

口径（2026-09-12 用户拍板，worklog §61）：

```text
P1 自己的文件（清单内）：读 / 写 / 改 / 删
P2 别人的文件：**只读**（改 / 删 / 移出同禁）
P3 不存在"未认领文件"——真出现就是分裂/整理的缺陷（停 + 报错），不是正常态
P4 分裂之前（注册表里只有主 agent）：主 agent 全权
P5 分裂之后：主 agent 权限**与子 agent 相同**（只有自己清单内的可写改删）
P6 越权 → 硬拒，报错文本**直接指路**（route_to 转给它 / consult 问它）
F5 新文件：谁创建谁拥有（创建成功即写进创建者的清单）
```

为什么放在工具层而不是提示词：提示词只是纪律，模型可以不听；**权限必须是
硬事实**——"干不干看有没有改写删权"（用户口径），这条只有工具层能保证。

守卫由运行时绑定（`agent/core._file_guard`），因为它需要"当前是谁在干活"
（轮上的 `active_view`）与注册表；工具层自己不认识任务。
"""

from __future__ import annotations

import contextlib
from typing import Callable, Iterator, Optional

from . import safety

# (op, path) → 拒绝理由；返回 None = 允许。op ∈ {"read","write","edit","delete","move"}
_GUARD: Optional[Callable[[str, str], Optional[str]]] = None


def set_file_guard(guard: Optional[Callable[[str, str], Optional[str]]]) -> None:
    """绑定文件权限守卫（None = 不设限）。

    一般不用直接调——运行时用 `guard_scope()` 按**工具调用**绑定，出栈还原：
    粘性绑定会在 Agent 收工后继续拦别人（脚本/CLI 直接调文件工具、同进程换会话），
    也会让测试互相污染。
    """
    global _GUARD
    _GUARD = guard


@contextlib.contextmanager
def guard_scope(guard: Optional[Callable[[str, str], Optional[str]]]) -> Iterator[None]:
    """在 with 块内生效的权限守卫（`agent/core._invoke_tool` 用它）。"""
    global _GUARD
    prev = _GUARD
    _GUARD = guard
    try:
        yield
    finally:
        _GUARD = prev


def check(op: str, path: str) -> Optional[str]:
    """查权限：None = 放行；否则返回给模型看的拒绝理由。

    守卫自身抛异常时**放行并审计**（fail-open）：一个坏掉的守卫不该把
    agent 彻底锁死；但会留痕，便于发现"守卫没生效"这种情况。
    """
    if _GUARD is None:
        return None
    try:
        return _GUARD(op, path)
    except Exception as exc:  # noqa: BLE001——守卫不能成为故障点
        safety._audit(f"[file_guard][异常放行] {op} {path}: {exc!r}")
        return None


def claim(op: str, path: str) -> None:
    """操作成功后通知守卫（新文件归属创建者；守卫可选实现）。"""
    guard = _GUARD
    claimer = getattr(guard, "claim", None) if guard is not None else None
    if claimer is None:
        return
    try:
        claimer(op, path)
    except Exception as exc:  # noqa: BLE001
        safety._audit(f"[file_guard][claim 异常] {op} {path}: {exc!r}")
