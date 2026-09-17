"""每轮一段话：锚（机械材料）、解析与校验（V4 §3.2，第一步）。

这一步只做"写"：轮闭合时同步产出一条**每轮一段话**（一句话 ＋ 失败 ＋ 账本增量）。
**不改装配、不改归因**——产物先只落盘与进账本，装配仍走原来的整理链路。

分工（§3.2）：
* **代码给锚**：轮内用户发言、动作清单（工具×次数、读写删过的文件、非零退出）、失败候选
  （带事件 ID）、结论草稿——这些原本要模型从上百 K 事件里回忆，是纯思考；
* **模型写一句话**：只写这一轮自己那份交班记录；
* **代码盖章**：执行者（该轮 `active_view`，不让模型自报身份）。

校验分两档（§154/§155）：**硬门只剩结构**（有产物 / seq 对得上 / 句子非空），
**内容正确性一律软档**（只留痕）；**产物一律落地**，失败只留痕、不重发、不跨轮。
"""
from __future__ import annotations

import json
import re

from .. import blocks as blocks_module

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
_DRAFT_CHARS = 320     # 结论草稿取多少字符
_HINT_CLIP = 90


def user_turns(round_: dict) -> list[str]:
    """本轮**全部**用户发言，逐字（轮头 ＋ 轮内追加），去重并排除 runtime 信封。

    轮内追加是用户中途的补充/修正（如"含网络检索的先不做"），属前提档：
    丢了它，"为什么这轮以被叫停收尾"就读不懂。
    """
    out: list[str] = []
    candidates = [str((round_.get("user_input") or {}).get("original") or "")]
    candidates += [
        str((e.get("message") or {}).get("content") or "")
        for e in round_.get("events") or []
        if e.get("type") == "user" and (e.get("message") or {}).get("role") == "user"
    ]
    for text in candidates:
        t = text.strip()
        if not t or t.startswith("<runtime-reminder>") or t in out:
            continue
        out.append(t)
    return out


def _round_actions(round_: dict) -> dict:
    """本轮动作清单：工具名×次数、读过/写过/删过的文件、非零退出的命令。"""
    tools: dict[str, int] = {}
    files: dict[str, str] = {}
    nonzero: list[str] = []
    pending: dict[str, str] = {}
    for e in round_.get("events") or []:
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
        ops = {str(o.get("op")) for o in (block.get("ops") or [])}
        mark = "".join(sorted({
            "write": "写", "edit": "改", "read": "读", "delete": "删",
        }.get(op, "") for op in ops)) or "动"
        files[str(block["file"])] = mark
    return {"tools": tools, "files": files, "nonzero": nonzero}


def failure_hints(round_: dict, limit: int = _MAX_HINT) -> list[tuple[str, str]]:
    """失败候选（事件 ID, 文本）：把"回忆失败"降级成"核对候选"。"""
    out: list[tuple[str, str]] = []
    calls: dict[str, str] = {}
    for e in round_.get("events") or []:
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


def conclusion_draft(round_: dict, limit: int = _DRAFT_CHARS) -> str:
    """结论草稿：本轮最终回答的首段（模型改写，不照抄）。"""
    for e in reversed(round_.get("events") or []):
        if e.get("type") == "final_answer":
            text = str((e.get("message") or {}).get("content") or "").replace("\n", " ").strip()
            return text[:limit] + ("…" if len(text) > limit else "")
    return ""


def anchor_lines(round_: dict) -> list[str]:
    """给结算调用的锚（逐行，喂进指令尾部）。"""
    seq = int(round_.get("seq") or 0)
    lines = [f"R{seq}（执行者：{round_.get('active_view') or 'Main'}）"]
    for extra in user_turns(round_)[1:]:
        lines.append(f"  轮内用户追加：{extra[:200]}")
    actions = _round_actions(round_)
    if not actions["tools"]:
        lines.append("  无工具动作（纯对话轮）——只写谈了什么、结论是什么")
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
        hints = failure_hints(round_)
        if hints:
            lines.append("  失败候选（逐条核对，同类可合并）：")
            lines += [f"    - 〔{eid}〕{text}" for eid, text in hints]
    draft = conclusion_draft(round_)
    if draft:
        lines.append(f"  结论草稿（改写成一句话，别照抄）：{draft}")
    return lines


