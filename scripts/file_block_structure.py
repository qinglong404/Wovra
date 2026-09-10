"""按文件分块 + 生命周期标签 → 结构化格式（零 LLM，纯确定性）。

用法：python scripts/file_block_structure.py <task_id> [--out docs/xxx.md]
输出（用户格式规格 2026-09-09）——只打标签，辅助整理；块之间空行：

    R1：
    【保底块】：

    R4：
    【环境块】：

    【agent.py】：只读

块标签：保底块 / 环境块 / 【文件】：生命周期标签（创建/修改/只读/删除，
账本判创建 vs 修改）。标签定义由用户另行给出，此处只产出标签。
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
    """文件块的生命周期标签（零 LLM），可多标签：
    创建（首写）/ 修改（后续写）/ 只读（仅读无写删）/ 删除，按序拼接。
    例如本轮创建又删除 → 「创建，删除」。
    """
    ops = b.get("ops") or []
    has_write = any(o["op"] in ("write", "edit") for o in ops)
    has_read = any(o["op"] == "read" for o in ops)
    has_delete = any(o["op"] == "delete" for o in ops)
    tags = []
    if has_write:
        tags.append(
            "创建" if versions_before.get(b.get("file", ""), 0) == 0 else "修改"
        )
    if has_read and not has_write and not has_delete:
        tags.append("只读")
    if has_delete:
        tags.append("删除")
    return "，".join(tags)


def block_label(b: dict, versions_before: dict, has_tools: bool,
                file_count: int) -> str:
    """块标签行（只打标签，不写内容）。

    工具标签规则（用户规格 2026-09-09）：
      * 本轮只有 1 个文件块 → 工具调用确定服务该文件，打「工具」；
      * 本轮多个文件块 → 不确定归属哪个，打「工具?」；
      * 保底块（无文件交互）调用工具了 → 打「工具」。
    """
    if b["kind"] == "fallback":
        return "【保底块】：" + ("工具" if has_tools else "")
    if b["kind"] == "environment":
        return "【环境块】："
    label = f"【{b['file']}】：{block_tags(b, versions_before)}"
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
            block_label(b, versions_before, has_tools, file_count) for b in bs
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
