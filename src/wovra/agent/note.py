"""每轮一段话：锚（机械材料）、解析与校验（V4 §3.2，第一步）。

这一步只做"写"：**水位处攒批**，一批轮一次调用，产出这批每轮一条
**每轮一段话**（一句话 ＋ 失败 ＋ 账本增量）。**不改装配、不改归因**——
产物先只落盘与进账本，装配只在折档那一刻读它。

分工（§3.2）：
* **代码给锚**：轮内用户发言、动作清单（工具×次数、读写删过的文件、非零退出）、失败候选
  （带事件 ID）、结论草稿——这些原本要模型从上百 K 事件里回忆，是纯思考；
* **模型写一句话**：只写这一轮自己那份交班记录；
* **代码盖章**：执行者（该轮 `active_view`，不让模型自报身份）。

校验分两档（§154/§155）：**硬门只剩结构**（有产物 / seq 落在批次里 / 句子非空），
**内容正确性一律软档**（只留痕）；**产物一律落地**，失败只留痕、不重发、不跨轮。
"""
from __future__ import annotations

import json
import re

from .. import blocks as blocks_module
from .. import tokens as tokens_module
from .. import views as views_module

# 结算的提交出口（模型侧工具名；守卫在 ledger.py）
_SUBMIT_TOOL = "submit_round_notes"

# 只扫"执行/抓取/检索"类工具的返回：读类工具的正文里"不存在/占位"全是假阳性，
# 但**拦截/报错**必须留（越界拦截就发生在读类工具上）。
_READ_LIKE = {"read_file", "page_text", "list_files", "glob_files", "search_files"}
_FAIL_SIGNS = (
    ("越界拦截", re.compile(r"路径越界|只允许访问项目目录|安全层|被驳回|被拒绝")),
    ("不存在", re.compile(r"文件不存在|No such file|is not a file")),
    ("HTTP 错误", re.compile(r"HTTP\s*[45]\d\d|返回 HTTP|status[ =:]+[45]\d\d")),
    ("工具报错", re.compile(r"工具执行出错|Traceback|请求失败|连接失败|执行失败|超时")),
    ("被中止", re.compile(r"exit_code=-9|已被用户中止|中止本轮")),
    ("空/占位", re.compile(r"未找到|没有找到|占位|placeholder")),
)
_PATH_RE = re.compile(r"[\w./-]*[\w-]+\.[A-Za-z]{1,6}\b")

_MAX_HINT = 6          # 一次最多给几条失败候选
_DRAFT_CHARS = 320     # 结论草稿取多少字符（有执行动作的条：数字/路径都在里面）
_DRAFT_CHARS_CHAT = 120
_TAIL_CHARS = 200        # 结尾原话取多少字符（"我在等用户拍板"的话通常在这）  # 无工具动作的条：草稿给多了，改写就写成一张清单（实测）
_HINT_CLIP = 90


def _user_turn_items(round_: dict) -> list[tuple[int, str]]:
    """用户发言 ＋ 它在事件流里的位置（轮头记 `-1`），逐字、去重、排除 runtime 信封。

    轮内追加是用户中途的补充/修正（如"含网络检索的先不做"），属前提档：
    丢了它，"为什么这轮以被叫停收尾"就读不懂。
    """
    candidates: list[tuple[int, str]] = [
        (-1, str((round_.get("user_input") or {}).get("original") or ""))
    ]
    for i, e in enumerate(round_.get("events") or []):
        if e.get("type") == "user" and (e.get("message") or {}).get("role") == "user":
            candidates.append((i, str((e.get("message") or {}).get("content") or "")))
    out: list[tuple[int, str]] = []
    for index, text in candidates:
        t = text.strip()
        if not t or t.startswith("<runtime-reminder>") or t in [x[1] for x in out]:
            continue
        out.append((index, t))
    return out


def user_turns(round_: dict) -> list[str]:
    """本轮**全部**用户发言，逐字（轮头 ＋ 轮内追加）。"""
    return [text for _index, text in _user_turn_items(round_)]


