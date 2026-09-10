"""Block 结构化（上下文分化运行时 · 机制一）：纯规则、零 LLM。

docs/context-differentiation-runtime.md §2 机制一。轮内以**写/改文件**
为截止划分 Block；shell 命令打分类标签（v1 六类）；环境配置天然单独
组块；一轮没有写/改 → 整轮一块。全部是确定性规则——Runtime 先把明显
的结构切出来，语义归后面的低频批量标注（机制二）。

segment_round 是事件流的纯函数：不读运行时状态、不调模型，对历史
task.json 同样成立（scripts/render_blocks.py 即此离线用法）。

Block v1 结构（最小集 + 渲染辅助字段）：
    {
      "id": "R17-B1",
      "kind": "work" | "environment",
      "start": 0, "end": 4,             # 事件下标区间（含端点）
      "start_event": "R17-E01",
      "end_event": "R17-E05",
      "touched_files": ["icp.py"],      # 块内涉及的文件路径（去重保序）
      "wrote_files": ["icp.py"],        # 其中被写/改的（块截止的依据）
      "command_types": ["test"],        # 块内 shell 命令标签（去重保序）
    }
事件原文不复制——渲染/整理时按下标回查，块本身零 token 成本。
"""

import json
import re
from typing import Optional

WRITE_TOOLS = ("write_file", "edit_file")
READ_TOOLS = ("read_file",)

# 命令标签 v1 六类：environment / file / test / build / run / other。
# 元组顺序即优先级——安装类最先（"pip install pytest" 是装环境不是跑
# 测试），其后测试、构建、常驻服务、文件操作，可执行类兜底在 "other"
# 之前。全字符串规则，零 LLM。
_TAG_RULES = (
    ("environment", re.compile(
        r"pip3?\s+install|uv\s+(add|pip|sync|venv|init|tool)|npm\s+(install|\bi\b|\bci\b)"
        r"|yarn\s+add|pnpm\s+(add|i\b)|conda\s+(install|create|activate)"
        r"|apt(-get)?\s+(install|update)|brew\s+install|cargo\s+add"
        r"|python[\d.]*\s+-m\s+venv|virtualenv|requirements\.txt|export\s+\w+=")),
    ("test", re.compile(
        r"pytest|py\.test|unittest|npm\s+(run\s+)?test|yarn\s+test|pnpm\s+test"
        r"|jest|vitest|node\s+--test|go\s+test|cargo\s+test|\btox\b")),
    ("build", re.compile(
        r"npm\s+run\s+build|yarn\s+build|pnpm\s+build|\bgcc\b|\bg\+\+|clang"
        r"|\bmake\b|cmake|cargo\s+build|go\s+build|\btsc\b|py_compile|webpack"
        r"|esbuild|vite\s+build|\bmvn\b|gradle")),
    ("environment", re.compile(
        r"uvicorn|gunicorn|flask\s+run|http\.server|npm\s+run\s+dev|npm\s+start"
        r"|yarn\s+(dev|start)|pnpm\s+dev|\bvite\b|next\s+dev|live-server"
        r"|runserver|artisan\s+serve")),
    ("file", re.compile(
        r"\b(mkdir|rmdir|cp|mv|rm|touch|ls|dir|cat|head|tail|ln|chmod|chown"
        r"|tar|unzip|zip|gzip|grep|rg|find|findstr|sed|awk|wc|diff)\b")),
    ("run", re.compile(
        r"\bpython[\d.]*\b|\bnode\b|\bdeno\b|\bbun\b|\bruby\b|\bjava\b|go\s+run"
        r"|cargo\s+run|\bbash\b|\bzsh\b|\bsh\b|\./|powershell")),
)


def tag_command(command: str) -> str:
    """shell 命令 → 六类标签（零 LLM）。

    按 _TAG_RULES 顺序首个命中即返回，全不命中归 "other"（git/curl/
    echo 等杂项）。复合命令（&&/;）整串匹配——安装、测试这类强信号
    优先，宁可高估环境属性也不漏隔离。
    """
    for tag, pattern in _TAG_RULES:
        if pattern.search(command or ""):
            return tag
    return "other"


def _call_info(call: dict) -> tuple[str, dict]:
    """tool_call → (工具名, 参数 dict)；参数解析失败按空参处理。"""
    fn = call.get("function") or {}
    try:
        args = json.loads(fn.get("arguments") or "{}")
    except json.JSONDecodeError:
        args = {}
    if not isinstance(args, dict):
        args = {}
    return str(fn.get("name") or ""), args


def _new_block(kind: str) -> dict:
    return {
        "kind": kind,
        "_idx": [],           # 私有：事件下标累积，_finalize 时剥离
        "start_event": "",
        "end_event": "",
        "touched_files": [],
        "wrote_files": [],
        "command_types": [],
    }


def _remember(block: dict, key: str, value: str) -> None:
    """去重保序地记录一个值（文件路径 / 命令标签）。"""
    if value and value not in block[key]:
        block[key].append(value)


