"""工具输出的统一上限与「超限落盘」——默认全量，上限只是防爆阀。

背景（2026-09-11 用户拍板，worklog §25）：工具层此前各自写死一个小上限
（run_command 1500 字符、search_files 50 条、glob_files 200 条、
check_background 2000 字符、web_fetch 8000 字符），命中即**丢内容**。
实测代价：模型看不到中段 → 换方法重试 → 多烧数轮到数十轮往返，而省下
的不过几百 token。用户口径是「除压缩外，全面全量输入」——而压缩只属于
整理侧（水位整理），不属于工具返回。

现行规则：

* 默认上限 200,000 字符（约 60K tok），`WOVRA_OUTPUT_LIMIT` 可调整。
  正常命令根本碰不到它，它只防 `dir /s` 之类把上下文一次性炸掉。
* 真超限时**不丢数据**：完整内容落盘到 `output/spill/`，返回文本给出
  路径，模型可用 read_file 分段取回（或让命令只输出关键部分）。
* 上限作用在**工具返回值**这一层，装配层零截断的纪律不受影响。
"""

from __future__ import annotations

import os
import time
from pathlib import Path

_DEFAULT_LIMIT = 200_000

# 首尾各保留多少（仅在真超限的兜底路径上用）
_HEAD_RATIO = 2 / 3


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


def clip(text: str, name: str, limit: int | None = None) -> str:
    """超限时保留首尾 + 完整内容落盘；未超限原样返回。

    name 用于落盘文件名（如 run_command / web_fetch），便于事后辨认。
    """
    limit = limit if limit is not None else output_limit()
    if len(text) <= limit:
        return text
    head = int(limit * _HEAD_RATIO)
    tail = limit - head
    omitted = len(text) - head - tail
    saved = spill(text, name)
    note = (
        f"…（中间 {omitted:,} 字符未内联：原文共 {len(text):,} 字符。"
        f"完整内容已落盘 {saved}，用 read_file 分段读取；"
        f"也可调大 WOVRA_OUTPUT_LIMIT 或让命令只输出关键部分）"
        if saved
        else f"…（中间 {omitted:,} 字符未内联：原文共 {len(text):,} 字符）"
    )
    return f"{text[:head]}\n{note}\n{text[-tail:]}"
