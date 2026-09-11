"""轮内分块（零 LLM、纯函数）。

* `segment_round`（v1，兼容保留）：以写/改为截止的粗分块；
* `segment_round_by_file`（v3，当前主线）：按文件聚合，一个文件的所有
  交互成一块；无文件交互的轮产出保底块（或 user 块 + 保底块）。

v3 的块类型：file（带生命周期标签）/ environment / user / fallback。
"""
from typing import Optional
from .common import FILE_OP_TOOLS, READ_TOOLS, WRITE_TOOLS, _call_info, _remember, tag_command

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

_NOT_FOUND_MARKERS = ("文件不存在:", "目录不存在:", "FileNotFoundError", "No such file")

_ESCAPE_MARKERS = ("路径越界，",)

_VERIFY_TAGS = ("test", "build", "run")

def _op_failure(op: dict, results: dict) -> Optional[str]:
    """读/删操作的结果失败分类：幽灵 / 越界 / None（成功或通用工具出错）。

    判定只看返回文本的**首行**（2026-09-11 机制评审修复）。旧实现按全文
    子串匹配"不存在"，于是读到正文含该字面量的文件（如 files.py 自己的
    `文件不存在: {path}` 文案）会被误判成幽灵——实测当次会话 10/81 块
    中招、R1 纯读轮独占 8 个，而该标签是整理指令的输入，会让组织器把
    live 文件当成 DEAD。

    幽灵与越界都要求"首行命中"：safety 抛的 ValueError 经工具层包装后
    首行形如 `工具执行出错: ValueError('路径越界，…')`，故越界按首行
    中缀判定即可；而通用工具出错（如 `工具执行出错: 编码错误`）不属于
    这两类，仍返回 None（不作分类）。
    """
    if op["op"] in ("write", "edit"):
        return None
    content = results.get(op.get("c", "")) or ""
    head = content.split("\n", 1)[0]
    if any(m in head for m in _ESCAPE_MARKERS):
        return "越界"
    if any(m in head for m in _NOT_FOUND_MARKERS):
        return "幽灵"
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
        # 纯聊天轮：单一保底块（含全部事件）。中途用户补充并入轮头
        # 用户输入（需求先于结论），不单独成块——用户块只在有文件/
        # 环境交互的轮出现（那里它排在时间序里，不与结论抢位）
        fb = _fblock("fallback", 0)
        fb["_evs"] = [str(e.get("id") or "") for e in events]
        fb["_last"] = max(len(events) - 1, 0)
        return [_finalize_fblock(seq, 1, fb)]
    merged += user_blocks
    if pending:
        first = min(merged, key=lambda b: b["_first"])
        first["_evs"] = pending + first["_evs"]
        if pending_verify and first["kind"] == "file":
            first["_has_tools"] = True
    merged.sort(key=lambda b: b["_first"])
    return [_finalize_fblock(seq, i + 1, b) for i, b in enumerate(merged)]
