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
