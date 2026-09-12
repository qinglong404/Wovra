"""思考过程随事件落盘（thinking 只进 event 不进 message，上下文零影响）。"""

import pytest
from wovra.agent import Agent
from wovra.task import Task
from wovra import task as task_module

from ._helpers import _StubLLM, _chunk, _delta, _fragment


def test_thinking_persisted_to_events(monkeypatch, tmp_path):
    """tool_call / final_answer 事件带思考全文；协议 message 不掺 thinking。"""

    def echo(text: str):
        """原样返回。"""
        return text

    responses = [
        # 第一步：思考后调工具
        [
            _chunk(_delta(reasoning="先想一步")),
            _chunk(_delta(tool_calls=[
                _fragment(0, id="c1", name="echo", arguments='{"text":"hi"}')])),
        ],
        # 第二步：拿到工具结果，再思考后收尾
        [
            _chunk(_delta(reasoning="再想两步")),
            _chunk(_delta(content="完成"), finish_reason="stop"),
        ],
    ]
    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path)
    task = Task.create(goal="g")
    agent = Agent(llm=_StubLLM(responses), tools=[echo], task=task,
                  async_organization=False)
    agent.run("干活")

    events = task.rounds[-1]["events"]
    ev_tool = next(e for e in events if e["type"] == "tool_call")
    ev_ans = next(e for e in events if e["type"] == "final_answer")
    assert ev_tool["thinking"] == "先想一步"
    assert ev_ans["thinking"] == "再想两步"
    # 上下文纯度：装配只读 message，thinking 不得混入
    assert "thinking" not in ev_tool["message"]
    assert "thinking" not in ev_ans["message"]
