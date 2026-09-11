"""工具层测试（自 test_tools.py 拆分，2026-09-11）。

本模块：test_audit。"""

import json
from wovra import task as task_module
from wovra.agent import Agent
from wovra.task import Task
from wovra.tools import run_command, write_file

# 共享夹具/工具（_helpers.py 是原文件的公共头部）
from ._helpers import *  # noqa: F401,F403

def test_agent_audits_overwrite_with_old_content_backup(monkeypatch, tmp_path):
    """覆盖文件时，旧内容通过审计挂钩完整留底，可对照还原。"""
    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path)
    from wovra import tools

    monkeypatch.setattr(tools.safety, "PROJECT_ROOT", tmp_path)

    # 旧文件先存在，随后被 agent 覆盖
    (tmp_path / "note.txt").write_text("这是旧内容", encoding="utf-8")

    task = Task.create(goal="审计测试")
    agent = Agent(llm=_StubLLM(), tools=[write_file], task=task)
    agent._execute("c1", "write_file", '{"path": "note.txt", "content": "新内容"}')

    backups = [e for e in task.history if e["kind"] == "file_change"]
    assert len(backups) == 1
    assert "[write_file 旧内容备份]" in backups[0]["detail"]
    assert "这是旧内容" in backups[0]["detail"]
    # 新内容在常规的 tool_call 记录里
    assert any("新内容" in e["detail"] for e in task.history if e["kind"] == "tool_call")


def test_first_creation_has_no_backup_but_tool_call_recorded(monkeypatch, tmp_path):
    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path)
    from wovra import tools

    monkeypatch.setattr(tools.safety, "PROJECT_ROOT", tmp_path)

    task = Task.create(goal="审计测试")
    agent = Agent(llm=_StubLLM(), tools=[write_file], task=task)
    agent._execute("c1", "write_file", '{"path": "note.txt", "content": "审计内容"}')

    # 创建（无旧内容）没有备份事件，但常规调用记录在
    assert not [e for e in task.history if e["kind"] == "file_change"]
    assert any("审计内容" in e["detail"] for e in task.history if e["kind"] == "tool_call")
    assert (tmp_path / "note.txt").read_text(encoding="utf-8") == "审计内容"


def test_dangerous_command_is_audited_and_not_executed(monkeypatch, tmp_path):
    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path)
    from wovra import tools

    monkeypatch.setattr(tools.safety, "PROJECT_ROOT", tmp_path)

    task = Task.create(goal="拒绝测试")
    agent = Agent(llm=_StubLLM(), tools=[run_command], task=task)
    agent._execute("c1", "run_command", json.dumps({"command": "rm -rf /"}))

    # 拒绝结果回传给模型（红色判定标记在）
    tool_msg = next(m for m in agent.messages if m["role"] == "tool")
    assert "已拒绝执行危险命令" in tool_msg["content"]
    # 审计记录完整保留了试图执行的命令原文
    assert any("rm -rf /" in e["detail"] for e in task.history if e["kind"] == "file_change")
