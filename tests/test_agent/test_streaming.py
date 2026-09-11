"""Agent 运行时测试（自 test_agent.py 拆分，2026-09-11）。

本模块：test_streaming。"""

import json
import pytest
from wovra.agent import Agent
from wovra.tools import read_file
from wovra.task import Task

# 共享夹具/工具（_helpers.py 是原文件的公共头部）
from ._helpers import *  # noqa: F401,F403

def test_streaming_accumulates_fragmented_tool_call():
    """同一工具调用的参数 JSON 分两个分片到达，必须聚合后执行。"""

    def echo(text: str):
        """原样返回。"""
        return text

    responses = [
        # 第一次调用：工具调用分两片到达
        [
            _chunk(_delta(tool_calls=[_fragment(0, id="c1", name="echo", arguments='{"text"')])),
            _chunk(_delta(tool_calls=[_fragment(0, arguments=':"你好"}')])),
        ],
        # 第二次调用：拿到工具结果后给出最终回答
        [_chunk(_delta(content="完成"))],
    ]
    agent = _agent_with([echo], responses)

    answer = agent.run("开始")

    assert answer == "完成"
    # 回传给模型的工具结果正是聚合后的参数执行所得
    tool_msg = next(m for m in agent.messages if m["role"] == "tool")
    assert tool_msg["content"] == "你好"


def test_streaming_forwards_thinking_and_answer_deltas():
    thinking_seen, answer_seen = [], []

    responses = [[
        _chunk(_delta(reasoning="先想一下")),
        _chunk(_delta(content="最终")),
        _chunk(_delta(content="回答")),
        _chunk(usage=_usage(10, 20, 30, reasoning=8)),
    ]]
    agent = _agent_with([], responses)

    answer = agent.run(
        "问", on_thinking=thinking_seen.append, on_answer_delta=answer_seen.append
    )

    assert answer == "最终回答"
    assert thinking_seen == ["先想一下"]
    assert answer_seen == ["最终", "回答"]
    # 用量跨分块聚合，思考 token 单独记录；提示词分类各字段齐备
    assert agent.last_stats["prompt_tokens"] == 10
    assert agent.last_stats["completion_tokens"] == 20
    assert agent.last_stats["reasoning_tokens"] == 8
    assert agent.last_stats["total_tokens"] == 30
    assert set(agent.last_stats["prompt_breakdown"]) == {
        "system", "context", "tools", "user", "assistant", "tool",
    }


def test_run_always_uses_streaming():
    """run() 统一走流式——这是成本核算和实时展示的前提。"""
    responses = [[_chunk(_delta(content="hi"))]]
    agent = _agent_with([], responses)
    agent.run("问")
    assert agent.llm.calls[0]["stream"] is True


def test_empty_stream_retries_then_keeps_round_open():
    """流被端点掐断（只有思考，正文与 usage 均未到）→ 空串不是最终回答。

    自动重试至多 2 次；仍空则 raise 且轮保持开放（end_state 缺省，\\c 可续）。
    """
    responses = [
        [_chunk(_delta(reasoning="想了一半")), _chunk(_delta())],
        [_chunk(_delta(reasoning="又想了一半"))],
        [_chunk(_delta(reasoning="还是空"))],
    ]
    agent = _agent_with([], responses)
    with pytest.raises(RuntimeError, match="保持开放"):
        agent.run("第一轮")
    assert len(agent.llm.calls) == 3  # 空响应自动重试了两次
    # 轮未闭合：没有 final_answer 事件，end_state 保持 open（\c 可续）
    assert not any(e["type"] == "final_answer" for e in agent.rounds[-1]["events"])
    assert agent.rounds[-1].get("end_state") == "open"
    # 中断通知记为持久轮事件，且每轮只记一次（重试/续跑时模型都知道失败过）
    notes = [e for e in agent.rounds[-1]["events"] if e["type"] == "runtime_note"]
    assert len(notes) == 1
    assert "异常终止" in notes[0]["message"]["content"]