def _actions(round_: dict, start: int = 0, end: int | None = None) -> dict:
    """动作清单：工具名×次数、读过/写过/删过的文件、非零退出的命令。

    `start`/`end` 是**事件切片**（一轮多 agent 时只算这一段干的活）。
    """
    events = round_.get("events") or []
    end = len(events) if end is None else end
    tools: dict[str, int] = {}
    files: dict[str, str] = {}
    nonzero: list[str] = []
    pending: dict[str, str] = {}
    for e in events[start:end]:
        message = e.get("message") or {}
        for call in message.get("tool_calls") or []:
            fn = call.get("function") or {}
            name = str(fn.get("name") or "")
            if name:
                tools[name] = tools.get(name, 0) + 1
            pending[str(call.get("id") or "")] = str(fn.get("arguments") or "")
        if message.get("role") == "tool":
            args = pending.get(str(message.get("tool_call_id") or ""), "")
            code = re.search(r"exit_code=(-?\d+)", str(message.get("content") or ""))
            if code and code.group(1) != "0":
                cmd = re.search(r'"command"\s*:\s*"([^"]{0,50})', args)
                nonzero.append(f"{cmd.group(1) if cmd else args[:30]}(exit={code.group(1)})")
    for block in blocks_module.segment_round_by_file(round_):
        if block.get("kind") != "file" or not block.get("file"):
            continue
        if int(block.get("start") or 0) >= end or int(block.get("end") or 0) <= start:
            continue          # 块不与这一段相交
        ops = {str(o.get("op")) for o in (block.get("ops") or [])}
        mark = "".join(sorted({
            "write": "写", "edit": "改", "read": "读", "delete": "删",
        }.get(op, "") for op in ops)) or "动"
        files[str(block["file"])] = mark
    return {"tools": tools, "files": files, "nonzero": nonzero}


def failure_hints(round_: dict, limit: int = _MAX_HINT,
                  start: int = 0, end: int | None = None) -> list[tuple[str, str]]:
    """失败候选（事件 ID, 文本）：把"回忆失败"降级成"核对候选"。切片同 `_actions`。"""
    events = round_.get("events") or []
    end = len(events) if end is None else end
    out: list[tuple[str, str]] = []
    calls: dict[str, str] = {}
    for e in events[start:end]:
        message = e.get("message") or {}
        for call in message.get("tool_calls") or []:
            calls[str(call.get("id") or "")] = str((call.get("function") or {}).get("name") or "")
        if message.get("role") != "tool":
            continue
        name = calls.get(str(message.get("tool_call_id") or ""), "")
        text = str(message.get("content") or "")
        if name in _READ_LIKE:
            if not re.search(r"路径越界|只允许访问|工具执行出错|被驳回|被拒绝", text):
                continue
        elif name.startswith(("run_command", "run_background")):
            code = re.search(r"exit_code=(-?\d+)", text)
            if not code or code.group(1) == "0":
                continue
        flat = text.replace("\n", " ")
        for label, pattern in _FAIL_SIGNS:
            hit = pattern.search(flat)
            if not hit:
                continue
            snippet = flat[max(0, hit.start() - 20):hit.end() + 50].strip()
            item = (str(e.get("id") or ""), f"{label}：{snippet}")
            if item[1][:_HINT_CLIP] not in {x[1][:_HINT_CLIP] for x in out}:
                out.append(item)
            break
    return out[:limit]


def conclusion_draft(round_: dict, limit: int = _DRAFT_CHARS,
                     start: int = 0, end: int | None = None) -> str:
    """结论草稿：这一段里的最终回答首段（模型改写，不照抄）。"""
    events = round_.get("events") or []
    end = len(events) if end is None else end
    for e in reversed(events[start:end]):
        if e.get("type") == "final_answer":
            text = str((e.get("message") or {}).get("content") or "").replace("\n", " ").strip()
            return text[:limit] + ("…" if len(text) > limit else "")
    return ""


