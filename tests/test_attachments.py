"""用户附件（2026-09-17）：落盘 + 轮内注入，不是一次工具调用。

两条被测口径：
* **原文不进存档**：用户消息里只有一行 `【附件】path=…`，内容在装配期展开；
* **注入失败要说出来**：超限/读不出/图片发不出去，模型必须知道"这东西我没看到"。
"""

import base64
import struct
import zlib

import pytest

from wovra import attachments
from wovra.agent.assembly import _AssemblyMixin


def _png_solid(w: int, h: int, rgb: tuple = (10, 20, 30)) -> bytes:
    """纯 stdlib 造一张纯色 PNG（不引依赖）。"""
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


@pytest.fixture
def workspace(monkeypatch, tmp_path):
    from wovra.tools import safety

    monkeypatch.setattr(safety, "workspace_root", lambda: tmp_path)
    return tmp_path


def _agent_with_round(round_: dict):
    """只借装配 mixin 的方法（`_attachment_message` 不依赖 Agent 其余状态）。"""

    class _Stub(_AssemblyMixin):
        _vision_ok = True

        def __init__(self, current):
            self.current_round = current

    return _Stub(round_)


def test_text_paste_lands_in_attachments_and_round_trips(workspace):
    rel = attachments.save_text("print(1)\n", "pasted.py")

    assert rel.startswith("wovra-attachments/") and rel.endswith("-pasted.py")
    assert (workspace / rel).read_text(encoding="utf-8") == "print(1)\n"
    assert attachments.read_text(rel) == ("", "print(1)\n")


def test_same_name_twice_does_not_overwrite(workspace):
    """附件是用户材料：重名补序号，不能悄悄替掉前一个。"""
    first = attachments.save_text("A", "log.txt")
    second = attachments.save_text("B", "log.txt")

    assert first != second
    assert (workspace / first).read_text(encoding="utf-8") == "A"
    assert (workspace / second).read_text(encoding="utf-8") == "B"


def test_safe_name_strips_directory_and_bad_chars():
    assert attachments.safe_name("../../etc/passwd") == "passwd"
    assert attachments.safe_name('a<b>c?.py') == "a_b_c_.py"
    assert attachments.safe_name("") == "pasted.txt"


def test_data_url_saves_bytes(workspace):
    raw = b"\x89PNG\r\n\x1a\n" + b"x" * 8
    url = "data:image/png;base64," + base64.b64encode(raw).decode()

    rel = attachments.save_data_url(url, "shot.png")

    assert (workspace / rel).read_bytes() == raw
    assert attachments.is_image(rel)


def test_resolve_refuses_escape(workspace):
    assert attachments.resolve("../../etc/passwd") is None
    assert attachments.resolve("attachments/nope.txt") is None
    assert attachments.resolve(attachments.save_text("x", "a.txt")) is not None


def test_marker_is_line_anchored(workspace):
    """标记必须行首锚定——源码/文档里引用这一行不该被当成引用。"""
    rel = attachments.save_text("body", "a.txt")

    assert attachments.ATTACH_MARKER_RE.search(attachments.marker(rel))
    assert attachments.ATTACH_MARKER_RE.search(f"见 {attachments.marker(rel)}") is None


def test_load_parts_injects_full_text(workspace):
    rel = attachments.save_text("行一\n行二\n行三", "a.txt")

    parts, skipped = attachments.load_parts(f"看看这个\n{attachments.marker(rel)}")

    assert skipped == []
    assert len(parts) == 1 and parts[0]["type"] == "text"
    body = parts[0]["text"]
    assert "行一" in body and "行三" in body
    assert rel in body and "3 行" in body


def test_load_parts_dedupes_and_reports_unreadable(workspace):
    rel = attachments.save_text("内容", "a.txt")
    missing = attachments.marker("attachments/gone.txt")

    parts, skipped = attachments.load_parts(
        f"{attachments.marker(rel)}\n{attachments.marker(rel)}\n{missing}"
    )

    assert parts[0]["text"].count("内容") == 1          # 同一路径只注入一次
    assert len(skipped) == 1 and "gone.txt" in skipped[0]


