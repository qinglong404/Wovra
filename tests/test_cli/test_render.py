"""CLI 测试（自 test_cli.py 拆分，2026-09-11）。

本模块：test_render。"""

from types import SimpleNamespace
from wovra.task import Task

# 共享夹具/工具（_helpers.py 是原文件的公共头部）
from ._helpers import *  # noqa: F401,F403

def test_tool_result_green_on_success_red_on_failure(monkeypatch):
    from wovra import ui

    monkeypatch.setattr(ui, "_ENABLED", True)  # 测试捕获环境非 TTY，强制开启着色

    success_line = ui.tool_result("文件内容正常")
    failure_line = ui.tool_result("命令执行失败（exit_code=1）\nstderr:\nxxx")
    assert "\033[92m" in success_line and "成功" in success_line
    assert "\033[91m" in failure_line and "失败" in failure_line
    assert "exit_code=1" in failure_line  # 失败原因简要保留


def test_usage_line_shows_context_window():
    """usage 行显示上下文占用与窗口大小（常见工具的上下文余量显示）。"""
    from wovra import ui

    stats = {"seconds": 0, "total_tokens": 100, "prompt_tokens": 80,
             "completion_tokens": 20, "cached_tokens": 0, "cache_miss_tokens": 80}
    line = ui.usage_line(stats, maint={}, context=12_000, window=1_000_000)
    assert "上下文 12,000 tok（1.2%，窗口 1M）" in line


def test_assistant_markdown_label_uses_rich_style(capsys):
    """恢复回放的标签行经 rich 上色：手工 ANSI 不再显示成裸码。"""
    from wovra import ui

    ui.assistant_markdown("你好")
    out = capsys.readouterr().out
    assert "助手>" in out
    assert "92m[1m助手" not in out


def test_replay_history_pairs_tool_call_and_result(capsys):
    """回放：调用与结果配对成一行，工具名完整、失败只留首行原因。"""
    import re as _re

    from wovra.cli import _replay_history

    task = Task.create(goal="g")
    task.history = [
        {"time": "t", "kind": "user_input", "detail": "跑一下"},
        {"time": "t", "kind": "tool_call", "detail": "run_command({'command': 'x'})"},
        {"time": "t", "kind": "tool_result",
         "detail": "run_command -> 命令执行失败（exit_code=1）\nstdout: 详细输出若干"},
        {"time": "t", "kind": "final_answer", "detail": "完成了"},
    ]

    _replay_history(task, last_n=12)

    out = capsys.readouterr().out
    assert "[调用] run_command → 失败：命令执行失败（exit_code=1）" in out
    assert not _re.search(r"\[调用\] .\n", out)  # 修复前：工具名被截成首字母
    assert "stdout: 详细输出若干" not in out  # 失败不漏整段输出
    assert "助手>" in out and "92m[1m" not in out  # Markdown 标签不漏裸码


def test_color_detection_env_rules(monkeypatch):
    """颜色探测的环境规则：dumb/NO_COLOR/管道关，WOVRA_COLOR=1 强制开。

    回归背景：TERM=dumb 的环境（cron/CI/沙箱）此前照样输出 ANSI，
    转义码被打成 "?[91m" 乱码——颜色能力必须随终端能力降级。
    """
    from wovra import ui

    tty = SimpleNamespace(isatty=lambda: True)
    monkeypatch.setattr(ui.sys, "stdout", tty)
    monkeypatch.delenv("WOVRA_COLOR", raising=False)
    monkeypatch.delenv("NO_COLOR", raising=False)

    monkeypatch.setenv("TERM", "xterm-256color")
    assert ui._detect_color() is True  # 正常交互终端：开

    monkeypatch.setenv("TERM", "dumb")
    assert ui._detect_color() is False  # 哑终端：转义码会变乱码

    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("NO_COLOR", "")
    assert ui._detect_color() is False  # NO_COLOR 存在即关（无论值）

    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr(ui.sys, "stdout", SimpleNamespace(isatty=lambda: False))
    assert ui._detect_color() is False  # 管道/重定向：默认纯文本

    monkeypatch.setenv("WOVRA_COLOR", "1")
    assert ui._detect_color() is True  # 显式强制开，优先级最高


