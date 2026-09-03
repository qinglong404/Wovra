"""Runtime 截断器的测试：零 LLM 成本的 Event 生成规则。"""

from wovra.truncate import (
    SAFE_RESULT_LIMIT,
    make_event,
    render_round_events,
)


def test_user_event_truncates_long_input():
    event = make_event("R1-E01", "user", {"role": "user", "content": "好" * 300})
    assert len(event["truncated"]) <= 121  # 120 字 + 省略号
    assert event["message"]["content"] == "好" * 300  # Full 原样保留


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


def test_safety_limit_keeps_full_and_marks_reference():
    big = "日志" * 3000  # 6000 字符 > SAFE_RESULT_LIMIT
    event = make_event(
        "R2-E05", "tool_result",
        {"role": "tool", "tool_call_id": "c3", "content": big},
        tool_name="read_file",
    )
    assert event["full"] == big  # 原文完整保存
    assert len(event["message"]["content"]) < SAFE_RESULT_LIMIT + 100  # 上下文只放安全范围
    assert "expand_history" in event["message"]["content"]  # 指回 Full 的引用


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
