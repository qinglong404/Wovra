"""块摘要与检视渲染：喂给语义标注的路由摘要（block_digest）+ 人工
检视用的可读视图（render_round）。
"""
from typing import Optional
from .common import WRITE_TOOLS, _call_info, _head, tag_command
from .segment import segment_round

def block_digest(r: dict, b: dict) -> str:
    """块的确定性摘要（机制二 LLM 标注的输入）：路由式信息，零 LLM。

    只带"动作与对象"——工具名、文件路径、命令原文（截断）、用户输入
    头部；正文内容不复制（那是 Full 存档的事，索引只做路由）。
    """
    events = r.get("events") or []
    lines = [f"{b['id']}（{b['start_event']}~{b['end_event']}，{b['kind']}）"]
    ui_head = _head(str((r.get("user_input") or {}).get("original") or ""), 80)
    if ui_head:
        lines.append(f"本轮用户输入: {ui_head}")
    for i in range(b["start"], b["end"] + 1):
        e = events[i]
        if e.get("type") != "tool_call":
            continue
        for call in (e.get("message") or {}).get("tool_calls") or []:
            name, args = _call_info(call)
            if name == "run_command":
                cmd = str(args.get("command") or "")
                lines.append(f"  ▸ [{tag_command(cmd)}] {_head(cmd, 100)}")
            elif name in WRITE_TOOLS:
                size = len(str(args.get("content") or ""))
                lines.append(f"  ✎ {name} → {args.get('path', '')}（{size} 字符）")
            elif name == "edit_file":
                lines.append(
                    f"  ✎ edit_file → {args.get('path', '')}"
                    f"（{len(str(args.get('old_text') or ''))} → "
                    f"{len(str(args.get('new_text') or ''))} 字符）"
                )
            elif name == "read_file":
                lines.append(f"  👁 read_file ← {args.get('path', '')}")
    # 块内若含最终回答，附头部（那是"对用户的承诺"，路由价值高）
    tail = events[b["end"]]
    if tail.get("type") == "final_answer":
        lines.append(f"  ◆ 最终回答头: {_head(str(tail['message'].get('content') or ''), 100)}")
    return "\n".join(lines)

def render_round(r: dict, blocks: Optional[list[dict]] = None) -> str:
    """把一个 Round 的分块渲染成人读视图（检查用；不改任何数据）。

    blocks 缺省时现场计算——历史 task.json 不需要迁移。
    """
    events = r.get("events") or []
    if not events:
        return ""
    if blocks is None:
        blocks = segment_round(r)
    state = "" if r.get("end_state") == "completed" else "（进行中）"
    lines = [f"R{r.get('seq', 0)} · {len(events)} 事件 · {len(blocks)} 块{state}"]
    for b in blocks:
        parts = [b["id"], f"{b['start_event']}~{b['end_event']}"]
        if b["wrote_files"]:
            parts.append("写: " + ", ".join(b["wrote_files"]))
        reads = [f for f in b["touched_files"] if f not in b["wrote_files"]]
        if reads:
            parts.append("读: " + ", ".join(reads))
        if b["command_types"]:
            parts.append("[" + ", ".join(b["command_types"]) + "]")
        if b["kind"] == "environment":
            parts.append("环境块")
        lines.append("  " + " · ".join(parts))
        # 块内的命令原文与写/改动作摘出来，肉眼核对切块质量用
        for i in range(b["start"], b["end"] + 1):
            e = events[i]
            if e.get("type") != "tool_call":
                continue
            for call in (e.get("message") or {}).get("tool_calls") or []:
                name, args = _call_info(call)
                if name == "run_command":
                    cmd = str(args.get("command") or "")
                    lines.append(f"        ▸ [{tag_command(cmd)}] {_head(cmd, 90)}")
                elif name in WRITE_TOOLS:
                    lines.append(f"        ✎ {name}({args.get('path', '')})")
    return "\n".join(lines)
