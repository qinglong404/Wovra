"""工具输出的统一上限与「超限落盘」——默认全量，上限只是防爆阀。

背景（2026-09-11 用户拍板，worklog §25）：工具层此前各自写死一个小上限
（run_command 1500 字符、search_files 50 条、glob_files 200 条、
check_background 2000 字符、web_fetch 8000 字符），命中即**丢内容**。
实测代价：模型看不到中段 → 换方法重试 → 多烧数轮到数十轮往返，而省下
的不过几百 token。用户口径是「除压缩外，全面全量输入」——而压缩只属于
整理侧（水位整理），不属于工具返回。

现行规则（2026-09-11 二次修订，用户口径：「不要截断丢失文本，量大可以
不进上下文，返回截断内容但可以让你取回，告诉它有多大就可以」）：

* 默认上限 200,000 字符（约 60K tok），`WOVRA_OUTPUT_LIMIT` 可调整。
  未超限时**原样全量返回**——正常命令根本碰不到这条线。
* 真超限时只内联开头一小段预览（默认 2,000 字符，`WOVRA_PREVIEW_CHARS`
  可调），附上**原文体量**（字符数 / 行数）与**取回方式**。完整内容
  落盘 `output/spill/`，模型按需用 read_file 取回——大输出不进上下文，
  但一个字都不丢，而且模型知道它有多大、可以去哪拿。
  这与「首尾各留一大段」的区别：后者仍把上限字符塞进上下文（默认
  20 万字符 ≈ 60K tok），省不下任何东西，还让中段凭空消失。
* 原文本来就在磁盘上的（read_file），不落盘副本，直接给继续读取的
  位置提示（start_line），因为取回路径就是原文件。
* 上限作用在**工具返回值**这一层，装配层零截断的纪律不受影响。
"""

from __future__ import annotations

import os
import time
from pathlib import Path

_DEFAULT_LIMIT = 200_000

# 超限时内联多少（只有开头预览进上下文；其余靠落盘 + 按需取回）
_DEFAULT_PREVIEW = 2_000


def preview_chars() -> int:
    """超限时内联的预览字符数。`WOVRA_PREVIEW_CHARS` 可调；非法值退回默认。"""
    raw = os.environ.get("WOVRA_PREVIEW_CHARS", "")
    if raw.strip():
        try:
            value = int(raw)
        except ValueError:
            return _DEFAULT_PREVIEW
        if value > 0:
            return value
    return _DEFAULT_PREVIEW


def output_limit() -> int:
    """工具返回值的字符上限。`WOVRA_OUTPUT_LIMIT` 可调；非法值退回默认。"""
    raw = os.environ.get("WOVRA_OUTPUT_LIMIT", "")
    if raw.strip():
        try:
            value = int(raw)
        except ValueError:
            return _DEFAULT_LIMIT
        if value > 0:
            return value
    return _DEFAULT_LIMIT


def list_limit(default: int) -> int:
    """列表型工具（搜索命中、glob 结果）的条数上限。

    与字符上限共用 `WOVRA_OUTPUT_LIMIT`（按条数解释）——调大一个开关
    就把所有工具的输出一起放开，不必记五个环境变量。
    """
    raw = os.environ.get("WOVRA_OUTPUT_LIMIT", "")
    if raw.strip():
        try:
            value = int(raw)
        except ValueError:
            return default
        if value > 0:
            return max(default, value)
    return default


def _spill_dir() -> Path:
    from . import safety

    path = Path(safety.PROJECT_ROOT) / "output" / "spill"
    path.mkdir(parents=True, exist_ok=True)
    return path


def spill(text: str, name: str) -> str | None:
    """把完整内容落盘，返回相对工作区的路径；失败返回 None（不影响主流程）。

    落盘目录 output/spill/ 已在 .gitignore 内（output/ 整目录忽略）。
    """
    try:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        target = _spill_dir() / f"{stamp}-{name}.txt"
        target.write_text(text, encoding="utf-8")
        from . import safety

        try:
            return target.relative_to(Path(safety.PROJECT_ROOT)).as_posix()
        except ValueError:
            return str(target)
    except OSError:
        return None


def clip(text: str, name: str, limit: int | None = None,
         source: str | None = None) -> str:
    """超限时只内联开头预览 + 体量 + 取回方式；未超限原样返回。

    2026-09-11 二次修订（worklog §26）：原实现超限时内联 limit 那么长
    的首尾（默认 20 万字符）——既不省上下文，中段又凭空消失。现在超限
    只内联 preview_chars() 字符的预览，把体量与取回方式告诉模型。

    source：原文本来就在磁盘上的路径（read_file 传它），此时不落盘副本，
    只在提示里指明去哪取。
    name：落盘文件名用途前缀（run_command / web_fetch / search…）。
    """
    limit = limit if limit is not None else output_limit()
    if len(text) <= limit:
        return text
    preview_n = min(preview_chars(), limit)  # 预览绝不比上限还多
    saved = None if source else spill(text, name)
    return f"{text[:preview_n]}\n{_overflow_note(text, preview_n, saved, source)}"


def _overflow_note(text: str, preview_n: int, saved: str | None,
                   source: str | None) -> str:
    """超限提示：先说原文有多大，再说去哪取/怎么定位，最后给省事的替代做法。

    2026-09-12 强化（用户口径）：大输出可以不全加载，但必须能**快速定位**
    到要的那一段，且永远不丢信息。故提示里直接给出定位用法（pattern=）。
    """
    lines = text.count("\n") + 1
    size = f"原文共 {len(text):,} 字符 / {lines:,} 行"
    if source:
        return (
            f"…（{size}；此处只内联开头 {preview_n:,} 字符。完整内容仍在原文件 "
            f"{source}：用 read_file('{source}', pattern='关键词') 定位，"
            f"或按 start_line/num_lines 取任意区间。）"
        )
    if saved:
        return (
            f"…（{size}；此处只内联开头 {preview_n:,} 字符。完整内容已落盘 "
            f"{saved}：用 read_file('{saved}', pattern='关键词') 定位，"
            f"或按 start_line/num_lines 取任意区间；也可让命令只输出关键部分。）"
        )
    return (
        f"…（{size}；此处只内联开头 {preview_n:,} 字符。落盘失败，"
        f"请缩小范围重跑或让命令只输出关键部分。）"
    )
