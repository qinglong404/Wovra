"""终端输出的着色与排版。

双层方案：
    * 简单的行级消息（工具活动、提示、列表）→ 本模块的 ANSI 转义，
      依赖为零、行内粒度可控；
    * AI 回答是多行 Markdown（标题/列表/代码块）→ 用 rich 的
      Markdown 渲染，自己解析不现实。这是当初预留的升级点。

降级策略：输出不是终端（重定向/管道/测试捕获）、设置了 NO_COLOR、
或 TERM=dumb（cron/CI/部分内嵌终端，不理解转义码）时，ANSI 助手
退化为纯文本，rich 也会自动关闭样式——保证 `wovra list > out.md`
这类用法不会混入转义码。WOVRA_COLOR=1 可在管道里强制保留颜色。
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


def _detect_color() -> bool:
    """颜色开关的环境探测：任何"终端不理解转义码"的信号都关闭。

    判定顺序（后三条任一命中即关）：
        * WOVRA_COLOR=1 → 强制开：`wovra chat | tee run.log` 这类
          管道用法想要彩色日志时的显式出口，优先级最高；
        * stdout 不是终端（重定向/管道/测试捕获）→ 关；
        * NO_COLOR 存在即关（无论值是什么，遵循 No Color 规范；
          Windows 的 cmd 环境变量大小写不敏感，environ 已统一大小写）；
        * TERM=dumb → 关：这类终端会把 \033[91m 原样打出来变成
          "?[91m" 乱码（cron/CI/部分编辑器内嵌终端都是 dumb）。
    Windows 的 cmd.exe/Windows Terminal 通常不设 TERM——取不到值
    视为不 dumb，颜色照常启用。
    """
    if os.environ.get("WOVRA_COLOR") == "1":
        return True
    if not sys.stdout.isatty():
        return False
    if os.environ.get("NO_COLOR") is not None:
        return False
    return os.environ.get("TERM") != "dumb"


_ENABLED = _detect_color()

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
        # 不用 transient（2026-09-11 实测）：transient 结束时"擦掉实时帧
        # 再静态重打一遍"，部分终端擦除 off-by-one 残留首行，与后面的
        # 静态全文叠出"开头重复"（真实会话 R1 复现）。让 Live 帧自己
        # 保留为最终渲染——同一段内容只渲染一遍，没有擦除就没有残留。
        # console 必须显式传入：rich 默认用全局控制台，会绕过 _ENABLED
        # 的着色开关、也让测试无法捕获。
        _answer_live = Live(
            Markdown(""), refresh_per_second=4, console=_console
        )
        _answer_live.start()
    if _answer_live is not None:
        _answer_live.update(Markdown("".join(_answer_buffer)))
    else:
        print(text, end="", flush=True)


def answer_live_stop() -> None:
    """段落结束：Live 帧保留为最终渲染，不再二次静态打印（消除开头
    重复）；无 Live 能力时纯文本增量已流过，同样不重复输出。"""
    global _answer_live, _answer_buffer
    if _answer_live is not None:
        _answer_live.stop()
        _answer_live = None
        print()  # Live 帧末无换行，为后续状态行另起一行
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


def thinking_head(text: str, limit: int = 100) -> str:
    """思考单行化的内容：折叠空白取**尾部**——单行滚动显示最近思路。"""
    collapsed = " ".join((text or "").split())
    if len(collapsed) <= limit:
        return collapsed
    return "…" + collapsed[-limit:]


def thinking_line(text: str) -> str:
    """思考单行（品红），配合 \\r 原地刷新。"""
    return paint(f"💭 {text}", "magenta")


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
    if stats.get("ttft_max"):
        parts.append(
            f"首字 {stats['ttft_max']:.1f}s（合计 {stats['ttft_seconds']:.1f}s）"
        )
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


# ---- 人机协同报告（组织运行时 V1）：机械渲染，零模型成本 ---------------------


def _registry_lines(registry: list | None) -> list[str]:
    """机械渲染 Agent 注册表（零 LLM）。

    注册表是"分裂产物的下游消费者"最早的一处：域树由分裂分析产出，
    promote 时翻译成条目（`registry.build_entries`），这里给人看。
    描述与所有权文件域是路由的依据，必须可见——否则用户无法判断
    分裂质量（"分出来之后能不能用"）。缩进按路径 ID 的层级。

    3a（2026-09-12 用户口径「每个子 agent 有自己的轮次、步数、上下文窗口与
    占比」）：条目自带运行时账，这里一并打出来——没账的历史会话不打（不编 0）。
    """
    if not registry:
        return ["-（无）"]
    lines: list[str] = []
    for entry in registry:
        if not isinstance(entry, dict):
            continue
        path_id = str(entry.get("id") or "?")
        depth = max(0, path_id.count("-"))
        indent = "  " * depth
        name = str(entry.get("name") or "")
        status = str(entry.get("status") or "")
        lines.append(f"{indent}- {path_id}（{name}｜{status}）")
        stat = _agent_stat_line(entry)
        if stat:
            lines.append(f"{indent}  {stat}")
        desc = " ".join(str(entry.get("description") or "").split())
        if desc:
            cut = desc[:160]
            lines.append(f"{indent}  描述：{cut}{'…' if len(desc) > 160 else ''}")
        files = [str(f) for f in (entry.get("file_domains") or [])]
        if files:
            shown = "、".join(files[:6]) + ("…" if len(files) > 6 else "")
            lines.append(f"{indent}  所有权文件域：{shown}")
    return lines


def _agent_stat_line(entry: dict) -> str:
    """该 agent 的运行时账行（3a）：轮次 / 步数 / 上下文 / 窗口占比 / 转出。"""
    rounds = int(entry.get("rounds") or 0)
    steps = int(entry.get("steps") or 0)
    cur = int(entry.get("ctx_cur") or 0)
    peak = int(entry.get("ctx_peak") or 0)
    window = int(entry.get("window") or 0)
    handoffs = int(entry.get("handoffs") or 0)
    if not any((rounds, steps, cur, peak, window, handoffs)):
        return ""
    parts = [f"轮 {rounds}", f"步 {steps}"]
    if cur or peak:
        share = f"（{cur / window:.0%}）" if window else ""
        parts.append(f"上下文 {cur:,}{share}／峰值 {peak:,} tok")
    if window:
        parts.append(f"窗口 {window:,}")
    if handoffs:
        parts.append(f"转出 {handoffs}")
    return "｜".join(parts)


def _domain_view_lines(task) -> list[str]:
    """机械渲染域视图摘要（零 LLM，Level 1 第二步的可见面）。

    为什么必须可见：域视图是"装配按域分化"的材料层——它决定每个子 agent
    看得到什么。用户若看不到"每个域拿到多少块、多大体量、账本切到几条"，
    就无法判断分裂质量（同注册表栏的理由）。
    """
    try:
        from .views import build_views, human_report
    except Exception:  # noqa: BLE001——渲染层的降级：缺该模块不阻断报告
        return ["-（域视图不可用）"]
    try:
        built = build_views(task.rounds, task.get_state(), registry=task.registry)
    except Exception as exc:  # noqa: BLE001
        return [f"-（域视图派生失败：{exc!r}）"]
    lines = human_report(built, registry=task.registry)
    return lines[2:] if len(lines) > 2 else ["-（无域视图）"]


def report_view(task, children: list[dict] | None = None) -> str:
    """人机协同报告：大局默认，细节按事件 ID 剥开（机制四）。

    全部内容来自 TaskState 与 rounds 的机械渲染——零 LLM 成本，随时
    可看。决策升级与待办实验单列成栏（一等公民）。children 是子任务
    摘要列表 [{"id","status","goal"}]，由 CLI 扫描 parent_id 得出。
    """
    state = task.get_state()
    lines = [f"# 任务报告：{task.id}", ""]
    head = f"目标：{task.goal}　状态：{task.status}"
    if task.parent_id:
        head += f"　父任务：{task.parent_id}"
    lines.append(head)
    if state.current_status:
        lines.append(f"当前状态：{state.current_status}")

    lines += ["", "## 当前阶段（大步 / 小步）"]
    lines += task.todo_lines()

    lines += ["", "## 已完成"]
    lines += [f"- {x}" for x in state.completed] or ["-（暂无）"]
    lines += ["", "## 已决策"]
    lines += [f"- {x}" for x in state.decisions] or ["-（暂无）"]
    if state.known_issues:
        lines += ["", "## 已知问题"]
        lines += [f"- {x}" for x in state.known_issues]
    if state.open_questions:
        lines += ["", "## 待解决问题"]
        lines += [f"- {x}" for x in state.open_questions]
    lines += ["", "## 决策升级（等你拍板）"]
    lines += [f"- {x}" for x in state.escalations] or ["-（无）"]
    lines += ["", "## 待办实验（需要你验证）"]
    lines += [f"- {x}" for x in state.experiments] or ["-（无）"]

    lines += ["", "## 里程碑线"]
    milestones = []
    for r in task.rounds:
        user_input = r.get("user_input") or {}
        head_text = user_input.get("normalized") or user_input.get("original") or ""
        head_text = " ".join(head_text.split())[:60]
        mark = "✓" if r.get("end_state") == "completed" else "…"
        milestones.append(f"- R{r.get('seq')}{mark} {head_text}")
    lines += milestones or ["-（暂无轮次）"]

    lines += ["", "## 子任务"]
    if children:
        lines += [f"- {c['id']}（{c['status']}）：{c['goal']}" for c in children]
    else:
        lines.append("-（无）")

    lines += ["", "## Agent 注册表（分裂产物，机制三）"]
    lines += _registry_lines(task.registry)

    lines += ["", "## 域视图（Level 1 第二步：装配按域分化的材料）"]
    lines += _domain_view_lines(task)

    lines += [
        "",
        "> 细节按需剥开：对主 agent 说\"展开 R3-E02\"即可取回任意事件原文；",
        "> 未截断的记录见同目录 task.json。",
    ]
    return "\n".join(lines) + "\n"


# ---- 维护进度视图（整理 / 分裂）---------------------------------------------


def _org_watermark() -> int:
    """整理触发线（与 agent/support.py 的 _ORG_WATERMARK_DEFAULT 同口径，
    这里直接读环境变量，避免 ui → agent 的跨包引用）。"""
    return int(os.environ.get("WOVRA_ORG_WATERMARK", "100000"))


def _last_context_estimate(task) -> int | None:
    """从 usage 记账里取最近一次上下文体量（尽力而为，取不到返回 None）。

    agent/core.py 每轮闭合时 record("usage", "... context={x:,} ...")，
    x 带千分位逗号——解析时先去掉逗号。
    """
    for h in reversed(task.history or []):
        if h.get("kind") != "usage":
            continue
        detail = str(h.get("detail") or "")
        idx = detail.find("context=")
        if idx == -1:
            continue
        rest = detail[idx + len("context="):].split()
        if not rest:
            continue
        try:
            return int(rest[0].replace(",", ""))
        except ValueError:
            continue
    return None


def maint_view(task) -> str:
    """维护进度视图：整理（organization）与分裂（split）的机械渲染。

    全部来自 Task.rounds 与 history 的确定性字段，零 LLM 成本：
    * 整理 = 每轮 org_state（done/pending/failed/未触发）+ 整理代次 +
      pending_org 暂存残留 + 水位参考（usage 记账里的最近一次上下文体量）；
    * 分裂 = split_assessment（可分裂性/原因）+ 域归属（domains/unassigned）；
    * 批次记账 = history 里 kind=="maintenance" 的启动/结束记录
      （org/split 成败、批次范围、输入快照、硬上限）。
    """
    rounds = task.rounds or []
    org_states: dict[str, int] = {}
    generations: set[int] = set()
    pending_count = 0
    for r in rounds:
        st = r.get("org_state") or ""
        org_states[st] = org_states.get(st, 0) + 1
        if r.get("org_generation"):
            generations.add(int(r["org_generation"]))
        if r.get("pending_org"):
            pending_count += 1

    lines = [f"# 维护进度：{task.id}", ""]
    lines.append("## 整理（organization）")
    total = len(rounds)
    if total == 0:
        lines.append("-（尚无轮次）")
    else:
        done = org_states.get("done", 0)
        pending = org_states.get("pending", 0)
        failed = org_states.get("failed", 0)
        untouched = org_states.get("", 0)
        lines.append(
            f"- 轮次 {total}：已整理 {done}（{done / total * 100:.0f}%）"
            f"／排队中 {pending}／失败 {failed}／未触发 {untouched}"
        )
        if generations:
            lines.append(f"- 最新整理代次：{max(generations)}")
        if pending_count:
            lines.append(
                f"- 有 {pending_count} 轮整理产物待生效（pending_org 暂存）："
                "产物在**轮闭合边界**即时生效（§50）；只有「跑完时用户已经"
                "进了下一轮」这种情形才会留到下一次开轮补生效"
            )
    ctx = _last_context_estimate(task)
    if ctx is not None:
        lines.append(
            f"- 最近一次上下文体量 ≈ {ctx:,}（整理触发线 {_org_watermark():,}）"
        )

    lines += ["", "## 分裂（split）"]
    split_rounds = [r for r in rounds if r.get("split_assessment")]
    if not split_rounds:
        lines.append("-（尚无分裂分析产物——整理批次完成后会附带分裂判定）")
    else:
        for r in split_rounds:
            sa = r["split_assessment"] or {}
            splittable = sa.get("splittable")
            if splittable is True:
                mark = "可分裂"
            elif splittable is False:
                mark = "不可分裂"
            else:
                mark = "（无判定）"
            lines.append(f"- R{r.get('seq')}：{mark}")
            reason = " ".join(str(sa.get("reason") or "").split())
            if reason:
                cut = reason[:120]
                lines.append(f"  原因：{cut}{'…' if len(reason) > 120 else ''}")
            domains = r.get("domains") or []
            if domains:
                names = [
                    str(d.get("name") or "?") for d in domains if isinstance(d, dict)
                ]
                shown = "、".join(names[:5]) + ("…" if len(names) > 5 else "")
                lines.append(f"  域：{len(domains)} 个（{shown}）")
            unassigned = r.get("unassigned") or {}
            un_ids = unassigned.get("block_ids") or []
            if un_ids:
                lines.append(f"  未归属（归主 agent）：{len(un_ids)} 块")

    lines += ["", "## Agent 注册表（分裂产物，机制三）"]
    lines += _registry_lines(getattr(task, "registry", None))

    lines += ["", "## 域视图（Level 1 第二步：装配按域分化的材料）"]
    lines += _domain_view_lines(task)

    lines += ["", "## 维护批次（history 记账）"]
    maint = [h for h in (task.history or []) if h.get("kind") == "maintenance"]
    if not maint:
        lines.append("-（无维护批次记录——尚未触发过水位整理）")
    else:
        for h in maint:
            lines.append(f"- {h.get('time', '')} {h.get('detail', '')}")

    lines += [
        "",
        "> task.json 的机械渲染，零模型成本；批次记账由维护管线自动写入 history。",
    ]
    return "\n".join(lines) + "\n"
