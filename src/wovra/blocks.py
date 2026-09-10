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
# 首条用户输入不进块（轮头 👤/🎯/📌 承载）；被打断后补充/修正的用户
# 输入 → 用户块（全量保存）；非文件非环境工具事件与助手结论并入"当前
# 活动块"（最近的文件块/环境块）；轮首无活动块时挂起并入首个块——
# 每个事件恰好归属一个块，块是分裂拼装的最小单位。
# 块顺序 = 块内第一次交互在事件流中的时间顺序。
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


# 读/删操作的结果失败分类（2026-09-09 用户拍板）：
#   幽灵——文件不存在（FileNotFoundError），从未真实存在；
#   越界——路径越界被安全拦截（读没发生）；
#   工具出错（通用执行错误）——不用管，不作特殊分类。
_NOT_FOUND_MARKERS = ("不存在", "FileNotFoundError", "No such file")
_ESCAPE_MARKERS = ("路径越界",)

# 工具归属 v2（2026-09-09 用户拍板）：执行时按工具性质打标签——
# 验证类（test/build/run 标签命令）验证刚做的文件工作，吸收进最近
# 文件块并打「工具」；调研类（file 标签命令、web_search/fetch、
# list_files）与账本类（todo/notify/consult）吸收但不打标——文件块
# 的「工具」只代表"有验证/执行类工具，结果需融入叙述"。
_VERIFY_TAGS = ("test", "build", "run")


def _op_failure(op: dict, results: dict) -> Optional[str]:
    """读/删操作的结果失败分类：幽灵 / 越界 / None（成功或通用工具出错）。"""
    if op["op"] in ("write", "edit"):
        return None
    content = results.get(op.get("c", "")) or ""
    if any(m in content for m in _NOT_FOUND_MARKERS):
        return "幽灵"
    if any(m in content for m in _ESCAPE_MARKERS):
        return "越界"
    return None


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
    if b.get("fail_tags"):
        out["fail_tags"] = list(b["fail_tags"])
    if b.get("_has_tools"):
        out["has_tools"] = True
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
    user_blocks: list[dict] = []
    cur: Optional[dict] = None
    pending: list[str] = []
    pending_verify = False  # 挂起事件里含验证类工具（并入首块后标记）
    user_seen = False
    call_owner: dict[str, dict] = {}  # tool_call_id → 归属块
    results: dict[str, str] = {}       # tool_call_id → 结果文本（幽灵判定）

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
            if not user_seen:
                user_seen = True
                continue  # 首条用户输入由轮头 👤/🎯/📌 承载
            # 被打断后补充/修正的用户输入 → 用户块（全量保存，时间序就位）
            b = _fblock("user", idx)
            b["_evs"].append(eid)
            user_blocks.append(b)
            continue

        if etype == "tool_call":
            owners: dict[int, dict] = {}  # 事件只入各归属块一次（并行去重）
            for call in message.get("tool_calls") or []:
                name, args = _call_info(call)
                cid = str(call.get("id") or "")
                k = _block_kind(name, args)
                if k == "file":
                    blk = new_file(str(args["path"]), idx)
                    blk["ops"].append({
                        "e": eid, "op": FILE_OP_TOOLS[name], "c": cid,
                    })
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
                        if name == "run_command" and tag_command(
                                str(args.get("command") or "")) in _VERIFY_TAGS:
                            pending_verify = True
                        continue
                    if name == "run_command":
                        tag = tag_command(str(args.get("command") or ""))
                        _remember(cur, "command_types", tag)
                        # 验证类才打「工具」；调研/账本类吸收但不打标
                        if cur["kind"] == "file" and tag in _VERIFY_TAGS:
                            cur["_has_tools"] = True
                    call_owner[cid] = cur
                    owners[id(cur)] = cur
            for blk in owners.values():
                blk["_evs"].append(eid)
                blk["_last"] = max(blk["_last"], idx)
        elif etype == "tool_result":
            blk = call_owner.get(str(message.get("tool_call_id") or ""))
            results[str(message.get("tool_call_id") or "")] = str(
                message.get("content") or ""
            )
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

    # 失败块分类：读/删全失败（幽灵=文件不存在 / 越界=路径越界）→ 保留
    # 块但打失败标签（agent.py 越界、nope.txt 幽灵的实证）；通用工具出错
    # 不作分类（不用管）。
    for blk in file_blocks.values():
        failures = [_op_failure(o, results) for o in blk["ops"]]
        if all(f is not None for f in failures):
            fail_tags = []
            for tag in ("幽灵", "越界"):
                if tag in failures:
                    fail_tags.append(tag)
            if fail_tags:
                blk["fail_tags"] = fail_tags

    merged = list(file_blocks.values()) + ([env_block] if env_block else [])
    if not merged:
        if user_blocks:
            # 用户块不占保底名额：用户补充块 + 保底块并存（至少两块）——
            # 保底承载助手结论，不能只描述用户输入丢了模型输出
            non_user = [(i, e) for i, e in enumerate(events)
                        if e.get("type") != "user"]
            fb = _fblock("fallback", non_user[0][0] if non_user else 0)
            fb["_evs"] = [str(e.get("id") or "") for _, e in non_user]
            fb["_last"] = max((i for i, _ in non_user), default=0)
            merged = user_blocks + [fb]
            pending = []  # 保底块已含全部非用户事件（含挂起），防重复
        else:
            # 保底块：整轮一块（无文件/环境/用户补充交互）
            fb = _fblock("fallback", 0)
            fb["_evs"] = [str(e.get("id") or "") for e in events]
            fb["_last"] = max(len(events) - 1, 0)
            return [_finalize_fblock(seq, 1, fb)]
    else:
        merged += user_blocks
    if pending:
        first = min(merged, key=lambda b: b["_first"])
        first["_evs"] = pending + first["_evs"]
        if pending_verify and first["kind"] == "file":
            first["_has_tools"] = True
    merged.sort(key=lambda b: b["_first"])
    return [_finalize_fblock(seq, i + 1, b) for i, b in enumerate(merged)]