def test_load_parts_truncates_and_says_so(workspace, monkeypatch):
    monkeypatch.setattr(attachments, "MAX_INJECT_CHARS", 20)
    rel = attachments.save_text("要" * 100, "big.txt")

    parts, _ = attachments.load_parts(attachments.marker(rel))

    body = parts[0]["text"]
    assert "已截断到 20 字符" in body and "read_file" in body


def test_load_parts_stops_at_total_budget(workspace, monkeypatch):
    monkeypatch.setattr(attachments, "MAX_TOTAL_CHARS", 8)
    first = attachments.save_text("a" * 8, "a.txt")
    second = attachments.save_text("b" * 8, "b.txt")

    parts, skipped = attachments.load_parts(
        f"{attachments.marker(first)}\n{attachments.marker(second)}"
    )

    assert "a" * 8 in parts[0]["text"]
    assert len(skipped) == 1 and "总量已达上限" in skipped[0]


def test_attachment_message_injects_into_round(workspace):
    rel = attachments.save_text("def f():\n    return 1\n", "pasted.py")
    agent = _agent_with_round({
        "user_input": {"original": f"改一下这个\n{attachments.marker(rel)}"},
        "events": [],
    })

    msg = agent._attachment_message()

    assert msg is not None and msg["role"] == "user"
    assert msg["content"][0]["type"] == "text"
    assert "不必再调 read_file" in msg["content"][0]["text"]
    assert "def f()" in msg["content"][1]["text"]


def test_attachment_message_scans_in_round_user_events(workspace):
    """轮内用户补充发言里的附件同样要注入（那是同一轮的追加材料）。"""
    rel = attachments.save_text("补充材料", "extra.txt")
    agent = _agent_with_round({
        "user_input": {"original": "开始吧"},
        "events": [
            {"type": "user",
             "message": {"role": "user",
                         "content": f"忘了，给你\n{attachments.marker(rel)}"}},
        ],
    })

    msg = agent._attachment_message()

    assert msg is not None and "补充材料" in msg["content"][1]["text"]


def test_attachment_message_none_without_marker(workspace):
    agent = _agent_with_round({"user_input": {"original": "普通一句话"}, "events": []})

    assert agent._attachment_message() is None


def test_image_attachment_becomes_image_part(workspace):
    rel = attachments.save(_png_solid(8, 8), "shot.png")

    parts, skipped = attachments.load_parts(attachments.marker(rel))

    assert skipped == []
    assert any(p["type"] == "image_url" for p in parts)


def test_nonvision_model_gets_no_image_part(workspace):
    """不支持视觉的模型：图不发出去（发了也被拒），只在说明里点出来。"""
    rel = attachments.save(_png_solid(8, 8), "shot.png")
    agent = _agent_with_round({
        "user_input": {"original": attachments.marker(rel)},
        "events": [],
    })
    agent._vision_ok = False

    msg = agent._attachment_message()

    assert msg is not None
    assert all(p.get("type") != "image_url" for p in msg["content"])


def test_assembly_end_to_end_injects_attachment(monkeypatch, tmp_path):
    """装配级：附件内容真出现在本轮消息里（不是只到 `_attachment_message`）。"""
    from wovra.agent import Agent
    from wovra.task import Task
    from wovra.tools import safety

    monkeypatch.setattr(safety, "workspace_root", lambda: tmp_path)
    rel = attachments.save_text("被粘贴进来的那一段\n第二行", "pasted.txt")
    text = f"改一下\n{attachments.marker(rel)}"

    task = Task.create(goal="x")
    agent = Agent(llm=None, tools=[], task=task)
    agent.current_round = {
        "seq": 1,
        "user_input": {"original": text},
        "events": [{"id": "R1-E01", "type": "user",
                    "message": {"role": "user", "content": text}}],
        "end_state": "open",
    }
    agent.messages = [{"role": "user", "content": text}]   # 轮内协议消息（与 events 对齐）
    agent.rounds = [agent.current_round]

    msgs = agent._assemble_messages()

    assert any("被粘贴进来的那一段" in str(m.get("content")) for m in msgs)
    # 用户消息本身仍是引用行（原文不落存档）
    assert any(attachments.marker(rel) in str(m.get("content")) for m in msgs)
