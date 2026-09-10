"""按文件分块 + 生命周期账本 → 效果文档（零 LLM，纯确定性）。

用法：python scripts/file_block_structure.py <task_id> [--out docs/xxx.md]
输出 = 用户格式规格（2026-09-09）：
    R{n}（{events} 事件 · {blocks} 块）【没有文件交互，触发保底 1 块】
    👤 用户: "…"
    🎯 意图: ***（待整理填充）
    📌 关键约束: ***
    块细节：
    【保底块】：助手结论。
    【环境块】：…（为什么这样做+结果，LLM 整理填充；此处为确定性摘要）
    【world.js】：修改，write(E02), read(E04)
块名规则：文件块带生命周期标签（创建/修改/只读/删除，账本判创建 vs 修改）；
环境块不打标签；无文件交互的轮保底 1 块。
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


def block_tag(b: dict, versions_before: dict) -> str:
    """文件块的生命周期标签（零 LLM）：删除 > 创建/修改（账本判）> 只读。"""
    ops = b.get("ops") or []
    if any(o["op"] == "delete" for o in ops):
        return "删除"
    if any(o["op"] in ("write", "edit") for o in ops):
        return "创建" if versions_before.get(b.get("file", ""), 0) == 0 else "修改"
    return "只读"


def block_line(seq: int, b: dict, events: list, versions_before: dict) -> str:
    """块细节一行（确定性摘要；整理（LLM）接管后由它写描述）。"""
    kind = b["kind"]
    evs = {e["id"]: e for e in events}
    if kind == "fallback":
        fa = next(
            (str(e["message"].get("content") or "") for e in events
             if e.get("type") == "final_answer"), "")
        return f"【保底块】：助手结论。{_head(fa, 80)}"
    if kind == "environment":
        cmds = []
        for eid in b.get("events") or []:
            e = evs.get(eid) or {}
            if e.get("type") != "tool_call":
                continue
            for call in (e.get("message") or {}).get("tool_calls") or []:
                fn = call.get("function") or {}
                if fn.get("name") == "run_command":
                    import json as _json
                    try:
                        args = _json.loads(fn.get("arguments") or "{}")
                    except Exception:
                        args = {}
                    cmds.append(_head(str(args.get("command") or ""), 50))
        return f"【环境块】：{'；'.join(cmds) or '环境配置'}"
    if kind == "file":
        ops = ", ".join(f"{o['op']}({o['e'].rsplit('-E', 1)[-1]})"
                        for o in (b.get("ops") or []))
        return f"【{b['file']}】：{block_tag(b, versions_before)}，{ops or '工具调用'}"
    return f"【{kind}】"


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
    fallback_n = sum(1 for r in d.rounds
                     if not any(b["kind"] != "fallback"
                                for b in blocks.segment_round_by_file(r)))
    lines += [
        f"- 轮数：{len(d.rounds)}，总事件 {total_events}，总块数 {total_blocks}"
        f"（保底轮 {fallback_n}）",
        f"- 每轮文件块数分布：" +
        "，".join(f"{n} 个 × {c} 轮" for n, c in dist.items()),
        f"- 生命周期：LIVE **{ledger.live_count()}** / DEAD "
        f"{sum(1 for e in ledger.entries().values() if e['state'] == 'dead')} / "
        f"READ_ONLY {sum(1 for e in ledger.entries().values() if e['state'] == 'read_only')}",
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

    lines.append("## 逐轮结构化结果")
    lines.append("")

    # 第二轮：按用户格式规格输出，且带生命周期标签（需账本判创建 vs 修改）
    ledger2 = lifecycle.FileLedger()
    for r in d.rounds:
        seq = r["seq"]
        versions_before = {p: len(e["versions"])
                           for p, e in ledger2.entries().items()}
        bs = blocks.segment_round_by_file(r)
        ledger2.update(r, blocks=bs)
        evs = r.get("events") or []
        ui = r.get("user_input") or {}
        note = ""
        if len(bs) == 1 and bs[0]["kind"] == "fallback":
            note = "【没有文件交互，触发保底 1 块】"
        lines.append(f"### R{seq}（{len(evs)} 事件 · {len(bs)} 块）{note}")
        lines.append("")
        lines.append(f"👤 用户: \"{_head(ui.get('original') or '', 80)}\"")
        lines.append(f"🎯 意图: {ui.get('normalized') or '***'}")
        lines.append(f"📌 关键约束: {ui.get('key_constraints') or '***'}")
        lines.append("块细节：")
        for b in bs:
            lines.append(block_line(seq, b, evs, versions_before))
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
