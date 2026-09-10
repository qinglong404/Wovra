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
    """本轮是否有"工具类"调用（run_command 非环境、list_files、web_fetch、
    todo 等；文件操作与环境命令不算——它们各有标签/块承载）。"""
    for event in r.get("events") or []:
        if event.get("type") != "tool_call":
            continue
        for call in (event.get("message") or {}).get("tool_calls") or []:
            fn = call.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            if blocks._block_kind(str(fn.get("name") or ""), args) == "tool":
                return True
    return False


def block_tags(b: dict, versions_before: dict) -> str:
    """文件块的生命周期标签（零 LLM），可多标签，按定义拼接：

    创建——write_file 且之前无此文件；
    重构——write_file 但文件已存在（整体重写）；
    修改——edit_file / replace_lines（编辑修改，非清空重写）；
    读——有 read 操作且其后有写/改/删操作（读服务于后续动作）；
    只读——只进行了 read 操作；
    删除——删除文件操作。
    """
    ops = b.get("ops") or []
    has_write = any(o["op"] == "write" for o in ops)
    has_edit = any(o["op"] == "edit" for o in ops)
    has_delete = any(o["op"] == "delete" for o in ops)
    reads = [i for i, o in enumerate(ops) if o["op"] == "read"]
    leading_read = any(
        any(ops[j]["op"] in ("write", "edit", "delete")
            for j in range(i + 1, len(ops)))
        for i in reads
    )
    tags = []
    if has_write:
        tags.append(
            "创建" if versions_before.get(b.get("file", ""), 0) == 0 else "重构"
        )
    if has_edit:
        tags.append("修改")
    if leading_read:
        tags.append("读")
    elif reads and not has_write and not has_edit and not has_delete:
        tags.append("只读")
    if has_delete:
        tags.append("删除")
    return "，".join(tags)


def _state_label(ledger_state: str) -> str:
    """文件状态显示：live→LIVE / dead→DEAD / read_only→LIVE（存在即可用）。
    HISTORICAL 预留：文件被另一文件取代时（move/重构替换）出现，本会话数据暂无。"""
    return {"live": "LIVE", "dead": "DEAD"}.get(ledger_state, "LIVE")


def block_label(b: dict, versions_before: dict, has_tools: bool,
                file_count: int, state: str) -> str:
    """块标签行（只打标签，不写内容）。

    工具标签规则（用户规格 2026-09-09）：
      * 本轮只有 1 个文件块 → 工具调用确定服务该文件，打「工具」；
      * 本轮多个文件块 → 不确定归属哪个，打「工具?」；
      * 保底块（无文件交互）调用工具了 → 打「工具」。
    幽灵文件块（从未真实存在）：保留块，打「幽灵」标签，状态 DEAD。
    """
    if b["kind"] == "fallback":
        return "【保底块】：" + ("工具" if has_tools else "")
    if b["kind"] == "environment":
        return "【环境块】："
    if b.get("fail_tags"):
        # 失败块：幽灵=读文件不存在（从未存在）/ 越界=路径越界被拦
        label = f"【{b['file']}(DEAD)】：{'，'.join(b['fail_tags'])}"
    else:
        label = f"【{b['file']}({state})】：{block_tags(b, versions_before)}"
    if has_tools:
        label += "，工具" if file_count == 1 else "，工具?"
    return label


def build(task_id: str) -> str:
    d = task_module.Task.load(task_id)
    ledger = lifecycle.FileLedger()
    lines: list[str] = []
    for r in d.rounds:
        seq = r["seq"]
        versions_before = {p: len(e["versions"])
                           for p, e in ledger.entries().items()}
        bs = blocks.segment_round_by_file(r)
        ledger.update(r, blocks=bs)
        has_tools = round_has_tool_calls(r)
        file_count = sum(1 for b in bs if b["kind"] == "file")
        lines.append(f"R{seq}：")
        lines.append("\n\n".join(
            block_label(b, versions_before, has_tools, file_count,
                        _state_label(ledger.state_of(b.get("file", ""))))
            for b in bs
        ))
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
