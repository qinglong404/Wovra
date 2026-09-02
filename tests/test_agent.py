"""Agent 纯逻辑部分的测试：schema 生成、错误回传、路径安全。

不发起任何真实模型调用——Agent 只有 run() 会碰 llm.chat，
构造时传一个什么都不做的 stub 即可。
"""

import json
from types import SimpleNamespace

import pytest

from wovra import task as task_module
from wovra.agent import Agent, _schema_of, read_file
from wovra.task import Task


class _StubLLM:
    """替身 LLM：构造时收到"每次 chat 调用应返回的分块序列"列表。"""

    model = "stub"

    def __init__(self, responses: list | None = None):
        self.responses = list(responses or [])
        self.calls: list[dict] = []

    def chat(self, messages, tools=None, stream=False, **kwargs):
        self.calls.append({"messages": messages, "stream": stream})
        return iter(self.responses.pop(0))


def _agent_with(tools, responses=None) -> Agent:
    return Agent(llm=_StubLLM(responses), tools=tools)


# ---- 流式协议分块的替身构造 -------------------------------------------------


def _delta(content=None, tool_calls=None, reasoning=None):
    return SimpleNamespace(
        content=content,
        tool_calls=tool_calls,
        reasoning_content=reasoning,
        model_extra=None,
    )


def _fragment(index, id=None, name=None, arguments=None):
    """一个 tool_call 分片：流式下 name 和 arguments 是分次到达的。"""
    return SimpleNamespace(index=index, id=id, function=SimpleNamespace(name=name, arguments=arguments))


def _chunk(delta=None, usage=None):
    choices = [] if delta is None else [SimpleNamespace(delta=delta)]
    return SimpleNamespace(choices=choices, usage=usage)


def _usage(prompt, completion, total, reasoning=None, cached=None):
    completion_details = SimpleNamespace(reasoning_tokens=reasoning) if reasoning else None
    # cached=None 时不提供 prompt_tokens_details，模拟服务端不支持缓存统计
    prompt_details = (
        SimpleNamespace(cached_tokens=cached) if cached is not None else None
    )
    return SimpleNamespace(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=total,
        completion_tokens_details=completion_details,
        prompt_tokens_details=prompt_details,
    )


# ---- schema 生成 ---------------------------------------------------------


def test_schema_from_type_hints():
    def demo_tool(count: int, label: str = "x"):
        """工具的一句话描述。"""

    schema = _schema_of(demo_tool)
    fn = schema["function"]
    assert fn["name"] == "demo_tool"
    assert fn["description"] == "工具的一句话描述。"
    assert fn["parameters"]["properties"]["count"] == {"type": "integer"}
    assert fn["parameters"]["properties"]["label"] == {"type": "string"}
    # label 有默认值 → 非必填
    assert fn["parameters"]["required"] == ["count"]


def test_schema_without_annotation_falls_back_to_string():
    def loose_tool(anything):
        """无注解参数。"""

    props = _schema_of(loose_tool)["function"]["parameters"]["properties"]
    assert props["anything"] == {"type": "string"}


# ---- 工具执行的三种失败路径：错误都回传给模型，而不是抛出 --------------------


def test_unknown_tool_returns_error_text():
    agent = _agent_with([])
    agent._execute("call_1", "不存在的工具", "{}")

    last = agent.messages[-1]
    assert last["role"] == "tool"
    assert "未知工具" in last["content"]


def test_invalid_json_arguments_returns_error_text():
    def ok_tool(a: int):
        """参数一个。"""

    agent = _agent_with([ok_tool])
    agent._execute("call_1", "ok_tool", "{不是json")

    assert "合法 JSON" in agent.messages[-1]["content"]


def test_tool_exception_returns_error_text():
    def boom():
        """必然抛错。"""
        raise ValueError("炸了")

    agent = _agent_with([boom])
    agent._execute("call_1", "boom", "{}")

    assert "工具执行出错" in agent.messages[-1]["content"]
    assert "炸了" in agent.messages[-1]["content"]


def test_non_string_result_is_serialized():
    def make_list():
        """返回 list。"""
        return ["a", "b"]

    agent = _agent_with([make_list])
    agent._execute("call_1", "make_list", "{}")

    assert json.loads(agent.messages[-1]["content"]) == ["a", "b"]


# ---- 流式循环：分片聚合、回调、用量统计 -------------------------------------


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


def test_usage_accumulates_across_turns():
    """一次 run 内多次 LLM 调用（工具轮 + 回答轮）的用量要累加。"""

    def noop():
        """什么也不做。"""

    responses = [
        # 工具轮：分片 + 该轮流量的 usage 分块（choices 为空）
        [
            _chunk(_delta(tool_calls=[_fragment(0, id="c1", name="noop", arguments="{}")])),
            _chunk(usage=_usage(10, 5, 15)),
        ],
        # 回答轮：内容 + 流量
        [_chunk(_delta(content="ok")), _chunk(usage=_usage(20, 10, 30))],
    ]
    agent = _agent_with([noop], responses)

    agent.run("go")

    assert agent.last_stats["prompt_tokens"] == 30
    assert agent.last_stats["completion_tokens"] == 15
    assert agent.last_stats["total_tokens"] == 45


