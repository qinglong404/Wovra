"""按文件分块 + 生命周期账本 → 效果文档（零 LLM，纯确定性）。

用法：python scripts/file_block_structure.py <task_id> [--out docs/xxx.md]
产出：逐轮块结构 + 文件生命周期账本（LIVE/DEAD/READ_ONLY）的可读视图，
供人工核验分块质量与生命周期推导。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wovra import blocks, lifecycle, task as task_module  # noqa: E402


def _head(text: str, limit: int = 60) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit] + "…"


def render_round_block(seq: int, b: dict, events: list) -> str:
    """一块一行摘要（人读用）。"""
    kind = b["kind"]
    if kind == "file":
        ops = ", ".join(f"{o['op']}({o['e'].rsplit('-E',1)[-1]})" for o in b["ops"])
        return f"- **{b['id']}** file `{b['file']}`：{ops} · {len(b['events'])} 事件"
    if kind == "environment":
        tags = "/".join(b.get("command_types") or [])
        return f"- **{b['id']}** 环境块[{tags}]：{len(b['events'])} 事件"
    if kind == "tool":
        tags = "/".join(b.get("command_types") or []) or "其他"
        return f"- **{b['id']}** 工具块[{tags}]：{len(b['events'])} 事件"
    if kind == "user":
        ui = next(
            (str(e["message"].get("content") or "") for e in events
             if e["id"] == b["start_event"]), "")
        return f"- **{b['id']}** 用户：{_head(ui)}"
    if kind == "assistant":
        fa = next(
            (str(e["message"].get("content") or "") for e in events
             if e["id"] == b["start_event"]), "")
        return f"- **{b['id']}** 助手：{_head(fa)}"
    return f"- **{b['id']}** {kind}"


def build(task_id: str) -> str:
    d = task_module.Task.load(task_id)
    ledger = lifecycle.FileLedger()
    lines = [
        f"# 按文件分块效果 · 会话 {task_id}",
        "",
        f"生成：`segment_round_by_file` + `FileLedger`（零 LLM，纯确定性）",
        "",
        "## 概览",
        "",
    ]

    total_events = total_blocks = 0
    per_round_files = []
    for r in d.rounds:
        bs = blocks.segment_round_by_file(r)
        total_events += len(r.get("events") or [])
        total_blocks += len(bs)
        per_round_files.append(len([b for b in bs if b["kind"] == "file"]))
        ledger.update(r, blocks=bs)

    dist = {n: per_round_files.count(n) for n in sorted(set(per_round_files))}
    lines += [
        f"- 轮数：{len(d.rounds)}，总事件 {total_events}，总块数 {total_blocks}",
        f"- 每轮文件块数分布：" +
        "，".join(f"{n} 个 × {c} 轮" for n, c in dist.items()),
        f"- 生命周期：LIVE **{ledger.live_count()}** / DEAD "
        f"{sum(1 for e in ledger.entries().values() if e['state']=='dead')} / "
        f"READ_ONLY {sum(1 for e in ledger.entries().values() if e['state']=='read_only')}",
        "",
        "## 文件生命周期账本",
        "",
    ]

    live = [e for e in ledger.entries().values() if e["state"] == "live"]
    dead = [e for e in ledger.entries().values() if e["state"] == "dead"]
    ro = [e for e in ledger.entries().values() if e["state"] == "read_only"]

    lines.append(f"### LIVE（{len(live)}）——关注度最高")
    lines.append("")
    lines.append("| 文件 | 版本数 | 写 | 读 | 块引用 |")
    lines.append("|---|---:|---:|---:|---|")
    for e in live:
        lines.append(
            f"| `{e['path']}` | {len(e['versions'])} | {e['write_count']} | "
            f"{e['read_count']} | {' '.join(e['block_refs']) or '—'} |")
    lines.append("")

    lines.append(f"### DEAD（{len(dead)}）——只保留结论（结论槽待整理填充）")
    lines.append("")
    lines.append("| 文件 | 版本数 | 删除事件 |")
    lines.append("|---|---:|---|")
    for e in sorted(dead, key=lambda x: x["deleted_at"] or ""):
        lines.append(f"| `{e['path']}` | {len(e['versions'])} | {e['deleted_at']} |")
    lines.append("")

    lines.append(f"### READ_ONLY（{len(ro)}）——低关注，需要时再读")
    lines.append("")
    lines.append("| 文件 | 读次数 |")
    lines.append("|---:|---|")
    for e in ro:
        lines.append(f"| `{e['path']}` | {e['read_count']} |")
    lines.append("")

    lines.append("## 逐轮块结构")
    lines.append("")
    for r in d.rounds:
        seq = r["seq"]
        bs = blocks.segment_round_by_file(r)
        evs = r.get("events") or []
        lines.append(f"### R{seq}（{len(evs)} 事件 · {len(bs)} 块）")
        lines.append("")
        for b in bs:
            lines.append(render_round_block(seq, b, evs))
        lines.append("")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description="按文件分块效果文档生成")
    ap.add_argument("task_id")
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    doc = build(args.task_id)
    out = args.out or f"docs/file-block-structure-{args.task_id}.md"
    Path(out).write_text(doc, encoding="utf-8")
    print(f"已写入 {out}（{len(doc)} 字符）")


if __name__ == "__main__":
    main()
