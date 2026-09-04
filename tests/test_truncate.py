"""Runtime 索引行生成器的测试：零 LLM 成本的 Event 生成规则。"""

from wovra.truncate import (
    make_event,
    render_round_events,
)


def test_user_event_truncates_long_input():
    event = make_event("R1-E01", "user", {"role": "user", "content": "好" * 300})
    assert len(event["truncated"]) <= 121  # 120 字 + 省略号
    assert event["message"]["content"] == "好" * 300  # message 原样保留


def test_tool_result_failure_status_is_detected():
    event = make_event(
        "R1-E02", "tool_result",
        {"role": "tool", "tool_call_id": "c1", "content": "命令执行失败（exit_code=1）"},
        tool_name="run_command",
    )
    assert event["status"] == "error"
    assert "失败" in event["truncated"]  # run_command 专用规则保留失败头


def test_run_command_truncator_extracts_error_lines():
    content = "exit_code=1\nstdout:\n(无输出)\nstderr:\nFileNotFoundError: no such file"
    event = make_event(
        "R2-E04", "tool_result",
        {"role": "tool", "tool_call_id": "c2", "content": content},
        tool_name="run_command",
    )
    assert "关键错误" in event["truncated"]
    assert "FileNotFoundError" in event["truncated"]


def test_tool_result_content_is_never_truncated():
    """轮内赦免：大结果原样进上下文（截断曾诱发"读→失忆→重读"死循环）。"""
    big = "日志" * 3000  # 6000 字符
    event = make_event(
        "R2-E05", "tool_result",
        {"role": "tool", "tool_call_id": "c3", "content": big},
        tool_name="read_file",
    )
    assert event["message"]["content"] == big  # 原样，不截断
    assert "full" not in event  # 不再需要分离的原文层（message 即全文）
    assert "日志" in event["truncated"][:100]  # 索引行照常生成（供整理/降档用）


def test_render_round_events_lists_ids():
    round_data = {
        "events": [
            {"id": "R1-E01", "type": "user", "status": "", "truncated": "第一问"},
            {"id": "R1-E02", "type": "tool_result", "status": "error", "truncated": "失败，…"},
        ]
    }
    lines = render_round_events(round_data).splitlines()
    assert lines[0].startswith("[R1-E01]")
    assert "[error]" in lines[1]
