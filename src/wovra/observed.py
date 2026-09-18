"""观察快照与"文件变更"通知（V4 §3.7，2026-09-17 用户口径）。

> "读文件后，如果发生改变，下次输入指令时，将其当作提示，注入到用户内容上面，
> 告诉其之前读过的文件进行了修改，修改了哪些行。方便其快速少量的更新。"

三件事，全部零 LLM：

1. **观察快照**（内容寻址）：agent 每次观察（读/写）一个文件，就把当时的内容存一份到
   `.wovra/observed/<view>/<路径__>/<hash>.txt`，索引写进 `<view>/index.json`。
   与 `restore_file` 的版本档案（`.wovra/history/`）**分开**：那个是"可回滚的旧版本"，
   这个是"你上次看到的内容"，目的不同。
2. **写入日志**：工具写过的文件记一笔（谁、哪一轮）——通知里"是不是工具改的"由它判，
   没有记录就是**用户操作**（外部编辑器改的）。
3. **通知**：`diff(你的观察快照, 现在的内容)` → 只给 hunk（默认 ≤14 行）+ `+a −b 行`，
   末尾固定"够用就别整文件重读"。

**这是"按 agent"的记录**（`view` 一级目录）：V4 分流后同进程里多个 agent 各有自己的观察，
B 写文件不该刷新 A 手里那份的"新鲜度"（旧的进程级全局记录正是这么漏检的）。
"""
from __future__ import annotations

import difflib
import hashlib
import json
import time
from pathlib import Path
from typing import Optional

_OBSERVED_DIR = (".wovra", "observed")
_INDEX = "index.json"
_WRITES = "writes.json"
_MAX_HUNK_LINES = 14
# 每个文件的写入记录保留条数（只用来判"是不是工具写的"，留最近若干条足够）
_WRITE_LOG_KEEP = 20


def _root(workspace: Path) -> Path:
    return Path(workspace).joinpath(*_OBSERVED_DIR)


def _slot(workspace: Path, view: str) -> Path:
    return _root(workspace) / _safe(view or "Main")


def _safe(text: str) -> str:
    """路径/名字的安全目录名（与 `.wovra/history` 同款：`/` 换 `__`）。"""
    return str(text or "").replace("/", "__").replace("\\", "__").strip() or "_"