def test_answer_live_degrades_to_plain_stream_without_tty(monkeypatch, capsys):
    """非 TTY（管道/测试）下 Live 退化为纯文本流，输出顺序保持。"""
    from wovra import ui

    # 钉死降级路径：即便外部环境设了 WOVRA_COLOR=1，这里也必须走纯文本
    monkeypatch.setattr(ui, "_ENABLED", False)

    ui.answer_live_start()
    ui.answer_live_append("片段1")
    ui.answer_live_append("片段2")
    ui.answer_live_stop()
    out = capsys.readouterr().out
    assert "片段1片段2" in out
    ui.answer_live_start()
    ui.answer_live_stop()


def test_answer_live_renders_once_on_capable_terminal(monkeypatch):
    """支持原位刷新的终端：流式期间 Live 原位更新（transient），
    段落结束时整段 Markdown 静态渲染一次——不再整页重打刷屏。"""
    import io

    from rich.console import Console

    from wovra import ui

    file = io.StringIO()
    # 测试模拟的是"能力完备的交互终端"，所以环境也要钉成正常值：
    # rich 对 TERM=dumb 是三层叠加——is_interactive=False、抑制控制
    # 序列、无颜色系统；只传 force_terminal/force_interactive 时，
    # dumb 环境下 Live 照样退化成纯打印，测试就随环境漂移了
    monkeypatch.setenv("TERM", "xterm-256color")
    console = Console(
        file=file,
        force_terminal=True,
        force_interactive=True,
        width=80,
        legacy_windows=False,
    )
    monkeypatch.setattr(ui, "_console", console)
    monkeypatch.setattr(ui, "_ENABLED", True)

    ui.answer_live_start()
    ui.answer_live_append("# 标题\n\n第一段")
    assert ui._answer_live is not None  # Live 已启用
    ui.answer_live_append("\n\n第二段")
    ui.answer_live_stop()

    out = file.getvalue()
    assert "\x1b[" in out  # Live 确实走了原位刷新（有控制序列）
    assert "第一段" in out and "第二段" in out
    assert ui._answer_live is None


def test_report_view_renders_four_columns(monkeypatch, tmp_path):
    """人视图：机械渲染四栏目 + 子任务列表，零模型成本。"""
    from wovra import ui

    _use_tmp_root(monkeypatch, tmp_path)
    task = Task.create(goal="复刻小游戏")
    task.apply_state_patch({
        "completed": ["核心循环"], "current_status": "骨架可跑",
        "escalations": ["端口被占用：换端口还是杀进程？"],
        "experiments": ["打开页面验证行走"],
    })
    task.rounds = [{
        "seq": 1,
        "user_input": {"original": "开始", "normalized": "开始做骨架"},
        "events": [], "refined_index": {}, "end_state": "completed", "org_state": "done",
    }]
    child = Task.create(goal="渲染模块")
    child.parent_id = task.id
    child.save()
    task.save()

    out = ui.report_view(
        task, children=[{"id": child.id, "status": "in_progress", "goal": child.goal}]
    )

    assert "决策升级（等你拍板）" in out
    assert "端口被占用" in out
    assert "待办实验（需要你验证）" in out
    assert "打开页面验证行走" in out
    assert "R1✓ 开始做骨架" in out
    assert child.id in out


def test_report_view_renders_todo(monkeypatch, tmp_path):
    """终端报告里能看到大步/小步——此前只存在于模型上下文。"""
    from wovra import ui

    _use_tmp_root(monkeypatch, tmp_path)
    task = Task.create(goal="复刻小游戏")
    task.todo = {
        "milestone": {
            "goal": "骨架可跑",
            "acceptance": ["能移动"],
            "started_seq": 1,
            "deferred": [],
            "planned": True,
        },
        "steps": [{"text": "渲染循环", "done": False}],
    }
    task.save()

    out = ui.report_view(task)
    assert "## 当前阶段（大步 / 小步）" in out
    assert "骨架可跑" in out and "小步 0/1" in out
    assert "[ ] 渲染循环" in out
