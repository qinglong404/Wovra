"""tokens 分类估算与用量展示的测试。全部离线（估算器走启发式）。"""

import pytest

from wovra import tokens, ui


@pytest.fixture(autouse=True)
def _heuristic_estimator(monkeypatch):
    """强制走字符启发式：测试不依赖 tiktoken 词表（可能未下载）。"""
    monkeypatch.setattr(tokens, "_encoding", None)


def test_estimate_counts_cjk_and_ascii_differently():
    assert tokens.estimate("") == 0
    assert tokens.estimate("你好") == 2  # 全角字符 1 字 1 token
    assert tokens.estimate("abcdefgh") == 2  # 8 个 ASCII 字符 ≈ 2 token
    assert tokens.estimate("a") == 1  # 非空至少 1


def test_breakdown_separates_six_categories():
    messages = [
        {"role": "system", "content": "system提示词+注入内容不在这里算"},
        {"role": "user", "content": "用户问题"},
        {"role": "assistant", "content": "中间回答", "tool_calls": [
            {"function": {"name": "f", "arguments": "{}"}}
        ]},
        {"role": "tool", "content": "工具结果内容"},
    ]
    result = tokens.breakdown(
        system_prompt="你是助手",
        task_context="# 当前任务\n目标：xx",
        tool_schemas=[{"type": "function"}],
        messages=messages,
    )

    assert set(result) == set(tokens.CATEGORIES)
    assert result["system"] == tokens.estimate("你是助手")
    assert result["context"] == tokens.estimate("# 当前任务\n目标：xx")
    assert result["user"] == tokens.estimate("用户问题")
    assert result["tool"] == tokens.estimate("工具结果内容")
    # 助手消息 = 正文 + tool_calls 参数 JSON
    assert result["assistant"] == tokens.estimate("中间回答") + tokens.estimate(
        '{"name": "f", "arguments": "{}"}'
    )


def test_breakdown_without_tools_or_context():
    result = tokens.breakdown("", "", [], [{"role": "user", "content": "hi"}])
    assert result["tools"] == 0
    assert result["context"] == 0
    assert result["user"] > 0


def test_usage_line_shows_turns_steps_tools_and_cache():
    stats = {
        "seconds": 2.0,
        "turn": 2,
        "llm_calls": 3,
        "tool_calls": 2,
        "prompt_tokens": 60,
        "completion_tokens": 40,
        "total_tokens": 100,
        "reasoning_tokens": 0,
        "cached_tokens": 40,
        "cache_miss_tokens": 20,
        "prompt_breakdown": {"user": 10},
    }

    line = ui.usage_line(stats)

    assert "轮次 第2轮" in line
    assert "步数 3" in line
    assert "工具调用 2 次" in line
    # 占比保留 1 位小数：40/60 与 20/60
    assert "缓存命中 40 tok（66.7%）" in line
    assert "未命中 20 tok（33.3%）" in line


def test_usage_line_treats_missing_cache_report_as_zero_hit():
    stats = {
        "seconds": 1.0,
        "turn": 1,
        "llm_calls": 1,
        "tool_calls": 0,
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "total_tokens": 15,
        "reasoning_tokens": 0,
        "cached_tokens": 0,  # 服务端漏报 → 保守按 0 命中
        "cache_miss_tokens": 10,
        "prompt_breakdown": {},
    }

    line = ui.usage_line(stats)

    assert "缓存命中 0 tok（0.0%）" in line
    assert "未命中 10 tok（100.0%）" in line


def test_usage_line_scales_breakdown_to_real_total():
    stats = {
        "seconds": 1.0,
        "prompt_tokens": 100,
        "completion_tokens": 50,
        "total_tokens": 150,
        "reasoning_tokens": 0,
        # 估算总和 200 → 按 100/200 缩放，各项之和应等于真实输入 100
        "prompt_breakdown": {
            "system": 40, "context": 20, "tools": 60,
            "user": 30, "assistant": 30, "tool": 20,
        },
    }

    line = ui.usage_line(stats)

    assert "输入构成" in line
    # 缩放后各分项之和 = 真实输入 100；占比取估算份额，保留 1 位小数
    assert "系统提示词 20 tok（20.0%）" in line  # 40 * 0.5
    assert "工具定义 30 tok（30.0%）" in line   # 60 * 0.5
    assert "用户消息 15 tok（15.0%）" in line


def test_usage_line_without_breakdown_or_usage():
    # 没有 usage：只说明情况，不显示构成
    line = ui.usage_line({"seconds": 1.0, "total_tokens": 0})
    assert "未返回 usage" in line
    assert "输入构成" not in line
