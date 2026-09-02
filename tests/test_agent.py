"""Agent 纯逻辑部分的测试：schema 生成、错误回传、路径安全。

不发起任何真实模型调用——Agent 只有 run() 会碰 llm.chat，
构造时传一个什么都不做的 stub 即可。
"""

import json
from types import SimpleNamespace

import pytest

from wovra.agent import Agent, _schema_of, read_file


class _StubLLM:
    """替身 LLM：Agent 构造和 _execute 都不会真正调用模型。"""

    model = "stub"

    def chat(self, *args, **kwargs):
        raise AssertionError("单元测试不应触发真实模型调用")


def _tool_call(name: str, arguments: str):
    """伪造一个 OpenAI 协议的 tool_call 对象。"""
    return SimpleNamespace(
        id="call_1",
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def _agent_with(tools) -> Agent:
    return Agent(llm=_StubLLM(), tools=tools)


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
    agent._execute(_tool_call("不存在的工具", "{}"))

    last = agent.messages[-1]
    assert last["role"] == "tool"
    assert "未知工具" in last["content"]


def test_invalid_json_arguments_returns_error_text():
    def ok_tool(a: int):
        """参数一个。"""

    agent = _agent_with([ok_tool])
    agent._execute(_tool_call("ok_tool", "{不是json"))

    assert "合法 JSON" in agent.messages[-1]["content"]


def test_tool_exception_returns_error_text():
    def boom():
        """必然抛错。"""
        raise ValueError("炸了")

    agent = _agent_with([boom])
    agent._execute(_tool_call("boom", "{}"))

    assert "工具执行出错" in agent.messages[-1]["content"]
    assert "炸了" in agent.messages[-1]["content"]


def test_non_string_result_is_serialized():
    def make_list():
        """返回 list。"""
        return ["a", "b"]

    agent = _agent_with([make_list])
    agent._execute(_tool_call("make_list", "{}"))

    assert json.loads(agent.messages[-1]["content"]) == ["a", "b"]


# ---- 任务绑定 ------------------------------------------------------------


def test_agent_records_events_into_task(monkeypatch, tmp_path):
    from wovra import task as task_module
    from wovra.task import Task

    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path)

    def echo(text: str):
        """原样返回。"""
        return text

    task = Task.create(goal="记录测试")
    agent = Agent(llm=_StubLLM(), tools=[echo], task=task)
    agent._execute(_tool_call("echo", '{"text": "hi"}'))

    kinds = [e["kind"] for e in task.history]
    assert "tool_call" in kinds and "tool_result" in kinds
    assert (tmp_path / task.id / "task.json").exists()  # 每次执行后都落盘


# ---- 路径安全 -------------------------------------------------------------


def test_read_file_blocks_escape_from_project_root():
    with pytest.raises(ValueError, match="路径越界"):
        read_file("../../etc/passwd")
