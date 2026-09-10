"""文件生命周期账本（零 LLM，确定性）：从事件流推导每个文件的状态。

讨论v2 机制的运行时底座：LIVE / HISTORICAL / DEAD / READ_ONLY 是磁盘
事实（写了没删 = live，删了 = dead，只读没写 = read_only），不需要
模型判断——模型只负责"这属于哪个架构节点 / 为什么废弃"这类语义。
本模块把确定性部分先落地：

    * update(round)：扫一轮事件，登记每个文件的读/写/改/删与版本；
    * state_of / live_files / live_count：状态查询（分裂上限 =
      live 文件数，规则在分裂模块，账本只提供事实）；
    * sync_disk(exists)：可选磁盘对账——被 run_command rm 掉而
      没有 delete_file 事件的文件，据此翻成 dead。

版本历史（versions）= 该文件的写/改事件序列，末位即当前有效内容；
DEAD 结论槽（conclusion）留空，由整理（LLM）在归档时填充——本模块
不碰语义。
"""

from __future__ import annotations

from typing import Callable, Optional

READ_TOOLS = ("read_file",)
WRITE_TOOLS = ("write_file",)  # 整体写入/新建
EDIT_TOOLS = ("edit_file", "replace_lines")  # 局部修改
DELETE_TOOLS = ("delete_file",)
FILE_TOOLS = READ_TOOLS + WRITE_TOOLS + EDIT_TOOLS + DELETE_TOOLS

_OP_OF = {
    **{t: "read" for t in READ_TOOLS},
    **{t: "write" for t in WRITE_TOOLS},
    **{t: "edit" for t in EDIT_TOOLS},
    **{t: "delete" for t in DELETE_TOOLS},
}

# delete_file 的失败返回标记（用户拒绝 / 文件不存在 / 目标是目录）——
# 命中任一即不标记 dead（删除没真发生，账本不能记假死）。
_DELETE_FAIL_MARKERS = ("拒绝", "不存在", "是目录")

STATE_LIVE = "live"
STATE_DEAD = "dead"
STATE_READ_ONLY = "read_only"


def _call_info(call: dict) -> tuple[str, dict]:
    """tool_call → (工具名, 参数 dict)；参数解析失败按空参处理。"""
    fn = call.get("function") or {}
    import json

    try:
        args = json.loads(fn.get("arguments") or "{}")
    except json.JSONDecodeError:
        args = {}
    if not isinstance(args, dict):
        args = {}
    return str(fn.get("name") or ""), args


def _empty_entry(path: str, event_id: str) -> dict:
    return {
        "path": path,
        "state": STATE_READ_ONLY,  # 未见写之前按只读计；写入后翻 live
        "first_seen": event_id,
        "last_write": None,
        "last_read": None,
        "write_count": 0,
        "read_count": 0,
        "versions": [],      # 写/改事件序列，末位 = 当前有效内容
        "block_refs": [],    # 该文件的块 ID（segment_round_by_file 产出）
        "deleted_at": None,
        "conclusion": None,  # DEAD 结论槽，LLM 整理时填充
    }


class FileLedger:
    """文件生命周期账本：update 逐轮增量维护，状态纯规则推导。"""

    def __init__(self, entries: Optional[dict] = None) -> None:
        self._by_path: dict[str, dict] = entries or {}

    # ---- 增量维护 ---------------------------------------------------------

    def update(self, round_: dict, blocks: Optional[list] = None) -> list[str]:
        """扫一轮事件，登记文件交互；返回本轮触及的文件路径列表。

        blocks 可选：segment_round_by_file 的产物，用于登记 block_refs
        （文件 → 块 的跨轮索引，分裂拼装时直接用）。
        删除按结果判定：delete_file 被用户拒绝/文件不存在时不算死亡
        （读结果文本，零 LLM）。
        """
        touched: list[str] = []
        pending_deletes: dict[str, dict] = {}  # call_id → {path, event}
        results: dict[str, str] = {}           # call_id → 结果文本

        for event in round_.get("events") or []:
            message = event.get("message") or {}
            etype = event.get("type")
            if etype == "tool_call":
                for call in message.get("tool_calls") or []:
                    name, args = _call_info(call)
                    op = _OP_OF.get(name)
                    if op is None:
                        continue
                    path = str(args.get("path") or "")
                    if not path:
                        continue
                    eid = str(event.get("id") or "")
                    if op == "delete":
                        pending_deletes[str(call.get("id") or "")] = {
                            "path": path, "event": eid,
                        }
                    else:
                        self._apply(path, op, eid)
                    if path not in touched:
                        touched.append(path)
            elif etype == "tool_result":
                results[str(message.get("tool_call_id") or "")] = str(
                    message.get("content") or ""
                )

        for call_id, info in pending_deletes.items():
            content = results.get(call_id) or ""
            if not any(m in content for m in _DELETE_FAIL_MARKERS):
                self._apply(info["path"], "delete", info["event"])

        if blocks:
            for b in blocks:
                if b.get("kind") == "file" and b.get("file"):
                    entry = self._by_path.setdefault(
                        b["file"], _empty_entry(b["file"], b["start_event"])
                    )
                    if b["id"] not in entry["block_refs"]:
                        entry["block_refs"].append(b["id"])
        return touched

    def _apply(self, path: str, op: str, event_id: str) -> None:
        entry = self._by_path.setdefault(path, _empty_entry(path, event_id))
        if op == "read":
            entry["read_count"] += 1
            entry["last_read"] = event_id
        elif op in ("write", "edit"):
            entry["write_count"] += 1
            entry["last_write"] = event_id
            entry["versions"].append(event_id)
            entry["state"] = STATE_LIVE  # 写了就是活的，删除才翻 dead
            entry["deleted_at"] = None
        elif op == "delete":
            entry["state"] = STATE_DEAD
            entry["deleted_at"] = event_id

    def sync_disk(self, exists: Callable[[str], bool]) -> list[str]:
        """磁盘对账：写了但磁盘上已不存在的文件翻 dead（rm 没走
        delete_file 事件）；存在则保持/恢复 live。返回状态变化路径。"""
        changed = []
        for path, entry in self._by_path.items():
            on_disk = exists(path)
            if entry["write_count"] > 0 and not on_disk:
                if entry["state"] != STATE_DEAD:
                    entry["state"] = STATE_DEAD
                    changed.append(path)
            elif on_disk and entry["state"] == STATE_DEAD:
                entry["state"] = STATE_LIVE
                changed.append(path)
        return changed

    # ---- 查询 --------------------------------------------------------------

    def state_of(self, path: str) -> str:
        entry = self._by_path.get(path)
        return entry["state"] if entry else STATE_READ_ONLY

    def live_files(self) -> list[str]:
        """当前 LIVE 文件（按首次出现顺序）——分裂子 agent 数量上限的输入。"""
        return [p for p, e in self._by_path.items() if e["state"] == STATE_LIVE]

    def live_count(self) -> int:
        return len(self.live_files())

    def entries(self) -> dict[str, dict]:
        return dict(self._by_path)

    def to_dict(self) -> dict:
        return {p: dict(e) for p, e in self._by_path.items()}
