"""终端输出的着色与排版。

双层方案：
    * 简单的行级消息（工具活动、提示、列表）→ 本模块的 ANSI 转义，
      依赖为零、行内粒度可控；
    * AI 回答是多行 Markdown（标题/列表/代码块）→ 用 rich 的
      Markdown 渲染，自己解析不现实。这是当初预留的升级点。

降级策略：输出不是终端（重定向/管道/测试捕获）或设置了 NO_COLOR
时，ANSI 助手退化为纯文本，rich 也会自动关闭样式——保证
`wovra list > out.md` 这类用法不会混入转义码。
"""

import os
import sys
import unicodedata

from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.text import Text

from . import tokens as tokens_module
from .tools import FAILURE_MARKERS

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

    标签行用 rich 自己的样式上色——paint() 的手工 ANSI 交给 rich
    会被转义成裸码（rich 不解释控制字符，实测教训）。
    """
    _console.print(Text("助手>", style="green bold"))
    _console.print(Markdown(text))
    _console.print()


# ---- 流式回答的 Markdown 实时渲染 ---------------------------------------------
# 能力判定：rich Live 只在"可交互且非 legacy 控制台"的原位刷新模式下启用。
# 此前用 vertical_overflow="visible" 想让完整内容留在屏上——内容一旦高过
# 屏幕，Live 退化为每次刷新整页重打，实测就是"开头重复 N 遍、越来越长"
# 的刷屏 bug。现行方案：流式期间只刷新可见尾部（裁剪、transient），
# 段落结束时擦掉实时帧、把整段 Markdown 静态渲染一次（与回放观感一致）；
# 非 TTY 或 legacy 终端退化为纯文本流（不重复输出）。

_answer_live: Live | None = None
_answer_buffer: list[str] = []


def _live_capable() -> bool:
    return _ENABLED and _console.is_interactive and not getattr(
        _console, "legacy_windows", False
    )


def answer_live_start() -> None:
    """进入回答阶段：重置缓冲；Live 在首个增量到达时惰性启动。"""
    global _answer_buffer, _answer_live
    _answer_buffer = []
    _answer_live = None


def answer_live_append(text: str) -> None:
    """追加回答增量：支持原位刷新的终端里更新 Live（只看尾部），
    否则直接输出纯文本增量。"""
    global _answer_buffer, _answer_live
    _answer_buffer.append(text)
    if _answer_live is None and _live_capable():
        _answer_live = Live(Markdown(""), refresh_per_second=4, transient=True)
        _answer_live.start()
    if _answer_live is not None:
        _answer_live.update(Markdown("".join(_answer_buffer)))
    else:
        print(text, end="", flush=True)


def answer_live_stop() -> None:
    """段落结束：擦掉实时帧，把整段 Markdown 静态渲染一次
    （与回放观感一致）；无 Live 能力时纯文本已流过，不重复输出。"""
    global _answer_live, _answer_buffer
    buffer_text = "".join(_answer_buffer)
    if _answer_live is not None:
        _answer_live.stop()  # transient：擦掉实时帧
        _answer_live = None
        if buffer_text.strip():
            _console.print(Markdown(buffer_text))
            print()
    _answer_buffer = []


def tool_pair(name: str, result_detail: str, limit: int = 100) -> str:
    """回放用的配对行：一行说清"调用了什么、成没成"。失败只取首行
    原因，不再漏出整段 stdout（实测反馈）。"""
    failed = any(tag in result_detail for tag in FAILURE_MARKERS)
    if failed:
        first_line = (result_detail.splitlines() or [""])[0]
        reason = " ".join(first_line.split())[:limit]
        return paint(f"  [调用] {name} → 失败：{reason}", "red")
    return paint(f"  [调用] {name} → 成功", "green")


def tool_call(name: str) -> str:
    """工具调用：只说调用了什么，参数不展开（细节在 task.json 可查）。"""
    return paint(f"  [调用] {name}", "yellow")


def tool_result(result: str, limit: int = 100) -> str:
    """工具结果：一行说清成功/失败，失败才给简短原因。"""
    failed = any(tag in result for tag in FAILURE_MARKERS)
    if failed:
        return paint(f"  [结果] 失败：{_head_reason(result, limit)}", "red")
    return paint("  [结果] 成功", "green")


def _head_reason(text: str, limit: int) -> str:
    """从失败结果里提取简短原因（跳过标记词本身）。"""
    for tag in FAILURE_MARKERS:
        position = text.find(tag)
        if position != -1:
            reason = text[position + len(tag):].lstrip("：（:,， ")
            return " ".join(reason.split())[:limit]
    return " ".join(text.split())[:limit]


def thinking_delta(text: str) -> str:
    """流式思考内容：品红，与正式回答明显区分。"""
    return paint(text, "magenta")


def wait_hint(text: str) -> str:
    """等待提示（模型响应中、工具执行中）。"""
    return paint(f"  ⏳ {text}", "cyan")


def status_line(text: str) -> str:
    """系统后台动作的状态行（如"正在整理本轮对话"）。"""
    return paint(f"  … {text}", "cyan")




def _fmt_window(window: int) -> str:
    """窗口大小的人性化显示：1,000,000 → 1M。"""
    if window % 1_000_000 == 0:
        return f"{window // 1_000_000}M"
    if window % 1_000 == 0:
        return f"{window // 1_000}K"
    return f"{window:,}"


def usage_line(stats: dict, maint: dict | None = None,
               context: int | None = None, window: int | None = None) -> str:
    """一次 run 的成本核算行：轮次/步数/工具调用 + tokens 用量与构成。

    所有占比保留 1 位小数；输入构成来自本地估算的相对占比，按
    服务端返回的真实 prompt_tokens 等比校准——各分项之和等于输入
    总量。缓存命中依赖服务端的 prompt_tokens_details，漏报的调用
    按 0 命中计入未命中（口径见 Agent）。maint 是维护账本快照
    （整理/压缩成本，异步跨轮次，由 Agent 记账时取走）。
    context/window：本轮最后一次请求的上下文体量估算与窗口大小
    （占用比例 = 常见工具的"上下文窗口占用"显示，口径见 Agent）。
    """
    parts = [
        f"耗时 {stats['seconds']:.1f}s",
        f"轮次 第{stats.get('turn', 1)}轮",
        f"步数 {stats.get('llm_calls', 0)}",
        f"工具调用 {stats.get('tool_calls', 0)} 次",
    ]
    total = stats.get("total_tokens", 0)
    if total:
        prompt = stats.get("prompt_tokens", 0)
        completion = stats.get("completion_tokens", 0)
        parts.append(f"输入 {prompt:,} tok（{prompt / total:.1%}）")
        parts.append(f"输出 {completion:,} tok（{completion / total:.1%}）")
        reasoning = stats.get("reasoning_tokens", 0)
        if reasoning:
            parts.append(f"其中思考 {reasoning:,} tok")
        parts.append(f"合计 {total:,} tok")
        cached = stats.get("cached_tokens", 0)
        miss = stats.get("cache_miss_tokens", 0)
        if prompt:
            parts.append(f"缓存命中 {cached:,} tok（{cached / prompt:.1%}）")
            parts.append(f"未命中 {miss:,} tok（{miss / prompt:.1%}）")
            parts.append(f"等效输入 {miss + cached / tokens_module.CACHE_RATE:,.0f} tok")
    else:
        parts.append("tokens：服务端未返回 usage")

    if context and window:
        parts.append(
            f"上下文 {context:,} tok（{context / window:.1%}，窗口 {_fmt_window(window)}）"
        )

    maint = maint or {}
    org = (maint.get("organization") or {}).get("total", 0)
    comp = (maint.get("compaction") or {}).get("total", 0)
    if org:
        parts.append(f"整理 {org:,} tok")
    if comp:
        parts.append(f"压缩 {comp:,} tok")

    lines = [paint("  " + " ┃ ".join(parts), "dim")]

    breakdown = stats.get("prompt_breakdown") or {}
    estimated_total = sum(breakdown.values())
    if total and estimated_total:
        # 估算值只代表占比，按真实总量缩放后展示；占比取估算份额
        scale = stats.get("prompt_tokens", 0) / estimated_total
        detail = " ┃ ".join(
            f"{tokens_module.LABELS[category]} {round(value * scale):,} tok（{value / estimated_total:.1%}）"
            for category, value in breakdown.items()
            if value
        )
        lines.append(paint(f"  输入构成：{detail}", "dim"))
    return "\n".join(lines)


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
