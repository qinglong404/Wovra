"""按文件分块 + 生命周期标签 → 结构化格式（零 LLM，纯确定性）。

用法：python scripts/file_block_structure.py <task_id> [--out docs/xxx.md]
输出（用户格式规格 2026-09-09）——只打标签，辅助整理；块之间空行：

    R1：
    【保底块】：

    R4：
    【环境块】：

    【agent.py】：只读

块标签（用户定义 2026-09-09）：
    创建——write_file 且之前无此文件；
    重构——write_file 但文件已存在（整体重写）；
    修改——edit_file / replace_lines（编辑修改，非清空重写）；
    读——有 read 操作且其后有写/改/删操作；
    只读——只进行了 read 操作；
    删除——删除文件操作。
工具标签：本轮单文件块→工具（确定归属）；多文件块→工具?；保底块
调用工具→工具。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wovra import blocks, lifecycle, task as task_module  # noqa: E402


def round_has_tool_calls(r: dict) -> bool:
    """本轮是否有工具类调用——统一实现见 blocks.round_has_tool_calls。"""
    return blocks.round_has_tool_calls(r)


def block_tags(b: dict, versions_before: dict) -> str:
    """文件块生命周期标签（零 LLM）——统一实现见 blocks.lifecycle_tags。"""
    return blocks.lifecycle_tags(b, versions_before)


def _state_label(ledger_state: str) -> str:
    """文件状态显示——统一实现见 blocks.state_label。"""
    return blocks.state_label(ledger_state)


def block_label(b: dict, versions_before: dict, has_tools: bool,
                state: str) -> str:
    """块标签行（只打标签，不写内容）——统一实现见 blocks.label_line。"""
    return blocks.label_line(b, versions_before, state, has_tools)


def _fmt_anchor_run(run: list[int]) -> str:
    """连续轮号压缩成区间+单号：R1-R2/R5。"""
    parts, start = [], run[0]
    prev = run[0]
    for seq in run[1:] + [None]:
        if seq is None or seq != prev + 1:
            parts.append(str(start) if start == prev else f"{start}-{prev}")
            start = seq
        prev = seq
    return "R" + "/R".join(parts)


def build(task_id: str) -> str:
    d = task_module.Task.load(task_id)
    ledger = lifecycle.FileLedger()
    lines: list[str] = []
    chat_run: list[int] = []  # 连续纯聊天轮（保底块且无工具）合并
    for r in d.rounds:
        seq = r["seq"]
        versions_before = {p: len(e["versions"])
                           for p, e in ledger.entries().items()}
        bs = blocks.segment_round_by_file(r)
        ledger.update(r, blocks=bs)
        has_tools = round_has_tool_calls(r)
        is_chat = len(bs) == 1 and bs[0]["kind"] == "fallback" and not has_tools
        if is_chat:
            chat_run.append(seq)
            continue
        if chat_run:
            lines.append(f"{_fmt_anchor_run(chat_run)}：无法定夺")
            lines.append("")
            chat_run = []
        lines.append(f"R{seq}：")
        lines.append("\n\n".join(
            block_label(b, versions_before, has_tools,
                        blocks.state_label(blocks.block_end_state(
                            ledger.state_of(b.get("file", "")), b)))
            for b in bs
        ))
        lines.append("")
    if chat_run:
        lines.append(f"{_fmt_anchor_run(chat_run)}：无法定夺")
        lines.append("")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description="按文件分块格式生成（只打标签）")
    ap.add_argument("task_id")
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    doc = build(args.task_id)
    out = args.out or f"docs/file-block-structure-{args.task_id}.md"
    Path(out).write_text(doc, encoding="utf-8")
    print(f"已写入 {out}（{len(doc)} 字符）")


if __name__ == "__main__":
    main()
