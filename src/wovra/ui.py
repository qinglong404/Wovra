"""终端输出的着色与排版。

双层方案：
    * 简单的行级消息（工具活动、提示、列表）→ 本模块的 ANSI 转义，
      依赖为零、行内粒度可控；
    * AI 回答是多行 Markdown（标题/列表/代码块）→ 用 rich 的
      Markdown 渲染，自己解析不现实。这是当初预留的升级点。

降级策略：输出不是终端（重定向/管道/测试捕获）或设置了 NO_COLOR
时，ANSI 助手退化为纯文本，rich 也会自动关闭样式——保证
`wovra show > out.md` 这类用法不会混入转义码。
"""

import os
import sys
import unicodedata

from rich.console import Console
from rich.markdown import Markdown

_ENABLED = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None

# rich 只在真正要渲染 Markdown 时才创建；样式开关跟随 _ENABLED
_console = Console(no_color=not _ENABLED)

# ANSI SGR 转义码：1/2 是字体样式；前景色用 90-97（高亮系列），
# 比 30-37 的标准色亮，暗色终端里可读性更好
_CODES = {
    "bold": 1,
    "dim": 2,
    "red": 91,
    "green": 92,
    "yellow": 93,
    "blue": 94,
    "magenta": 95,
    "cyan": 96,
}


def paint(text: str, *styles: str) -> str:
    """给文本加 ANSI 样式；未启用着色时原样返回。"""
    if not _ENABLED or not styles:
        return text
    prefix = "".join(f"\033[{_CODES[s]}m" for s in styles)
    return f"{prefix}{text}\033[0m"


def display_width(text: str) -> int:
    """文本在终端里占的列数：中文等全角字符占 2 列，其余占 1 列。"""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def pad(text: str, width: int) -> str:
    """按"显示宽度"补齐空格。

    不能用 f-string 的 :<N：一是它按字符数而不是显示宽度算
    （中文会错位），二是它必须作用在着色前的纯文本上——先着色再
    补齐会把转义码也数进长度，导致补齐完全失效（列会粘在一起）。
    正确顺序永远是：先 pad，再 paint。
    """
    return text + " " * max(0, width - display_width(text))


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


def assistant_markdown(text: str) -> None:
    """AI 回答的正式输出：标签行 + Markdown 渲染。

    rich 会按终端宽度折行、给标题/列表/代码块加结构和颜色；
    非 TTY 环境下自动退化为无样式的纯文本排版。
    """
    _console.print(paint("助手>", "green", "bold"))
    _console.print(Markdown(text))
    _console.print()


def tool_call(name: str, arguments: str) -> str:
    """工具调用事件：黄色，属于过程信息。"""
    return paint(f"  [调用工具] {name}({arguments})", "yellow")


# 工具结果里出现这些字样说明执行失败了（与 Task._result_tail 的判定一致）
_FAILURE_TAGS = ("工具执行出错", "未知工具", "合法 JSON")


def tool_result(name: str, result: str, limit: int = 80) -> str:
    """工具返回事件：成功绿色、失败红色，一眼区分。"""
    preview = result if len(result) <= limit else result[:limit] + "…"
    style = "red" if any(tag in result for tag in _FAILURE_TAGS) else "green"
    return paint(f"  [工具结果] {name} -> {preview}", style)


def thinking_delta(text: str) -> str:
    """流式思考内容：暗紫（magenta + dim），与正式回答明显区分。"""
    return paint(text, "magenta", "dim")


def usage_line(stats: dict) -> str:
    """一次 run 的成本核算行：耗时 + tokens 用量与占比。

    占比按 prompt / completion 各占总 tokens 的百分比展示；
    推理模型单列思考 token（它是 completion 的一部分，已含在内）。
    服务端没回 usage 时如实说明，而不是显示 0 误导人。
    """
    parts = [f"耗时 {stats['seconds']:.1f}s"]
    total = stats.get("total_tokens", 0)
    if total:
        prompt = stats.get("prompt_tokens", 0)
        completion = stats.get("completion_tokens", 0)
        parts.append(f"输入 {prompt:,} tok（{prompt / total:.0%}）")
        parts.append(f"输出 {completion:,} tok（{completion / total:.0%}）")
        reasoning = stats.get("reasoning_tokens", 0)
        if reasoning:
            parts.append(f"其中思考 {reasoning:,} tok")
        parts.append(f"合计 {total:,} tok")
    else:
        parts.append("tokens：服务端未返回 usage")
    return paint("  " + " ┃ ".join(parts), "dim")


def rule(text: str = "") -> str:
    """分隔线，可带标题。"""
    if text:
        return paint(f"──── {text} " + "─" * 40, "dim")
    return paint("─" * 56, "dim")


def status(status_value: str, width: int | None = None) -> str:
    """任务状态 → 中文 + 颜色；width 用于表格列对齐（先补齐再着色）。"""
    mapping = {
        "in_progress": ("进行中", "yellow"),
        "done": ("已完成", "green"),
        "blocked": ("已阻塞", "red"),
    }
    label, color = mapping.get(status_value, (status_value, "cyan"))
    if width is not None:
        label = pad(label, width)
    return paint(label, color)
