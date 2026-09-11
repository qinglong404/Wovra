"""Agent 运行时测试（自 test_agent.py 拆分，2026-09-11）。

本模块：test_support。"""

import json
import pytest
from wovra import task as task_module
from wovra.agent import Agent, _schema_of
from wovra.tools import read_file
from wovra.task import Task

# 共享夹具/工具（_helpers.py 是原文件的公共头部）
from ._helpers import *  # noqa: F401,F403

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


def test_schema_unwraps_optional_annotation():
    """Optional[int] 参数在 schema 里应为 integer，而不是退化为 string。"""

    def demo(timeout: int | None = None):
        """带可选整数的工具。"""

    schema = _schema_of(demo)["function"]["parameters"]["properties"]
    assert schema["timeout"] == {"type": "integer"}


def test_tool_args_with_unpaired_surrogates_are_sanitized(monkeypatch, tmp_path):
    """回归（实测 2026-09-05）：模型把 emoji 拆成不成对 \\uD83D 转义时，
    参数清洗保证工具执行与落盘都不崩。"""
    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path)
    seen = {}

    def edit_file(**kwargs):
        seen.update(kwargs)
        return "已修改"

    responses = [[_chunk(_delta(content="ok"))]]
    task = Task.create(goal="g")
    agent = Agent(llm=_StubLLM(responses), tools=[edit_file], task=task)
    raw = json.dumps({"path": "a.md", "old_text": "x", "new_text": "y"})
    raw = raw[:-2] + '\ud83d"}'  # 注入不成对代理转义（json.loads 合法、UTF-8 编码非法）

    agent._execute("c1", "edit_file", raw)

    assert "\ud83d" not in str(seen)
    assert "\ufffd" in seen["new_text"]
    task.save()  # 含该工具调用事件的会话照常落盘


def test_default_max_turns_is_200():
    """默认步数上限 200（安全网而非配额；60 时代用户实测两次撞顶）。"""
    task = Task.create(goal="x")
    agent = Agent(llm=_StubLLM(), tools=[], task=task)
    assert agent.max_turns == 200


def test_read_file_blocks_escape_from_project_root():
    with pytest.raises(ValueError, match="路径越界"):
        read_file("../../etc/passwd")


def test_maint_tools_default_is_full_array(monkeypatch):
    """缓存复议（2026-09-11）：维护调用的 tools 默认 = 与工作调用**完全
    相同的**数组。

    tools 是请求前缀的一部分，数组一差分叉，整段前缀缓存即失效。实测收窄
    的代价是 org 首跳命中 0.4%（≈0.6 元/批），而收益（防跑偏）不成立——
    维护调用不执行工具，漂移只让这批没产物（已有带诊断重发兜底）。
    """
    from wovra.agent.support import maint_tools

    schemas = [
        {"type": "function", "function": {"name": "read_file"}},
        {"type": "function", "function": {"name": "write_file"}},
        {"type": "function", "function": {"name": "submit_organization"}},
    ]
    monkeypatch.delenv("WOVRA_MAINT_NARROW_TOOLS", raising=False)
    tools = maint_tools(schemas, "submit_organization")
    assert [t["function"]["name"] for t in tools] == [
        "read_file", "write_file", "submit_organization"
    ]
    # 是副本而非原列表（调用方改动不污染 _schemas）
    assert tools is not schemas


def test_maint_tools_narrow_mode_via_env(monkeypatch):
    """回滚开关：WOVRA_MAINT_NARROW_TOOLS=1 切回收窄模式（只留单一出口）。"""
    from wovra.agent.support import maint_tools

    schemas = [
        {"type": "function", "function": {"name": "read_file"}},
        {"type": "function", "function": {"name": "submit_domains"}},
    ]
    for value in ("1", "true", "ON"):
        monkeypatch.setenv("WOVRA_MAINT_NARROW_TOOLS", value)
        tools = maint_tools(schemas, "submit_domains")
        assert [t["function"]["name"] for t in tools] == ["submit_domains"]
    monkeypatch.setenv("WOVRA_MAINT_NARROW_TOOLS", "0")
    assert len(maint_tools(schemas, "submit_domains")) == 2


def test_foreign_tool_calls_recorded(monkeypatch, tmp_path):
    """漂移观测：恒定数组下模型若调用了非出口工具，留痕（无害但可查）。

    这是复议问题"恒定数组是否真引来跑偏"的取数口径。
    """
    from wovra.agent import Agent
    from wovra.task import Task

    ordered = [
        {"name": "write_file", "arguments": "{}"},
        {"name": "submit_organization", "arguments": "{}"},
    ]
    assert Agent._foreign_tool_calls(ordered, "submit_organization") == [
        "write_file"
    ]
    assert Agent._foreign_tool_calls(
        [{"name": "submit_organization"}], "submit_organization"
    ) == []