def _load(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _dump(path: Path, data: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass


def _digest(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:12]


def record(workspace: Path, view: str, rel: str, content: str, *,
           by: str = "read") -> None:
    """把"这个 agent 现在看到的内容"存一份（内容寻址，天然去重）。"""
    rel = str(rel or "").strip()
    if not rel:
        return
    slot = _slot(workspace, view)
    digest = _digest(content)
    target = slot / _safe(rel) / f"{digest}.txt"
    try:
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
    except OSError:
        return
    index = _load(slot / _INDEX)
    index[rel] = {"hash": digest, "at": time.time(), "lines": content.count("\n") + 1,
                  "chars": len(content), "by": by}
    _dump(slot / _INDEX, index)


def note_write(workspace: Path, view: str, rel: str, seq: int = 0,
               task: str = "", content: Optional[str] = None) -> None:
    """记一笔"工具写了这个文件"——通知里用它区分工具写入与别的来源。

    记录**按 (view, rel) 分桶、追加成列表**：同一份工作区可能被多个会话/进程
    同时写（实测踩过），单键覆盖会把别人刚写的记录顶掉。

    判定归属用**内容摘要**（`content` 是工具刚写进去的内容）：当前内容与某条
    记录里的摘要相同 → 就是工具写的。只比时间戳不够稳——观察（读）也会刷新
    "上次看到"的时间，把工具刚写的记录比下去，于是自己的编辑被说成别人改的。
    """
    rel = str(rel or "").strip()
    if not rel:
        return
    path = _root(workspace) / _WRITES
    data = _load(path)
    key = f"{_safe(view or 'Main')}|{rel}"
    entries = data.get(key)
    if isinstance(entries, dict):                 # 旧格式（单条）：升级成列表
        entries = [entries]
    if not isinstance(entries, list):
        entries = []
    entries.append({"view": view or "Main", "seq": int(seq or 0),
                    "task": str(task or ""), "at": time.time(),
                    "hash": _digest(content) if content is not None else ""})
    data[key] = entries[-_WRITE_LOG_KEEP:]
    _dump(path, data)


def _write_entries(writes: dict, rel: str) -> list[dict]:
    """这个文件的写入记录（新格式按 view 分桶，旧格式是单条 dict）。"""
    out: list[dict] = []
    for key, value in (writes or {}).items():
        if str(key).split("|")[-1] != rel and key != rel:
            continue
        if isinstance(value, list):
            out += [v for v in value if isinstance(v, dict)]
        elif isinstance(value, dict):
            out.append(value)
    return out


def snapshot_text(workspace: Path, view: str, rel: str) -> Optional[str]:
    """这个 agent 上次看到的该文件内容；没有快照返回 None。"""
    index = _load(_slot(workspace, view) / _INDEX)
    info = index.get(rel)
    if not isinstance(info, dict):
        return None
    return _observed_text(workspace, view, rel, info)


def stale_report(workspace: Path, view: str, rel: str, now: str, *,
                 limit: int = _MAX_HUNK_LINES) -> str:
    """相对"你上次看到的内容"，这个文件变了哪些行；没有快照或没变返回空串。"""
    old = snapshot_text(workspace, view, rel)
    if old is None or old == now:
        return ""
    hunks, added, removed, truncated = _hunks(old.splitlines(), now.splitlines(), limit)
    if not hunks:
        return ""
    tail = f"\n  …（改动更多，只列前 {limit} 行）" if truncated else ""
    return f"变了这几行（+{added} −{removed}）：\n" + "\n".join(hunks) + tail


def _read(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _observed_text(workspace: Path, view: str, rel: str, info: dict) -> Optional[str]:
    slot = _slot(workspace, view)
    digest = str(info.get("hash") or "")
    if not digest:
        return None
    return _read(slot / _safe(rel) / f"{digest}.txt")


def _hunks(before: list[str], after: list[str], limit: int) -> tuple[list[str], int, int, bool]:
    """行级 hunk（默认 ≤limit 行）。返回 (行, +a, −b, 是否被截断)。"""
    added = removed = 0
    lines: list[str] = []
    truncated = False
    for line in difflib.unified_diff(before, after, n=1, lineterm=""):
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            removed += 1
        elif line.startswith("@@"):
            pass
        if len(lines) >= limit:
            truncated = True
            continue
        lines.append(line.rstrip("\n")[:120])
    return lines, added, removed, truncated


def _history_versions(workspace: Path, rel: str) -> int:
    slot = Path(workspace) / ".wovra" / "history" / _safe(rel)
    try:
        return len(list(slot.glob("*.bak"))) if slot.is_dir() else 0
    except OSError:
        return 0


def changes(workspace: Path, view: str, *, limit: int = _MAX_HUNK_LINES) -> list[str]:
    """这个 agent 手里哪些文件被改过（行级），渲染成通知块（零 LLM）。

    只报**这个 view 观察过**的文件；判断"谁改的"看写入日志：有工具写入记录 → 工具写入，
    没有 → **用户操作**（外部编辑器/别的会话改的）。
    """
    slot = _slot(workspace, view)
    index = _load(slot / _INDEX)
    if not index:
        return []
    writes = _load(_root(workspace) / _WRITES)
    out: list[str] = []
    for rel, info in sorted(index.items()):
        target = Path(workspace) / rel
        now = _read(target)
        if now is None:
            continue                       # 文件没了（删除也算变更，但通知只讲"改了哪些行"）
        old = _observed_text(workspace, view, rel, info if isinstance(info, dict) else {})
        if old is None or old == now:
            continue
        old_lines = old.splitlines()
        now_lines = now.splitlines()
        hunks, added, removed, truncated = _hunks(old_lines, now_lines, limit)
        # 「是谁改的」只有两种可判定的来源：**工具写入**（当前内容与某条写入
        # 记录的内容摘要相同，或写入记录落在观察之后）与**别的来源**（外部编辑器、
        # 别的会话、另一个进程——本进程的日志判不出它是谁，只知道自己没写）。
        # 不要断言成"用户操作"。
        observed_at = float((info or {}).get("at") or 0)
        now_hash = _digest(now)
        entries = _write_entries(writes, rel)
        by_tool = any(
            (str(e.get("hash") or "") and str(e.get("hash")) == now_hash)
            or float(e.get("at") or 0) >= observed_at
            for e in entries
        )
        who = (f"工具写入（{entries[-1].get('view') or '某个 agent'}）"
               if by_tool else "非工具写入")
        head = (f"[文件变更·{who}] {rel}"
                f"　你在上次观察之后它被改过：+{added} −{removed} 行"
                f"（{len(old_lines)} → {len(now_lines)} 行）")
        body = [head]
        body += hunks
        if truncated:
            body.append(f"  …（改动更多，只列前 {limit} 行；要看全文用 read_file）")
        versions = _history_versions(workspace, rel)
        if versions >= 2:
            body.append(f"  （该文件共 {versions} 份历史版本，更早的改动可能也影响你手里的内容）")
        if by_tool:
            body.append("  （只要改动处就够用，别整文件重读；要全文再 read_file）")
        else:
            body.append(
                "  **不是本会话的工具写的**——可能是用户手改，也可能是另一个会话/进程"
                "在同一份工作区里改（并行干活时常见）；**不要当成权威版本，也不要当成错误**，"
                "先看差异再决定：顺着它改、改回去、还是先问一句。"
            )
        out.append("\n".join(body))
    return out


def notice_text(workspace: Path, view: str, *, limit: int = _MAX_HUNK_LINES) -> str:
    """轮首注入用的整块文本（空串 ＝ 没有变更，不注入）。"""
    blocks = changes(workspace, view, limit=limit)
    return "\n\n".join(blocks)