def executor_segments(round_: dict, lookup: dict | None = None) -> list[dict]:
    """本轮的执行者分段（**机械派生**，判据复用 `views.event_owners`）。

    一轮由多家执行时（`route_to` 换手），谁是哪一段由事件流本身给出——不必在轮上
    另存交接锚点（§views）。返回 `[{executor, start, end, index, total}]`，
    同一执行方连续的事件合成一段；换手前的那次 `route_to` 调用算前一段的。
    """
    owners = views_module.event_owners(round_, lookup or {})
    out: list[dict] = []
    for i, owner in enumerate(owners):
        name = str(owner or "Main")
        if out and out[-1]["executor"] == name:
            out[-1]["end"] = i + 1
        else:
            out.append({"executor": name, "start": i, "end": i + 1})
    total = len(out)
    for i, seg in enumerate(out, 1):
        seg["index"], seg["total"] = i, total
    return out


def conclusion_tail(round_: dict, limit: int = _TAIL_CHARS,
                    start: int = 0, end: int | None = None) -> str:
    """这一段最终回答的**结尾**（最后 limit 字）——"我最后跟用户说了什么/问了他什么"。

    为什么单独给（2026-09-17 用户口径："注意处理好这种的前面连续性问题，防止脱节"）：
    结论草稿只取**开头**，而"等用户拍板"的话在**结尾**；取不到它，下一轮就接不上上一轮的话头
    （实测：结尾问"要不要让 B 合进去？"，下一轮却当成新活重新查）。
    """
    events = round_.get("events") or []
    end = len(events) if end is None else end
    for e in reversed(events[start:end]):
        if e.get("type") != "final_answer":
            continue
        text = str((e.get("message") or {}).get("content") or "").replace("\n", " ").strip()
        return text[-limit:]
    return ""


def anchor_lines(round_: dict, segment: dict | None = None) -> list[str]:
    """给结算调用的锚（逐行，喂进指令尾部）。

    `segment` 给定时只写**这一段**：动作清单、失败候选、结论草稿都只算这一段的事件
    （一轮多 agent 时，每段各自的交班记录）。
    """
    seq = int(round_.get("seq") or 0)
    events = round_.get("events") or []
    start = int((segment or {}).get("start") or 0)
    end = (segment or {}).get("end")
    end = len(events) if end is None else int(end)
    executor = str((segment or {}).get("executor") or round_.get("active_view") or "Main")
    head = f"R{seq}（执行者：{executor}）"
    if segment is not None and int(segment.get("total") or 1) > 1:
        head += (f"　本回合第 {segment.get('index')}/{segment.get('total')} 段"
                 f"（事件 {_event_span(events, start, end)}）——只写这一段")
    lines = [head]
    # 轮头那句用户原话不进锚（原话由代码逐字摆在装配里，写进来容易招复述）；
    # 只给**轮内追加**——那是中途的补充/修正，按段落归位。
    for index, extra in _user_turn_items(round_):
        if index < 0 or (segment is not None and not (start <= index < end)):
            continue
        lines.append(f"  轮内用户追加：{extra[:200]}")
    actions = _actions(round_, start, end)
    if not actions["tools"]:
        lines.append("  无工具动作——一两句说完谈了什么、得出什么（不列清单、不抄草稿）")
    else:
        lines.append("  工具：" + "、".join(
            f"{name}×{count}" for name, count in sorted(actions["tools"].items())
        ))
        if actions["files"]:
            lines.append("  文件：" + "；".join(
                f"{mark} {path}" for path, mark in list(actions["files"].items())[:12]
            ))
        if actions["nonzero"]:
            lines.append("  非零退出：" + "；".join(actions["nonzero"][:3]))
        hints = failure_hints(round_, start=start, end=end)
        if hints:
            lines.append("  失败候选（逐条核对，同类可合并）：")
            lines += [f"    - 〔{eid}〕{text}" for eid, text in hints]
    # 草稿给多长，改写就写多长（实测：闲聊轮的 320 字草稿直接被铺成一张清单），
    # 故无工具动作的条只给开头一截。
    draft = conclusion_draft(
        round_, limit=_DRAFT_CHARS if actions["tools"] else _DRAFT_CHARS_CHAT,
        start=start, end=end,
    )
    if draft:
        lines.append(f"  结论草稿（改写成一句话，别照抄）：{draft}")
    tail = conclusion_tail(round_, start=start, end=end)
    if tail:
        lines.append(f"  结尾原话（若在等用户拍板，写进 awaiting_user）：{tail}")
    return lines