def _finalize(seq: int, bno: int, cur: dict) -> dict:
    """剥掉私有字段，落成可持久化的最小 Block 结构。"""
    idxs = cur.pop("_idx")
    return {
        "id": f"R{seq}-B{bno}",
        "kind": cur["kind"],
        "start": idxs[0],
        "end": idxs[-1],
        "start_event": cur["start_event"],
        "end_event": cur["end_event"],
        "touched_files": cur["touched_files"],
        "wrote_files": cur["wrote_files"],
        "command_types": cur["command_types"],
    }


def segment_round(r: dict) -> list[dict]:
    """把一个 Round 的事件流切成 Block 列表（纯函数，零 LLM）。

    边界规则（确定性，v1）：
      * write_file / edit_file 是块的截止——它的调用与 tool_result
        落在同一块（结果事件永远跟随其调用）；
      * 环境类命令自成环境块，连续的环境命令合一块；环境块之后接任何
        工作事件都开新块；
      * 合并轮里的第二条用户输入开新块（新工作脉络）；
      * 其余事件（读/搜/讨论）跟随当前块；最终回答挂在收尾块上
        （它是本轮的结论，不是新工作单元）；
      * 全轮没有写/改 → 整轮一块（"总结成一段话"是整理的事，
        结构上就是一块）。
    """
    events = r.get("events") or []
    seq = r.get("seq", 0)
    blocks: list[dict] = []
    cur: Optional[dict] = None
    pending = False  # 块已含写/改调用，等它的 tool_result 跟上再谈边界

    def close() -> None:
        nonlocal cur, pending
        if cur is not None and cur["_idx"]:
            blocks.append(_finalize(seq, len(blocks) + 1, cur))
        cur = None
        pending = False

    for idx, event in enumerate(events):
        etype = event.get("type")
        message = event.get("message") or {}

        if etype == "tool_result":
            # 结果跟随它的调用所在块。并行批次里写的结果后面可能还跟着
            # 其他调用的结果，所以不在结果处关块——边界留给下一个
            # 工作事件（tool_call / user / final_answer）。
            if cur is None:
                cur = _new_block("work")
        elif etype == "user":
            close()
            cur = _new_block("work")
        elif etype == "tool_call":
            infos = [_call_info(c) for c in message.get("tool_calls") or []]
            is_env = any(
                name == "run_command"
                and tag_command(str(args.get("command") or "")) == "environment"
                for name, args in infos
            )
            has_write = any(name in WRITE_TOOLS for name, _ in infos)
            if is_env:
                # 环境块：连续环境命令合一块；从工作块切入则先关旧的
                if cur is not None and cur["kind"] != "environment":
                    close()
                if cur is None:
                    cur = _new_block("environment")
            else:
                if pending or (cur is not None and cur["kind"] == "environment"):
                    close()
                if cur is None:
                    cur = _new_block("work")
            for name, args in infos:
                if name in WRITE_TOOLS:
                    _remember(cur, "touched_files", str(args.get("path") or ""))
                    _remember(cur, "wrote_files", str(args.get("path") or ""))
                elif name in READ_TOOLS:
                    _remember(cur, "touched_files", str(args.get("path") or ""))
                elif name == "run_command":
                    _remember(
                        cur, "command_types",
                        tag_command(str(args.get("command") or "")),
                    )
            if has_write:
                pending = True
        else:
            # final_answer 及其他非工具事件：跟随当前块（它是本轮的结论，
            # 不是新工作单元——写截止拦不住它）；只有环境块才把它隔开
            if cur is not None and cur["kind"] == "environment":
                close()
            if cur is None:
                cur = _new_block("work")

        cur["_idx"].append(idx)
        if not cur["start_event"]:
            cur["start_event"] = str(event.get("id") or "")
        cur["end_event"] = str(event.get("id") or "")

    close()
    return blocks


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


def _head(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit] + "…"


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


# ---- v2/v3：按文件聚合的块切分（讨论v2 机制，零 LLM） -----------------------
# 2026-09-09 用户拍板格式规格：块只有三种——
#   * file       一个文件在本轮的全部交互（ops：read/write/edit/delete），
#                带生命周期标签（创建/修改/只读/删除，账本判创建 vs 修改）；
#   * environment 本轮环境命令合并为一块（描述"为什么这样做+结果"）；
#   * fallback   轮内无任何文件/环境交互时的保底块（整轮一块，助手结论
#                是描述主体）。
# 用户输入不进块（轮头 👤/🎯/📌 承载）；非文件非环境工具事件与助手
# 结论并入"当前活动块"（最近的文件块/环境块）；轮首无活动块时挂起并入
# 首个块——每个事件恰好归属一个块，块是分裂拼装的最小单位。
# 纯规则、零 LLM；v1 segment_round 退役由格式规格定。

FILE_OP_TOOLS = {
    "read_file": "read",
    "write_file": "write",
    "edit_file": "edit",
    "replace_lines": "edit",
    "delete_file": "delete",
}


