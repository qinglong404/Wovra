"""终端输出的着色与排版助手。

为什么不用 rich 等第三方库：项目调性是"低成本优先"，当前需要的
只是几种颜色和缩进，30 行 ANSI 转义就够了；等排版需求真的复杂起来
（表格、进度条、markdown 渲染）再考虑引入。

降级策略：输出不是终端（重定向/管道/测试捕获）或设置了 NO_COLOR
时，所有函数自动退化为纯文本——保证 `wovra show > out.md` 这类
用法不会混入转义码。
"""

import os
import sys

_ENABLED = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None

# ANSI SGR 转义码：字体样式 1/2，前景色 30-37
_CODES = {
    "bold": 1,
    "dim": 2,
    "red": 31,
    "green": 32,
    "yellow": 33,
    "blue": 34,
    "magenta": 35,
    "cyan": 36,
}


def paint(text: str, *styles: str) -> str:
    """给文本加 ANSI 样式；未启用着色时原样返回。"""
    if not _ENABLED or not styles:
        return text
    prefix = "".join(f"\033[{_CODES[s]}m" for s in styles)
    return f"{prefix}{text}\033[0m"


# ---- 语义化的输出助手：调用方说"这是什么消息"，样式集中在这里管 ----------


def info(text: str) -> str:
    """中性提示信息。"""
    return paint(text, "dim")


def success(text: str) -> str:
    """成功/完成类信息。"""
    return paint(text, "green", "bold")


def error(text: str) -> str:
    """错误信息。"""
    return paint(text, "red", "bold")


def user_prompt() -> str:
    """交互模式的输入提示符。"""
    return paint("你> ", "cyan", "bold")


def user(text: str) -> str:
    """回放历史中的用户发言。"""
    return paint("你> ", "cyan", "bold") + text


def assistant(text: str) -> str:
    """AI 的回答——终端里最重要的内容，绿色加粗标识。"""
    return paint("助手> ", "green", "bold") + text


def tool_call(name: str, arguments: str) -> str:
    """工具调用事件：黄色，属于过程信息。"""
    return paint(f"  [调用工具] {name}({arguments})", "yellow")


def tool_result(name: str, result: str, limit: int = 80) -> str:
    """工具返回事件：暗色，只留预览。"""
    preview = result if len(result) <= limit else result[:limit] + "…"
    return paint(f"  [工具结果] {name} -> {preview}", "dim")


def rule(text: str = "") -> str:
    """分隔线，可带标题。"""
    if text:
        return paint(f"──── {text} " + "─" * 40, "dim")
    return paint("─" * 56, "dim")


def status(status_value: str) -> str:
    """任务状态 → 中文 + 颜色。"""
    mapping = {
        "in_progress": ("进行中", "yellow"),
        "done": ("已完成", "green"),
        "blocked": ("已阻塞", "red"),
    }
    label, color = mapping.get(status_value, (status_value, "cyan"))
    return paint(f"{label:<4}", color)