def _event_span(events: list, start: int, end: int) -> str:
    """事件区间的 ID 写法（`R5-E12–R5-E20`）；空区间给"（无事件）"。"""
    window = events[start:end]
    if not window:
        return "无事件"
    first = str(window[0].get("id") or "")
    last = str(window[-1].get("id") or "")
    return first if first == last else f"{first}–{last}"


def seq_span(seqs: list[int]) -> str:
    """轮号区间的人读写法：`R1–R8` / `R1、R3、R5`。"""
    if not seqs:
        return "（空批）"
    if len(seqs) == 1:
        return f"R{seqs[0]}"
    if seqs == list(range(seqs[0], seqs[-1] + 1)):
        return f"R{seqs[0]}–R{seqs[-1]}"
    return "、".join(f"R{s}" for s in seqs)


def batch_anchor_lines(batch: list[dict]) -> list[str]:
    """**分裂前**的攒批锚：一批轮逐轮列出（轮号 ＋ 执行者 ＋ 机械事实）。

    分裂后不再攒批——一轮一整理、一轮多 agent 就每段一条，锚由 `anchor_lines(r, seg)`
    单条给出（见 `maintenance._settle_note_call`）。
    """
    lines = [f"这批 {len(batch)} 轮：{seq_span([int(r.get('seq') or 0) for r in batch])}"]
    for r in batch:
        lines.append("")
        lines += anchor_lines(r)
    return lines


def parse_notes(ordered: list, batch: list[dict],
                segments: dict[int, dict] | None = None) -> tuple[list[dict], list[str]]:
    """从响应里取产物并按 seq 对上批次；返回 (产物, 诊断行)。硬门只判结构。

    "条数与轮号一一对应"由条目自己保证：没对上的轮**没有产物**，由调用方按
    "没有产出"处理（原文继续顶着，下一批再收它）。诊断行只留痕。

    `segments`（seq → 分段）用于**一轮多 agent**：执行者与事件区间由代码按分段盖章
    （不让模型自报身份），产物里带上 `start`/`end` 便于分段归位。
    """
    raw = None
    for call in ordered or []:
        if str(call.get("name") or "") != _SUBMIT_TOOL:
            continue
        try:
            raw = json.loads(call.get("arguments") or "")
        except (TypeError, ValueError):
            return [], ["产物不是合法 JSON"]
        break
    if not isinstance(raw, dict):
        return [], [f"没有调用 {_SUBMIT_TOOL}（没有产出）"]
    items = raw.get("notes")
    if not isinstance(items, list):
        return [], ["notes 不是数组"]
    ledger = raw.get("ledger_append") if isinstance(raw.get("ledger_append"), dict) else {}
    by_seq = {int(r.get("seq") or 0): r for r in batch}
    out: list[dict] = []
    defects: list[str] = []
    seen: set[int] = set()
    for item in items:
        if not isinstance(item, dict):
            defects.append("有一条产物不是对象")
            continue
        try:
            seq = int(item.get("seq"))
        except (TypeError, ValueError):
            defects.append("有条产物的 seq 不是整数")
            continue
        round_ = by_seq.get(seq)
        if round_ is None:
            defects.append(f"产物里的 R{seq} 不在这批轮里")
            continue
        if seq in seen:
            defects.append(f"R{seq} 有两条产物，取第一条")
            continue
        sentence = str(item.get("sentence") or "").strip()
        if not sentence:
            defects.append(f"R{seq} 句子为空")
            continue
        seen.add(seq)
        failures: list[dict] = []
        for entry in item.get("failures") or []:
            if isinstance(entry, dict):
                text = str(entry.get("text") or "").strip()
                evidence = str(entry.get("evidence") or "").strip()
            else:
                text, evidence = str(entry).strip(), ""
            if text:
                failures.append({"text": text, "evidence": evidence})
        seg = (segments or {}).get(seq)
        product = {"seq": seq, "sentence": sentence, "failures": failures,
                   "awaiting_user": str(item.get("awaiting_user") or "").strip(),
                   "ledger_append": ledger,
                   "executor": str((seg or {}).get("executor")
                                   or round_.get("active_view") or "Main")}
        if seg is not None:
            product["start"], product["end"] = int(seg["start"]), int(seg["end"])
        out.append(product)
    return out, defects