def test_resume_after_empty_stream_sees_interruption_note():
    """重试耗尽 → \\c 续跑：中断通知在轮事件里，续跑调用的输入带 steering，
    模型不再盲目从头重想 13 分钟。"""
    responses = [
        [_chunk(_delta(reasoning="想了一半"))],
        [_chunk(_delta(reasoning="又想了一半"))],
        [_chunk(_delta(reasoning="还是空"))],
    ]
    agent = _agent_with([], responses)
    with pytest.raises(RuntimeError, match="保持开放"):
        agent.run("第一轮")
    agent.llm.responses = [[_chunk(_delta(content="续上了"))]]
    assert agent.resume() == "续上了"
    last_messages = agent.llm.calls[-1]["messages"]
    assert any(
        "runtime-reminder" in str(m.get("content"))
        and "压缩思考" in str(m.get("content"))
        for m in last_messages
    )


def test_empty_stream_retry_recovers_and_closes_round():
    """第一次空响应重试后拿到正文 → 正常闭合轮次。"""
    responses = [
        [_chunk(_delta(reasoning="断了"))],
        [_chunk(_delta(content="好了"))],
    ]
    agent = _agent_with([], responses)
    assert agent.run("问") == "好了"
    assert agent.rounds[-1]["end_state"] == "completed"


def test_empty_stream_with_length_finish_raises_without_retry():
    """finish_reason=length（输出上限）：重试必再撞上限，不重试直接上报。"""
    responses = [[_chunk(_delta(reasoning="想多了"), finish_reason="length")]]
    agent = _agent_with([], responses)
    with pytest.raises(RuntimeError, match="length"):
        agent.run("第一轮")
    assert len(agent.llm.calls) == 1


def test_midstream_api_error_retries_then_keeps_round_open():
    """服务端流中途报错（APIError 非 RuntimeError 家族）→ 转 LLMStreamError
    并入空响应护栏：重试至多 2 次，仍错则 raise 且轮保持开放，进程不崩。"""
    agent = _agent_with([], [_boom_stream(), _boom_stream(), _boom_stream()])
    with pytest.raises(RuntimeError, match="保持开放"):
        agent.run("第一轮")
    assert len(agent.llm.calls) == 3
    assert not any(e["type"] == "final_answer" for e in agent.rounds[-1]["events"])
    assert agent.rounds[-1].get("end_state") == "open"


def test_midstream_api_error_retry_recovers():
    """第一次流中途报错、重试拿到正文 → 正常闭合轮次。"""
    responses = [_boom_stream(), [_chunk(_delta(content="恢复"))]]
    agent = _agent_with([], responses)
    assert agent.run("问") == "恢复"
    assert agent.rounds[-1]["end_state"] == "completed"


def test_readonly_batch_runs_in_order(monkeypatch, tmp_path):
    """纯只读批次并发执行，结果仍按调用顺序记录（顺序是正确性契约）。"""
    from wovra import tools as tools_module

    monkeypatch.setattr(tools_module.safety, "PROJECT_ROOT", tmp_path)
    for name, text in (("a.txt", "内容A"), ("b.txt", "内容B"), ("c.txt", "内容C")):
        (tmp_path / name).write_text(text, encoding="utf-8")
    calls = [
        _fragment(i, id=f"c{i}", name="read_file",
                  arguments=json.dumps({"path": f"{name}.txt"}))
        for i, name in enumerate("abc")
    ]
    responses = [
        [_chunk(_delta(tool_calls=calls)), _chunk(usage=_usage(10, 5, 15))],
        [_chunk(_delta(content="ok"))],
    ]
    from wovra.tools import read_file

    # baseline：不触发整理调用，stub 响应序列刚好够用
    task = Task.create(goal="g")
    agent = Agent(llm=_StubLLM(responses), tools=[read_file], task=task,
                  context_mode="baseline")

    agent.run("读三个文件")

    results = [e["detail"] for e in task.history if e["kind"] == "tool_result"]
    assert len(results) == 3
    assert "内容A" in results[0] and "内容B" in results[1] and "内容C" in results[2]


def test_thinking_head_single_line():
    """思考单行化：折叠空白取尾部，单行展示。"""
    from wovra import ui

    long = "思路" * 200
    head = ui.thinking_head(long)
    assert head.startswith("…") and len(head) <= 102
    assert ui.thinking_line("x").startswith("💭")
