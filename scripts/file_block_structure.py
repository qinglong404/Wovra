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
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wovra import blocks, lifecycle, task as task_module  # noqa: E402


def block_tag(b: dict, versions_before: dict) -> str:
    """文件块的生命周期标签（零 LLM）：删除 > 创建/修改（账本判）> 只读。"""
    ops = b.get("ops") or []
    if any(o["op"] == "delete" for o in ops):
        return "删除"
    if any(o["op"] in ("write", "edit") for o in ops):
        return "创建" if versions_before.get(b.get("file", ""), 0) == 0 else "修改"
    return "只读"


def block_label(b: dict, versions_before: dict) -> str:
    """块标签行（只打标签，不写内容）。"""
    if b["kind"] == "fallback":
        return "【保底块】："
    if b["kind"] == "environment":
        return "【环境块】："
    return f"【{b['file']}】：{block_tag(b, versions_before)}"


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
        lines.append(f"R{seq}：")
        lines.append("\n\n".join(block_label(b, versions_before) for b in bs))
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