def test_turns_steps_tool_calls_and_cache_accounting():
    def noop():
        """什么也不做。"""

    responses = [
        [
            _chunk(_delta(tool_calls=[_fragment(0, id="c1", name="noop", arguments="{}")])),
            _chunk(usage=_usage(10, 2, 12, cached=4)),
        ],
        [_chunk(_delta(content="ok")), _chunk(usage=_usage(20, 8, 28, cached=6))],
        # 第二次 run：服务端这次没返回 prompt_tokens_details
        [_chunk(_delta(content="done")), _chunk(usage=_usage(5, 1, 6))],
    ]
    agent = _agent_with([noop], responses)

    agent.run("第一轮")
    stats = agent.last_stats
    # 一次 run = 2 步（工具轮 + 回答轮），1 次工具调用
    assert stats["turn"] == 1
    assert stats["llm_calls"] == 2
    assert stats["tool_calls"] == 1
    # 缓存命中累加：4 + 6；未命中 = 各轮 prompt - 命中
    assert stats["cached_tokens"] == 10
    assert stats["cache_miss_tokens"] == (10 - 4) + (20 - 6)

    agent.run("第二轮")
    assert agent.last_stats["turn"] == 2  # 轮次跨 run 累计
    assert agent.last_stats["llm_calls"] == 1
    # 服务端没返回缓存明细 → 按 0 命中计入未命中（保守口径）
    assert agent.last_stats["cached_tokens"] == 0
    assert agent.last_stats["cache_miss_tokens"] == 5


def test_run_always_uses_streaming():
    """run() 统一走流式——这是成本核算和实时展示的前提。"""
    responses = [[_chunk(_delta(content="hi"))]]
    agent = _agent_with([], responses)
    agent.run("问")
    assert agent.llm.calls[0]["stream"] is True


# ---- 任务绑定 ------------------------------------------------------------


def test_state_refresh_updates_goal_and_status_from_conversation(monkeypatch, tmp_path):
    """目标由 AI 每轮重估：一轮结束后 goal/status/summary 被更新。"""
    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path)
    state_json = '{"goal": "搞清测试覆盖", "status": "done", "summary": "全部完成"}'
    responses = [
        [_chunk(_delta(content="干完了"))],
        [_chunk(_delta(content=state_json))],
    ]
    task = Task.create(goal="初始的模糊想法")
    agent = Agent(llm=_StubLLM(responses), tools=[], task=task)

    answer = agent.run("把活干完")

    assert answer == "干完了"
    assert task.goal == "搞清测试覆盖"
    assert task.status == "done"
    assert task.summary == "全部完成"


def test_state_refresh_survives_invalid_json(monkeypatch, tmp_path):
    """状态评估输出不是合法 JSON 时，保留原状态而不是覆盖坏数据。"""
    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path)
    responses = [
        [_chunk(_delta(content="回答"))],
        [_chunk(_delta(content="这不是 JSON {{{"))],
    ]
    task = Task.create(goal="初始目标")
    agent = Agent(llm=_StubLLM(responses), tools=[], task=task)

    agent.run("问")

    assert task.goal == "初始目标"  # 原状态未被破坏
    assert task.status == "in_progress"


def test_state_refresh_does_not_downgrade_done_arbitrarily(monkeypatch, tmp_path):
    """apply_state 只接受合法 status；模型输出别的值不生效。"""
    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path)
    state_json = '{"goal": "x", "status": "completed", "summary": "s"}'
    responses = [
        [_chunk(_delta(content="回答"))],
        [_chunk(_delta(content=state_json))],
    ]
    task = Task.create(goal="目标")
    agent = Agent(llm=_StubLLM(responses), tools=[], task=task)

    agent.run("问")

    assert task.status == "in_progress"  # "completed" 非法，保留原值
    assert task.goal == "x"              # 合法字段照常更新


def test_agent_records_events_into_task(monkeypatch, tmp_path):
    from wovra import task as task_module
    from wovra.task import Task

    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path)

    def echo(text: str):
        """原样返回。"""
        return text

    task = Task.create(goal="记录测试")
    agent = Agent(llm=_StubLLM(), tools=[echo], task=task)
    agent._execute("call_1", "echo", '{"text": "hi"}')

    kinds = [e["kind"] for e in task.history]
    assert "tool_call" in kinds and "tool_result" in kinds
    assert (tmp_path / task.id / "task.json").exists()  # 每次执行后都落盘


# ---- 路径安全 -------------------------------------------------------------


def test_read_file_blocks_escape_from_project_root():
    with pytest.raises(ValueError, match="路径越界"):
        read_file("../../etc/passwd")
