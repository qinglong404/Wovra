"""Agent 运行时测试（自 test_agent.py 拆分，2026-09-11）。

本模块：test_usage。"""

from wovra import task as task_module
from wovra.agent import Agent
from wovra.task import Task

# 共享夹具/工具（_helpers.py 是原文件的公共头部）
from ._helpers import *  # noqa: F401,F403

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


def test_usage_record_includes_cache_fields(monkeypatch, tmp_path):
    """usage 落盘要带缓存命中/未命中/等效输入——缓存是成本差异的核心变量。"""
    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path)
    responses = [[_chunk(_delta(content="ok")), _chunk(usage=_usage(10, 2, 12, cached=4))]]
    task = Task.create(goal="g")
    # baseline：不触发整理调用，账目只来自这一次干活调用
    agent = Agent(llm=_StubLLM(responses), tools=[], task=task, context_mode="baseline")

    agent.run("问")

    detail = [e for e in task.history if e["kind"] == "usage"][-1]["detail"]
    assert "缓存命中 4 tok（40.0%）" in detail
    assert "等效输入 6 tok" in detail  # 未命中 6 + 命中 4/30 ≈ 6


def test_usage_record_includes_context_estimate(monkeypatch, tmp_path):
    """usage 落盘带 context=：上下文增长曲线可从 task.json 直接查得。"""
    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path)
    responses = [[_chunk(_delta(content="ok")), _chunk(usage=_usage(10, 2, 12))]]
    task = Task.create(goal="g")
    agent = Agent(llm=_StubLLM(responses), tools=[], task=task, context_mode="baseline")

    agent.run("问")

    assert agent.last_context_estimate > 0
    detail = [e for e in task.history if e["kind"] == "usage"][-1]["detail"]
    assert "context=" in detail


def test_ttft_recorded_in_stats_and_usage_line(monkeypatch, tmp_path):
    """时间仪器：每步首字延迟进 stats（合计 + 峰值），终端账单行可见。"""
    from wovra import ui

    responses = [[_chunk(_delta(content="hi")), _chunk(usage=_usage(10, 2, 12, cached=4))]]
    agent = _agent_with([], responses)
    agent.run("问")

    assert agent.last_stats["ttft_seconds"] > 0
    assert agent.last_stats["ttft_max"] > 0
    line = ui.usage_line(agent.last_stats)
    assert "首字" in line and "合计" in line