def _block_kind(name: str, args: dict) -> str:
    """tool_call → 归属块类：file / environment / tool（零 LLM）。

    文件操作但路径解析不出（参数截断等失败调用）按 tool 计——它没碰到
    任何文件，不产生文件块（R28 三次 write_file 参数被掐断的实证）。
    """
    if name in FILE_OP_TOOLS and str(args.get("path") or ""):
        return "file"
    if name == "run_command" and tag_command(str(args.get("command") or "")) == "environment":
        return "environment"
    return "tool"


def _fblock(kind: str, idx: int) -> dict:
    return {
        "kind": kind,
        "_first": idx,
        "_last": idx,
        "_evs": [],
        "file": "",
        "ops": [],
        "command_types": [],
    }


def _finalize_fblock(seq: int, bno: int, b: dict) -> dict:
    """剥私有字段，落成可持久化的 v3 块结构。"""
    evs = b.pop("_evs")
    out = {
        "id": f"R{seq}-B{bno}",
        "kind": b["kind"],
        "start": b["_first"],
        "end": b["_last"],
        "start_event": evs[0] if evs else "",
        "end_event": evs[-1] if evs else "",
        "events": evs,
    }
    if b["kind"] == "file":
        out["file"] = b["file"]
        out["ops"] = b["ops"]
    if b["command_types"]:
        out["command_types"] = b["command_types"]
    return out


def segment_round_by_file(r: dict) -> list[dict]:
    """按工作对象聚合一个 Round 的块（纯函数，零 LLM）。

    块类：file / environment / fallback。返回块按首事件在轮内的出现
    顺序排序；轮内每个事件恰好归属一个块（用户输入除外——它由轮头
    👤/🎯/📌 承载）。
    """
    events = r.get("events") or []
    seq = r.get("seq", 0)
    file_blocks: dict[str, dict] = {}
    env_block: Optional[dict] = None
    cur: Optional[dict] = None
    pending: list[str] = []
    call_owner: dict[str, dict] = {}  # tool_call_id → 归属块

    def new_file(path: str, idx: int) -> dict:
        blk = file_blocks.get(path)
        if blk is None:
            blk = _fblock("file", idx)
            blk["file"] = path
            file_blocks[path] = blk
        return blk

    def new_env(idx: int) -> dict:
        nonlocal env_block, cur
        if env_block is None:
            env_block = _fblock("environment", idx)
        cur = env_block
        return env_block

    for idx, event in enumerate(events):
        etype = event.get("type")
        eid = str(event.get("id") or "")
        message = event.get("message") or {}

        if etype == "user":
            continue  # 轮头承载，不进块

        if etype == "tool_call":
            owners: dict[int, dict] = {}  # 事件只入各归属块一次（并行去重）
            for call in message.get("tool_calls") or []:
                name, args = _call_info(call)
                cid = str(call.get("id") or "")
                k = _block_kind(name, args)
                if k == "file":
                    blk = new_file(str(args["path"]), idx)
                    blk["ops"].append({"e": eid, "op": FILE_OP_TOOLS[name]})
                    blk["_first"] = min(blk["_first"], idx)
                    blk["_last"] = max(blk["_last"], idx)
                    call_owner[cid] = blk
                    owners[id(blk)] = blk
                    cur = blk
                elif k == "environment":
                    blk = new_env(idx)
                    _remember(blk, "command_types",
                              tag_command(str(args.get("command") or "")))
                    call_owner[cid] = blk
                    owners[id(blk)] = blk
                else:
                    if cur is None:
                        if eid not in pending:
                            pending.append(eid)
                        continue
                    if name == "run_command":
                        _remember(cur, "command_types",
                                  tag_command(str(args.get("command") or "")))
                    call_owner[cid] = cur
                    owners[id(cur)] = cur
            for blk in owners.values():
                blk["_evs"].append(eid)
                blk["_last"] = max(blk["_last"], idx)
        elif etype == "tool_result":
            blk = call_owner.get(str(message.get("tool_call_id") or ""))
            if blk is None:
                if cur is None:
                    pending.append(eid)
                    continue
                blk = cur
            blk["_evs"].append(eid)
            blk["_last"] = max(blk["_last"], idx)
        else:
            # final_answer / runtime_note 等：并入当前活动块；无则挂起
            if cur is not None:
                cur["_evs"].append(eid)
                cur["_last"] = max(cur["_last"], idx)
            else:
                pending.append(eid)

    merged = list(file_blocks.values()) + ([env_block] if env_block else [])
    if not merged:
        # 保底块：整轮一块（无文件/环境交互）；用户输入由轮头承载不进块
        fb = _fblock("fallback", 0)
        fb["_evs"] = [
            str(e.get("id") or "") for e in events if e.get("type") != "user"
        ]
        fb["_last"] = max(len(events) - 1, 0)
        return [_finalize_fblock(seq, 1, fb)]
    if pending:
        first = min(merged, key=lambda b: b["_first"])
        first["_evs"] = pending + first["_evs"]
    merged.sort(key=lambda b: b["_first"])
    return [_finalize_fblock(seq, i + 1, b) for i, b in enumerate(merged)]