# 账本条目的来源戳：`（R14·A）`——谁在哪一轮加的一目了然；去重与匹配都按**去掉戳的正文**比。
_STAMP_RE = re.compile(r"（R(\d+)·[^）]*）\s*$")
_LEDGER_FIELDS = ("constraints", "decisions", "known_issues", "open_questions")


def core_text(text: str) -> str:
    """去掉来源戳后的正文（去重、匹配、子串结案都按它比）。"""
    return _STAMP_RE.sub("", str(text)).strip()


def stamp_round(text: str) -> int:
    """从来源戳里读回合号（没有戳 → 0）。"""
    hit = _STAMP_RE.search(str(text))
    return int(hit.group(1)) if hit else 0


def build_ledger_patch(ledger_append: dict, seq: int, executor: str,
                       existing: dict) -> tuple[dict, list[str]]:
    """把本轮产物的账本增量变成 `apply_state_patch` 的补丁（带来源戳、按正文去重）。

    结案（`closed`）在这里先过一道**越界保护**：只能结掉**更早轮次**留下的条目
    （戳里的轮号 < 本轮）——否则本轮就会把自己或别人刚写的条目删掉。
    返回 (patch, 留痕说明)。
    """
    patch: dict = {}
    notes: list[str] = []
    merged: dict = {}
    for field in _LEDGER_FIELDS:
        items = (ledger_append or {}).get(field)
        if not isinstance(items, list):
            continue
        have = {core_text(x) for x in (existing.get(field) or [])}
        added: list[str] = []
        for item in items:
            core = core_text(item)
            if not core or core in have:
                continue
            have.add(core)
            added.append(f"{core}（R{seq}·{executor}）")
        if added:
            merged[field] = added
    if merged:
        patch.update(merged)
        notes.append("新增 " + "、".join(f"{k} {len(v)} 条" for k, v in merged.items()))
    wanted = (ledger_append or {}).get("closed")
    if isinstance(wanted, list) and wanted:
        keep: list[dict] = []
        for entry in wanted:
            if not isinstance(entry, dict):
                continue
            field = str(entry.get("field") or "").strip()
            match = str(entry.get("match") or "").strip()
            if field not in _LEDGER_FIELDS or not match:
                continue
            hits = [x for x in (existing.get(field) or []) if match in x]
            if not hits:
                if any(match in x for x in merged.get(field) or []):
                    notes.append(f"结案被拒（{field}：{match[:20]}）——本轮刚新增的条目，"
                                 "不能同轮结案")
                else:
                    notes.append(f"结案未命中（{field}：{match[:20]}）——账本里没有这一段，不动")
                continue
            if len(hits) > 1:
                notes.append(f"结案歧义（{field}：{match[:20]}）——命中 {len(hits)} 条，不动")
                continue
            owner_round = stamp_round(hits[0])
            if owner_round and owner_round >= seq:
                notes.append(f"结案被拒（{field}：{match[:20]}）——该条来自 R{owner_round}，"
                             f"不比本轮早；只允许结更早轮次的条目")
                continue
            keep.append({"field": field, "match": match})
        if keep:
            patch["closed"] = keep
            notes.append(f"结案 {len(keep)} 条")
    return patch, notes


