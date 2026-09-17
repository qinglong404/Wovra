"""文件变更通知（V4 §3.7）：观察快照 → 轮首行级差异注入（零 LLM）。

用户口径（2026-09-17）："读文件后，如果发生改变，下次输入指令时，将其当作提示，注入到用户内容
上面，告诉其之前读过的文件进行了修改，修改了哪些行。"

覆盖：通知内容（行级 hunk、`+a −b` 计数、归属措辞）、轮首注入与**轮内冻结**、观察记录**按 agent 分开**
（同一进程里 B 写文件不该刷新 A 手里那份的新鲜度——这是顺手修掉的真缺陷）。
"""

import json
import pathlib

from wovra import observed as observed_module
from wovra.tools import safety as safety_module

from ._helpers import _StubLLM, _chunk, _delta
from wovra.agent import MODE_MANAGED, Agent
from wovra.task import Task
from wovra import task as task_module
from wovra.truncate import make_event


def _workspace(tmp_path, monkeypatch) -> pathlib.Path:
    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path / "tasks")
    safety_module.bind_workspace(tmp_path)
    return tmp_path


def test_notice_reports_tool_write_with_hunks(tmp_path, monkeypatch):
    """工具改过的文件：报 `+a −b` 与行级 hunk，末尾给"别整文件重读"的指引。"""
    ws = _workspace(tmp_path, monkeypatch)
    rel = "runner.py"
    (ws / rel).write_text("a\nb\nc\n", encoding="utf-8")
    observed_module.record(ws, "Main", rel, "a\nb\nc\n")
    observed_module.note_write(ws, "A", rel, seq=3)
    (ws / rel).write_text("a\nb\nB2\nc\n", encoding="utf-8")

    text = observed_module.notice_text(ws, "Main")

    assert "[文件变更·工具写入（A）]" in text
    assert "+1 −0 行（3 → 4 行）" in text
    assert "+B2" in text                      # hunk 里有那一行
    assert "别整文件重读" in text


def test_notice_for_user_edit_does_not_judge(tmp_path, monkeypatch):
    """外部（用户）改动**不预判对错**：措辞按用户口径原文（可能高质量、也可能手滑）。"""
    ws = _workspace(tmp_path, monkeypatch)
    rel = "note.md"
    (ws / rel).write_text("x\n", encoding="utf-8")
    observed_module.record(ws, "Main", rel, "x\n")
    (ws / rel).write_text("x\n-1\n", encoding="utf-8")     # 没有工具写入记录 → 用户操作

    text = observed_module.notice_text(ws, "Main")

    assert "[文件变更·用户操作]" in text
    assert "不是工具写的" in text
    assert "不要当成权威版本，也不要当成错误" in text


def test_notice_is_empty_when_nothing_changed(tmp_path, monkeypatch):
    """没变就不注入（空串）。"""
    ws = _workspace(tmp_path, monkeypatch)
    (ws / "a.py").write_text("x\n", encoding="utf-8")
    observed_module.record(ws, "Main", "a.py", "x\n")
    assert observed_module.notice_text(ws, "Main") == ""


def test_notice_is_per_agent(tmp_path, monkeypatch):
    """**按 agent 分开**：A 的观察不会因为 B 读过同一文件而变"新"。"""
    ws = _workspace(tmp_path, monkeypatch)
    rel = "share.py"
    (ws / rel).write_text("v1\n", encoding="utf-8")
    observed_module.record(ws, "A", rel, "v1\n")            # A 看过 v1
    (ws / rel).write_text("v2\n", encoding="utf-8")         # 之后被改了（用户改的）

    assert "v2" in observed_module.notice_text(ws, "A")      # A 收到通知
    observed_module.record(ws, "B", rel, "v2\n")             # B 现在才看到 v2
    assert "v2" in observed_module.notice_text(ws, "A")      # A 的记录不受 B 影响
    assert observed_module.notice_text(ws, "B") == ""        # B 是新的，没变更


def _agent_with_round(monkeypatch, tmp_path, ws, **kw):
    task = Task.create(goal="g")
    agent = Agent(llm=_StubLLM(), tools=[], task=task, context_mode=MODE_MANAGED, **kw)
    round_ = {
        "seq": 1, "user_input": {"original": "改一下 a.py", "normalized": ""},
        "events": [make_event("R1-E01", "user",
                              {"role": "user", "content": "改一下 a.py"})],
        "refined_index": {}, "end_state": "open", "org_state": "", "active_view": "Main",
    }
    agent.rounds = [round_]
    agent.current_round = round_
    # `_current_round_messages` 要求"事件数 == 消息数"（结构对不上就不动手），
    # 手搭的轮要把它一并摆好
    agent.messages = [{"role": "user", "content": "改一下 a.py"}]
    return agent, round_


def test_notice_is_injected_above_user_message_and_frozen(tmp_path, monkeypatch):
    """通知出现在**用户发言之前**，而且**轮内冻结**——轮中文件再变，装配字节不变。"""
    ws = _workspace(tmp_path, monkeypatch)
    rel = "a.py"
    (ws / rel).write_text("v1\n", encoding="utf-8")
    observed_module.record(ws, "Main", rel, "v1\n")
    (ws / rel).write_text("v1\nv2\n", encoding="utf-8")
    agent, round_ = _agent_with_round(monkeypatch, tmp_path, ws)

    msgs = agent._assemble_messages()
    first = next(m for m in msgs if "[文件变更" in str(m.get("content") or ""))
    assert first["role"] == "user" and "<runtime-reminder>" in first["content"]
    assert msgs.index(first) < msgs.index(
        next(m for m in msgs if m.get("content") == "改一下 a.py"))   # 在用户内容**上面**

    # 轮内：文件又变了，但这一轮的装配必须逐字不变（撞 §2 的话就是回归）
    (ws / rel).write_text("v1\nv2\nv3\n", encoding="utf-8")
    again = agent._assemble_messages()
    assert [m.get("content") for m in again] == [m.get("content") for m in msgs]


def test_stale_detection_is_per_agent(tmp_path, monkeypatch):
    """**过期写入检测也按 agent 判**（顺手修掉的真缺陷）：B 的观察/写入不该刷新 A 的记录。

    旧实现是进程级全局的 `_file_registry`：B 写过同一文件就把记录刷成最新，于是 A 手里
    那份旧内容在记录上"看起来很新"，`_stale_error` 不触发 → A 拿着过期内容覆盖别人的改动。
    """
    from wovra.tools import files as files_module

    ws = _workspace(tmp_path, monkeypatch)
    f = ws / "a.py"
    f.write_text("v1\n", encoding="utf-8")
    safety_module.bind_agent("A")
    files_module._observe_file(f)                 # A 看到的还是 v1

    f.write_text("v2\n", encoding="utf-8")        # 别人改了
    safety_module.bind_agent("B")
    files_module._observe_file(f)                 # B 现在观察的是 v2

    safety_module.bind_agent("A")
    assert files_module._stale_error(f) is not None      # A 仍应见过期（旧实现这里是 None）
    safety_module.bind_agent("B")
    assert files_module._stale_error(f) is None          # B 是新鲜的
    safety_module.bind_agent("")