def parse_note(ordered: list, round_: dict) -> tuple[dict | None, str]:
    """从响应里取产物并归一化；返回 (note, 失败原因)。硬门只判结构。"""
    raw = None
    for call in ordered or []:
        if str(call.get("name") or "") != "submit_round_note":
            continue
        try:
            raw = json.loads(call.get("arguments") or "")
        except (TypeError, ValueError):
            return None, "产物不是合法 JSON"
        break
    if not isinstance(raw, dict):
        return None, "没有调用 submit_round_note（没有产出）"
    sentence = str(raw.get("sentence") or "").strip()
    if not sentence:
        return None, "句子为空"
    try:
        seq = int(raw.get("seq"))
    except (TypeError, ValueError):
        return None, "seq 不是整数"
    if seq != int(round_.get("seq") or 0):
        return None, f"seq 对不上（产物 R{seq}，本轮 R{round_.get('seq')}）"
    failures: list[dict] = []
    for item in raw.get("failures") or []:
        if isinstance(item, dict):
            text = str(item.get("text") or "").strip()
            evidence = str(item.get("evidence") or "").strip()
        else:
            text, evidence = str(item).strip(), ""
        if text:
            failures.append({"text": text, "evidence": evidence})
    ledger = raw.get("ledger_append") if isinstance(raw.get("ledger_append"), dict) else {}
    return (
        {"seq": seq, "sentence": sentence, "failures": failures,
         "ledger_append": ledger, "executor": round_.get("active_view") or "Main"},
        "",
    )


def user_slot(round_: dict) -> str:
    """该轮槽位的"用户侧"内容：**全部**用户发言逐字（轮头 ＋ 轮内追加）。"""
    turns = user_turns(round_)
    if not turns:
        return "（本轮无用户发言）"
    lines = [f"👤 {turns[0]}"]
    lines += [f"👤（轮内追加）{t}" for t in turns[1:]]
    return "\n".join(lines)


def render_note(round_: dict) -> str:
    """段落档：`[R{n}]〔执行者〕一句话` ＋ 失败行（执行者非 Main 才显示，见 §3.2）。"""
    note = round_.get("note") or {}
    head = f"[R{round_.get('seq')}]"
    executor = str(note.get("executor") or "")
    if executor and executor != "Main":
        head += f"〔{executor}〕"
    lines = [f"{head} {note.get('sentence') or ''}"]
    for item in note.get("failures") or []:
        evidence = str(item.get("evidence") or "")
        lines.append(f"  ⚠ {item.get('text')}" + (f"　〔{evidence}〕" if evidence else ""))
    return "\n".join(lines)


def soft_defects(note: dict, round_: dict) -> list[str]:
    """软档：内容正确性（只留痕，不拦）。提到本轮没出现过的路径/工具、证据 ID 不存在。"""
    out: list[str] = []
    text = note["sentence"] + " " + " ".join(f["text"] for f in note["failures"])
    own = _round_actions(round_)
    allowed_paths = {
        m.group(0)
        for m in _PATH_RE.finditer(json.dumps(round_.get("events") or [], ensure_ascii=False))
    }

    def known_path(token: str) -> bool:
        if any(a == token or a.endswith("/" + token) for a in allowed_paths):
            return True
        return all(any(a == part or a.endswith("/" + part) for a in allowed_paths)
                   for part in token.split("/") if part)

    for token in {m.group(0) for m in _PATH_RE.finditer(text)}:
        if not known_path(token):
            out.append(f"提到本轮没出现过的路径 `{token}`")
    tools = {name for r in [round_] for name in _round_actions(r)["tools"]}
    for name in ("run_command", "write_file", "edit_file", "list_background",
                 "stop_background", "web_search", "search_files"):
        if name in text and name not in tools:
            out.append(f"提到本轮没调用的工具 `{name}`")
    ids = {str(e.get("id") or "") for e in round_.get("events") or []}
    for item in note["failures"]:
        if item["evidence"] and item["evidence"] not in ids:
            out.append(f"证据 ID 不存在于本轮 `{item['evidence']}`")
    return out
