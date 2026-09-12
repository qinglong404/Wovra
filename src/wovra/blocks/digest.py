"""块摘要与检视渲染：喂给语义标注的路由摘要（block_digest）+ 人工
检视用的可读视图（render_round）。

两种块结构并存（2026-09-11 收口：落盘口径已是 v3，v1 仅作兼容/离线
对比）：
* v3（`segment_round_by_file`）——按文件聚合，带 `file`/`ops`/`events`；
  块的 index 区间**会重叠**（一次并行写多文件时同一 tool_call 属多个块），
  所以块内事件必须按自带的 `events` 列表取；
* v1（`segment_round`）——以写/改为截止的粗分块，只有连续区间与
  `wrote_files`/`touched_files`。
本模块对两者都做适配（`_block_event_indices` + 字段 `.get` 兜底）。
"""
from typing import Optional
from .common import WRITE_TOOLS, _call_info, _head, tag_command
from .segment import segment_round, segment_round_by_file


def _block_event_indices(r: dict, b: dict) -> list[int]:
    """块内事件在轮事件流里的下标（v3 优先按 ID 取，v1 回退连续区间）。

    越界守卫（2026-09-12，会话 20260912-110034-98d42c 实测）：v1 回退路径是
    **位置区间** `start..end`，它假定"块记的下标在该轮 events 里仍然存在"。
    两种现场不成立：① 开新轮瞬间（新轮 `events=[]`，而 promote 恰好发生在
    这一刻）→ `end` 落在空数组外；② 事件被截短的轮（历史/诊断副本）。
    此前直接 `events[i]` 抛 IndexError，被 `_record_split_lifecycle` 吞掉 →
    promote 那刻的视图体量、生命周期动作、status 全部没落账（7 个域至今
    dormant 的根因之一，见 worklog §44 / `scripts/probe_split_watermark_indexerror.py`）。
    故此处把区间夹进 events 的实际范围：**拿不到就不取，而不是崩**。
    """
    events = r.get("events") or []
    ids = b.get("events")
    if ids:
        wanted = set(ids)
        return [i for i, e in enumerate(events) if e.get("id") in wanted]
    try:
        start = int(b.get("start") or 0)
        end = int(b.get("end") if b.get("end") is not None else -1)
    except (TypeError, ValueError):
        return []
    lo, hi = max(0, start), min(end, len(events) - 1)
    return list(range(lo, hi + 1)) if hi >= lo else []

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
    for i in _block_event_indices(r, b):
        e = events[i]
        if not isinstance(e, dict) or e.get("type") != "tool_call":
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
    # 块内若含最终回答，附头部（那是"对用户的承诺"，路由价值高）。
    # 同一守卫：`end` 可能落在 events 之外（空轮/截短轮），此时退到最后一条
    # 真实事件——摘要少一行不致命，抛 IndexError 会把整条维护管线带崩。
    tail = None
    try:
        idx = int(b.get("end") if b.get("end") is not None else -1)
    except (TypeError, ValueError):
        idx = -1
    if events:
        tail = events[idx] if 0 <= idx < len(events) else events[-1]
    if isinstance(tail, dict) and tail.get("type") == "final_answer":
        lines.append(f"  ◆ 最终回答头: {_head(str(tail['message'].get('content') or ''), 100)}")
    return "\n".join(lines)

def render_round(r: dict, blocks: Optional[list[dict]] = None) -> str:
    """把一个 Round 的分块渲染成人读视图（检查用；不改任何数据）。

    blocks 缺省时现场计算（v3 主线，与落盘口径一致）——历史 task.json
    不需要迁移。也接受 v1 块（离线对比用）：字段访问对两套结构都容错。
    """
    events = r.get("events") or []
    if not events:
        return ""
    if blocks is None:
        blocks = segment_round_by_file(r)
    state = "" if r.get("end_state") == "completed" else "（进行中）"
    lines = [f"R{r.get('seq', 0)} · {len(events)} 事件 · {len(blocks)} 块{state}"]
    for b in blocks:
        parts = [b["id"], f"{b['start_event']}~{b['end_event']}"]
        wrote = list(b.get("wrote_files") or [])
        touched = list(b.get("touched_files") or [])
        if b.get("file"):  # v3：一个文件一块（写/读由 ops 推）
            ops = {o.get("op") for o in b.get("ops") or []}
            if ops & {"write", "edit"}:
                wrote = [b["file"]]
            else:
                touched = [b["file"]]
        if wrote:
            parts.append("写: " + ", ".join(wrote))
        reads = [f for f in touched if f not in wrote]
        if reads:
            parts.append("读: " + ", ".join(reads))
        if b.get("command_types"):
            parts.append("[" + ", ".join(b["command_types"]) + "]")
        if b["kind"] == "environment":
            parts.append("环境块")
        lines.append("  " + " · ".join(parts))
        # 块内的命令原文与写/改动作摘出来，肉眼核对切块质量用
        for i in _block_event_indices(r, b):
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
