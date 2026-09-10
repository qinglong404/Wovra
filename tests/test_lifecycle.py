"""文件生命周期账本的离线测试：纯规则、零 LLM、全确定性。"""

import json

from wovra import blocks, lifecycle


def _event(seq: int, n: int, etype: str, tool: str = "", args: dict | None = None,
           content: str = "") -> dict:
    """构造最小事件（复用 test_blocks 的约定：call id c{n}，结果回 c{n-1}）。"""
    if etype == "tool_call":
        message = {"role": "assistant", "content": "", "tool_calls": [{
            "id": f"c{n}", "type": "function",
            "function": {"name": tool, "arguments": json.dumps(args or {})},
        }]}
    elif etype == "tool_result":
        message = {"role": "tool", "tool_call_id": f"c{n - 1}", "content": content}
    else:
        message = {"role": "user" if etype == "user" else "assistant",
                   "content": content}
    return {"id": f"R{seq}-E{n:02d}", "type": etype, "message": message,
            "truncated": "", "status": ""}


def _round(seq: int, events: list) -> dict:
    return {"seq": seq, "events": events, "end_state": "completed",
            "user_input": {"original": "", "normalized": ""}, "refined_index": {}}


def test_lifecycle_states_derived_from_events():
    """写→live，改→live 且版本追加，只读→read_only，删除→dead。"""
    ledger = lifecycle.FileLedger()
    ledger.update(_round(1, [
        _event(1, 1, "user", content="建文件"),
        _event(1, 2, "tool_call", tool="write_file",
               args={"path": "a.js", "content": "x"}),
        _event(1, 3, "tool_result", content="ok"),
        _event(1, 4, "tool_call", tool="read_file", args={"path": "notes.md"}),
        _event(1, 5, "tool_result", content="# 笔记"),
        _event(1, 6, "final_answer", content="done"),
    ]))
    assert ledger.state_of("a.js") == lifecycle.STATE_LIVE
    assert ledger.state_of("notes.md") == lifecycle.STATE_READ_ONLY
    assert ledger.live_files() == ["a.js"]
    assert ledger.live_count() == 1

    ledger.update(_round(2, [
        _event(2, 1, "user", content="改"),
        _event(2, 2, "tool_call", tool="edit_file",
               args={"path": "a.js", "old_text": "x", "new_text": "y"}),
        _event(2, 3, "tool_result", content="ok"),
        _event(2, 4, "tool_call", tool="delete_file", args={"path": "a.js"}),
        _event(2, 5, "tool_result", content="已归档"),
        _event(2, 6, "final_answer", content="ok"),
    ]))
    entry = ledger.entries()["a.js"]
    assert entry["state"] == lifecycle.STATE_DEAD
    assert entry["write_count"] == 2
    assert entry["versions"] == ["R1-E02", "R2-E02"]
    assert entry["deleted_at"] == "R2-E04"
    assert ledger.live_files() == []


def test_write_after_delete_revives():
    """删除后重写：文件复活为 live。"""
    ledger = lifecycle.FileLedger()
    ledger.update(_round(1, [
        _event(1, 2, "tool_call", tool="write_file",
               args={"path": "a.js", "content": "x"}),
        _event(1, 3, "tool_result", content="ok"),
    ]))
    ledger.update(_round(2, [
        _event(2, 2, "tool_call", tool="delete_file", args={"path": "a.js"}),
        _event(2, 3, "tool_result", content="ok"),
    ]))
    assert ledger.state_of("a.js") == lifecycle.STATE_DEAD
    ledger.update(_round(3, [
        _event(3, 2, "tool_call", tool="write_file",
               args={"path": "a.js", "content": "new"}),
        _event(3, 3, "tool_result", content="ok"),
    ]))
    assert ledger.state_of("a.js") == lifecycle.STATE_LIVE
    assert ledger.entries()["a.js"]["deleted_at"] is None


def test_sync_disk_flips_dead_for_rmd_files():
    """run_command rm 掉的文件（无 delete_file 事件）经磁盘对账翻 dead；
    重建后翻回 live。"""
    ledger = lifecycle.FileLedger()
    ledger.update(_round(1, [
        _event(1, 2, "tool_call", tool="write_file",
               args={"path": "tmp.js", "content": "x"}),
        _event(1, 3, "tool_result", content="ok"),
    ]))
    disk = {"tmp.js": True}
    assert ledger.sync_disk(lambda p: p in disk) == []
    disk.clear()
    changed = ledger.sync_disk(lambda p: p in disk)
    assert changed == ["tmp.js"]
    assert ledger.state_of("tmp.js") == lifecycle.STATE_DEAD
    disk["tmp.js"] = True
    assert ledger.sync_disk(lambda p: p in disk) == ["tmp.js"]
    assert ledger.state_of("tmp.js") == lifecycle.STATE_LIVE


def test_ledger_wired_with_file_blocks():
    """账本与按文件切分联动：update 收 blocks 登记 block_refs（跨轮文件索引）。"""
    ledger = lifecycle.FileLedger()
    r1 = _round(1, [
        _event(1, 1, "user", content="写"),
        _event(1, 2, "tool_call", tool="write_file",
               args={"path": "a.js", "content": "x"}),
        _event(1, 3, "tool_result", content="ok"),
        _event(1, 4, "final_answer", content="ok"),
    ])
    bs1 = blocks.segment_round_by_file(r1)
    ledger.update(r1, blocks=bs1)
    r2 = _round(2, [
        _event(2, 1, "user", content="改"),
        _event(2, 2, "tool_call", tool="edit_file",
               args={"path": "a.js", "old_text": "x", "new_text": "y"}),
        _event(2, 3, "tool_result", content="ok"),
        _event(2, 4, "final_answer", content="ok"),
    ])
    bs2 = blocks.segment_round_by_file(r2)
    ledger.update(r2, blocks=bs2)
    entry = ledger.entries()["a.js"]
    assert entry["block_refs"] == ["R1-B1", "R2-B1"]
    assert entry["versions"] == ["R1-E02", "R2-E02"]
    assert ledger.state_of("a.js") == lifecycle.STATE_LIVE
    assert ledger.live_count() == 1


def test_rejected_delete_does_not_kill():
    """delete_file 被用户拒绝（结果文本含失败标记）：账本不标记 dead。"""
    ledger = lifecycle.FileLedger()
    ledger.update(_round(1, [
        _event(1, 2, "tool_call", tool="write_file",
               args={"path": "keep.js", "content": "x"}),
        _event(1, 3, "tool_result", content="ok"),
    ]))
    ledger.update(_round(2, [
        _event(2, 2, "tool_call", tool="delete_file", args={"path": "keep.js"}),
        _event(2, 3, "tool_result", content="用户拒绝了删除操作。请换一种做法。"),
    ]))
    assert ledger.state_of("keep.js") == lifecycle.STATE_LIVE

    # 真删除才翻 dead
    ledger.update(_round(3, [
        _event(3, 2, "tool_call", tool="delete_file", args={"path": "keep.js"}),
        _event(3, 3, "tool_result", content="已删除 keep.js（删除前内容已归档）"),
    ]))
    assert ledger.state_of("keep.js") == lifecycle.STATE_DEAD
