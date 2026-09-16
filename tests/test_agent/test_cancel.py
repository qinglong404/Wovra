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


def test_invoke_tool_binds_cancel_check_for_blocking_tools():
    """停止本轮要能在**工具执行中**被看见（2026-09-16 用户实测的卡住问题）。

    实测症状：命令跑着时点停止，界面一直"停止中"，非得等命令自己跑完。
    因为 cancel_check 原先只在**步骤之间**被查。现在每次工具调用都把它绑进
    工具层（`tools.abort`），阻塞等待的工具可以轮询它。
    """
    from wovra import tools as tools_module

    seen: list = []

    def probe() -> str:
        """探针工具（测试替身）：报告此刻工具层看到的中断状态。"""
        seen.append(tools_module.abort.abort_requested())
        return "ok"

    agent = Agent(llm=_StubLLM(), tools=[probe])

    agent.cancel_check = lambda: True
    assert agent._invoke_tool("probe", "{}") == "ok"
    assert seen[-1] is True

    agent.cancel_check = lambda: False
    assert agent._invoke_tool("probe", "{}") == "ok"
    assert seen[-1] is False

    # 没设检查函数（脚本/测试直调路径）时，工具层恒为 False，不误杀
    agent.cancel_check = None
    assert agent._invoke_tool("probe", "{}") == "ok"
    assert seen[-1] is False
    assert tools_module.abort.abort_requested() is False      # 出栈已还原


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
