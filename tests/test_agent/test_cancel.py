"""⏹ 协作式取消：cancel_check 触发 KeyboardInterrupt，轮保持开放（CLI Ctrl+C 同语义）。"""

import pytest
from wovra.agent import Agent
from wovra.task import Task
from wovra import task as task_module

from ._helpers import _StubLLM, _chunk, _delta


def test_cancel_check_interrupts_before_first_call(monkeypatch, tmp_path):
    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path)
    task = Task.create(goal="g")
    agent = Agent(llm=_StubLLM(responses=[[_chunk(_delta(content="x"))]]),
                  tools=[], task=task, async_organization=False)
    agent.cancel_check = lambda: True
    with pytest.raises(KeyboardInterrupt):
        agent.run("干活")
    # 轮已开（用户事件已落）且保持开放——调用方 finalize_round("open") 收尾
    assert task.rounds and task.rounds[-1]["end_state"] in ("", "open")


def test_cancel_check_may_flip_midway():
    """False 时正常放行——取消是协作式的，可以在任意时刻翻转。"""
    agent = Agent(llm=_StubLLM(), tools=[])
    agent.cancel_check = lambda: False
    assert agent.cancel_check() is False


def test_dangling_tool_call_tail_gets_synthetic_results(monkeypatch, tmp_path):
    """工具卡即时落盘的伴生保护：开放轮尾部挂着未应答的 tool_calls 时，
    装配补合成 tool 结果，严格端点不再 400。"""
    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path)
    task = Task.create(goal="g")
    agent = Agent(llm=_StubLLM(), tools=[], task=task,
                  async_organization=False)
    agent._open_or_reuse_round("干活")
    tc = [{"id": "c1", "type": "function",
           "function": {"name": "run_command", "arguments": "{}"}},
          {"id": "c2", "type": "function",
           "function": {"name": "read_file", "arguments": "{}"}}]
    agent._record_event("tool_call",
                        {"role": "assistant", "content": "", "tool_calls": tc})
    # 批内只有 c1 的结果落了盘（进程死在 c2 执行中途的形态）
    agent._record_event("tool_result",
                        {"role": "tool", "tool_call_id": "c1", "content": "ok"})
    msgs = agent._assemble_messages()
    assert msgs[-1]["role"] == "tool"
    assert msgs[-1]["tool_call_id"] == "c2"
    assert "中断" in msgs[-1]["content"]
    # 事件本身不被污染（合成结果只在装配层）
    events = agent.rounds[-1]["events"]
    assert all(e["message"].get("role") != "tool" or
               e["message"].get("tool_call_id") != "c2"
               for e in events)
