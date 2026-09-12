"""注册表 window 字段口径：模型上下文窗口（非整理水位）。"""

from wovra.agent import Agent

from ._helpers import _StubLLM


def test_agent_window_is_context_limit():
    """_agent_window = 模型上下文上限（默认 1M），不再误取整理水位。"""
    agent = Agent(llm=_StubLLM(), tools=[])
    assert agent.context_limit == 1_000_000
    assert agent._agent_window() == 1_000_000
    assert agent._org_watermark == 100_000   # 水位独立存在，仅作整理阈值


def test_touch_view_context_overwrites_stale_window():
    """老会话 registry 里误存的水位 100K 要被随活动校正成真实窗口。"""
    agent = Agent(llm=_StubLLM(), tools=[])
    entry = {"id": "Main", "name": "主", "window": 100_000}
    agent._registry_entry_for = lambda view: entry
    agent._touch_view_context("Main", 16_800)
    assert entry["ctx_cur"] == 16_800
    assert entry["window"] == 1_000_000