def user_slot(round_: dict) -> str:
    """该轮槽位的"用户侧"内容：**全部**用户发言逐字（轮头 ＋ 轮内追加）。"""
    turns = user_turns(round_)
    if not turns:
        return "（本轮无用户发言）"
    lines = [f"👤 {turns[0]}"]
    lines += [f"👤（轮内追加）{t}" for t in turns[1:]]
    return "\n".join(lines)


def note_segments(round_: dict) -> list[dict]:
    """该轮已落地的段落（按事件顺序）：一轮多 agent 时每家一段。

    兼容两种形态：`note_segments`（分段落地，带 `start`/`end`）与早先的单条 `note`。
    """
    segs = round_.get("note_segments")
    if isinstance(segs, list) and segs:
        return sorted(
            [s for s in segs if isinstance(s, dict) and str(s.get("sentence") or "").strip()],
            key=lambda x: int(x.get("start") or 0),
        )
    note = round_.get("note")
    return [note] if isinstance(note, dict) and str(note.get("sentence") or "").strip() else []


def render_note(round_: dict) -> str:
    """段落档：`[R{n}]〔执行者〕一句话` ＋ 失败行（执行者非 Main 才显示，见 §3.2）。

    一轮多 agent 时**分段拼接**：每段一行，按事件顺序。
    """
    lines: list[str] = []
    for seg in note_segments(round_):
        head = f"[R{round_.get('seq')}]"
        executor = str(seg.get("executor") or "")
        if executor and executor != "Main":
            head += f"〔{executor}〕"
        lines.append(f"{head} {seg.get('sentence') or ''}")
        awaiting = str(seg.get("awaiting_user") or "").strip()
        if awaiting:
            # **等你答复**：这一轮结尾把选项摆给用户了——下一轮隔着一句"话"也要接得上（防脱节）
            lines.append(f"  ⏳ 等你答复：{awaiting}")
        for item in seg.get("failures") or []:
            evidence = str(item.get("evidence") or "")
            lines.append(f"  ⚠ {item.get('text')}" + (f"　〔{evidence}〕" if evidence else ""))
    return "\n".join(lines)


def est_note_tokens(round_: dict) -> int:
    """该轮折成段落后的体量估算（用户侧逐字原话 ＋ 段落）。"""
    return tokens_module.estimate(user_slot(round_)) + tokens_module.estimate(render_note(round_))


def soft_defects(note: dict, round_: dict, segment: dict | None = None) -> list[str]:
    """软档：内容正确性（只留痕，不拦）。提到这一段没出现过的路径/工具、证据 ID 不存在。"""
    out: list[str] = []
    text = note["sentence"] + " " + " ".join(f["text"] for f in note["failures"])
    events = round_.get("events") or []
    start = int(note.get("start", (segment or {}).get("start") or 0))
    end = int(note.get("end", (segment or {}).get("end") or len(events)))
    scope = events[start:end] if segment is not None or note.get("start") is not None else events
    allowed_paths = {
        m.group(0)
        for m in _PATH_RE.finditer(json.dumps(scope, ensure_ascii=False))
    }

    def known_path(token: str) -> bool:
        if any(a == token or a.endswith("/" + token) for a in allowed_paths):
            return True
        return all(any(a == part or a.endswith("/" + part) for a in allowed_paths)
                   for part in token.split("/") if part)

    for token in {m.group(0) for m in _PATH_RE.finditer(text)}:
        if not known_path(token):
            out.append(f"提到本轮没出现过的路径 `{token}`")
    tools = set(_actions(round_, start, end)["tools"])
    for name in ("run_command", "write_file", "edit_file", "list_background",
                 "stop_background", "web_search", "search_files"):
        if name in text and name not in tools:
            out.append(f"提到本轮没调用的工具 `{name}`")
    ids = {str(e.get("id") or "") for e in scope}
    for item in note["failures"]:
        if item["evidence"] and item["evidence"] not in ids:
            out.append(f"证据 ID 不存在于本轮 `{item['evidence']}`")
    return out
