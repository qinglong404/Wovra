"""CLI 的离线测试：list / delete 不依赖模型调用，可以完整覆盖。

chat / run 会发起真实模型调用，不属于单元测试范围——它们的逻辑
（任务加载、Agent 绑定）已被其他测试覆盖。
"""

import json
import threading
from types import SimpleNamespace

import pytest

from wovra import cli as cli_module
from wovra import task as task_module
from wovra.cli import main as cli_main
from wovra.task import Task


def _use_tmp_root(monkeypatch, tmp_path):
    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path)


def _seed_task(goal: str, updated_at: str | None = None) -> str:
    """直接落盘一个任务（new 命令已移除，测试自行构造数据）。"""
    task = Task.create(goal=goal)
    if updated_at:
        task.updated_at = updated_at
    task.save()
    return task.id


def test_list_shows_existing_tasks(monkeypatch, tmp_path, capsys):
    _use_tmp_root(monkeypatch, tmp_path)
    _seed_task("第一个任务")
    _seed_task("第二个任务")

    cli_main(["list"])
    out = capsys.readouterr().out
    assert "第一个任务" in out
    assert "第二个任务" in out
    assert "进行中" in out


def test_run_with_missing_task_fails_friendly(monkeypatch, tmp_path):
    _use_tmp_root(monkeypatch, tmp_path)

    try:
        cli_main(["run", "不存在的任务", "你好"])
    except SystemExit as e:
        assert "任务不存在" in str(e)
    else:
        raise AssertionError("应该以 SystemExit 报错")


def test_list_assigns_recency_numbers(monkeypatch, tmp_path, capsys):
    _use_tmp_root(monkeypatch, tmp_path)
    # updated_at 显式错开，避免"最近更新"排序受同秒创建影响
    _seed_task("较早的任务", updated_at="2020-01-01T00:00:00")
    _seed_task("较新的任务")

    cli_main(["list"])
    lines = [l for l in capsys.readouterr().out.splitlines() if l.strip()]
    task_lines = [l for l in lines if "较早的任务" in l or "较新的任务" in l]
    assert len(task_lines) == 2
    # 最近更新的排在第 1 位
    assert task_lines[0].strip().startswith("1")
    assert "较新的任务" in task_lines[0]
    assert task_lines[1].strip().startswith("2")


def test_resolve_numeric_id_maps_to_recency_order(monkeypatch, tmp_path):
    _use_tmp_root(monkeypatch, tmp_path)
    first_id = _seed_task("较早", updated_at="2020-01-01T00:00:00")
    _seed_task("较晚")

    from wovra.cli import _resolve_task_id

    # 编号 1 = 最近更新的任务
    assert _resolve_task_id("1") != first_id
    # 完整 id 原样通过
    assert _resolve_task_id(first_id) == first_id
    # 超范围编号友好报错
    try:
        _resolve_task_id("99")
    except SystemExit as e:
        assert "编号 99 不存在" in str(e)
    else:
        raise AssertionError("应该以 SystemExit 报错")


def test_bare_command_prints_help(monkeypatch, tmp_path, capsys):
    _use_tmp_root(monkeypatch, tmp_path)

    cli_main([])
    out = capsys.readouterr().out
    assert "usage:" in out
    assert "chat" in out

    cli_main(["help"])
    assert "usage:" in capsys.readouterr().out


def test_tool_result_green_on_success_red_on_failure(monkeypatch):
    from wovra import ui

    monkeypatch.setattr(ui, "_ENABLED", True)  # 测试捕获环境非 TTY，强制开启着色

    success_line = ui.tool_result("文件内容正常")
    failure_line = ui.tool_result("命令执行失败（exit_code=1）\nstderr:\nxxx")
    assert "\033[92m" in success_line and "成功" in success_line
    assert "\033[91m" in failure_line and "失败" in failure_line
    assert "exit_code=1" in failure_line  # 失败原因简要保留


def test_system_prompt_matches_mode_and_environment(monkeypatch):
    """系统提示词按模式写实：managed 才提 expand_history，并附带运行环境。"""
    import os as _os

    from wovra.agent import MODE_BASELINE, MODE_MANAGED
    from wovra.cli import _system_prompt

    managed = _system_prompt(MODE_MANAGED)
    baseline = _system_prompt(MODE_BASELINE)
    assert "expand_history" in managed
    assert "expand_history" not in baseline  # baseline 没注册这个工具，不能预告
    # R5 教训（新需求整体重写文件被用户批评）固化为工程纪律，两模式都有
    for prompt in (managed, baseline):
        assert "模块化多文件结构" in prompt
        assert "禁止为一条新需求整体重写文件" in prompt
        # D 组实证驱动的两条：批量调用省首字延迟；观感是前端评分大头
        assert "批量发出" in prompt
        assert "一眼全黑" in prompt
        assert "验证分层" in prompt
    # 运行环境信息防止模型在 Windows 上跑类 Unix 命令
    if _os.name == "nt":
        assert "cmd.exe" in managed and "Windows" in managed
    else:
        assert "Linux" in managed


# ---- delete：编号/完整 id、确认交互、--force、活锁拒绝 -----------------------


def test_delete_removes_directory_and_keeps_others(monkeypatch, tmp_path, capsys):
    _use_tmp_root(monkeypatch, tmp_path)
    doomed = _seed_task("要删的")
    survivor = _seed_task("要留的")

    monkeypatch.setattr("builtins.input", lambda _: "y")
    cli_main(["delete", doomed])

    assert not (task_module.TASKS_ROOT / doomed).exists()
    assert (task_module.TASKS_ROOT / survivor).exists()
    assert "已删除会话" in capsys.readouterr().out


def test_delete_decline_keeps_task(monkeypatch, tmp_path, capsys):
    _use_tmp_root(monkeypatch, tmp_path)
    task_id = _seed_task("不删的")

    monkeypatch.setattr("builtins.input", lambda _: "n")
    cli_main(["delete", task_id])

    assert (task_module.TASKS_ROOT / task_id).exists()
    assert "已取消" in capsys.readouterr().out


def test_delete_force_skips_confirm(monkeypatch, tmp_path, capsys):
    _use_tmp_root(monkeypatch, tmp_path)
    task_id = _seed_task("强制删")

    def _no_prompt(_):
        raise AssertionError("不该出现确认提示")

    monkeypatch.setattr("builtins.input", _no_prompt)
    cli_main(["delete", task_id, "--force"])

    assert not (task_module.TASKS_ROOT / task_id).exists()


def test_delete_refuses_when_locked_by_live_process(monkeypatch, tmp_path):
    _use_tmp_root(monkeypatch, tmp_path)
    import os as _os

    task_id = _seed_task("正被使用")
    lock = task_module.TASKS_ROOT / task_id / ".lock"
    lock.write_text(str(_os.getpid()), encoding="utf-8")  # 当前测试进程=活持有者

    try:
        cli_main(["delete", task_id])
    except SystemExit as e:
        assert "正在另一个进程中使用" in str(e)
    else:
        raise AssertionError("活进程持锁时应拒绝删除")
    assert (task_module.TASKS_ROOT / task_id).exists()


def test_delete_missing_task_fails_friendly(monkeypatch, tmp_path):
    _use_tmp_root(monkeypatch, tmp_path)

    try:
        cli_main(["delete", "不存在的任务"])
    except SystemExit as e:
        assert "任务不存在" in str(e)
    else:
        raise AssertionError("应该以 SystemExit 报错")


# ---- 会话锁（跨平台探活见 test_lock） ----------------------------------------


def test_session_lock_rejects_live_holder_and_cleans_stale(tmp_path):
    """会话锁：活进程持锁 → 拒绝；死进程的陈旧锁 → 清除后放行。"""
    import os as _os

    from wovra import task as task_module
    from wovra.cli import _acquire_session_lock
    from wovra.task import Task

    monkeypatch_root = tmp_path
    original_root = task_module.TASKS_ROOT
    task_module.TASKS_ROOT = monkeypatch_root
    try:
        task = Task.create(goal="锁测试")
        task.save()

        # 当前测试进程持有锁（存活）→ 第二次获取必须被拒
        _acquire_session_lock(task)
        try:
            _acquire_session_lock(task)
        except SystemExit as e:
            assert "正在另一个进程中使用" in str(e)
        else:
            raise AssertionError("活进程持锁时应拒绝")

        # 持锁进程"死亡"（伪造一个必然不存在的 PID）→ 陈旧锁清除后放行
        dead = tmp_path / task.id / ".lock"
        dead.write_text("999999999", encoding="utf-8")
        _acquire_session_lock(task)  # 不应抛异常
        assert dead.read_text() == str(_os.getpid())  # 锁被当前进程重新持有
    finally:
        task_module.TASKS_ROOT = original_root


def test_config_error_exits_with_friendly_message(monkeypatch, capsys):
    """配置错误由 main 统一打印提示并以非零码退出，不打 traceback。"""
    from wovra.llm import LLMConfigError

    def fake_cmd(args):
        raise LLMConfigError("模型配置问题：端点上不存在该模型")

    monkeypatch.setattr("wovra.cli.cmd_chat", fake_cmd)
    with pytest.raises(SystemExit) as excinfo:
        cli_main(["chat"])
    assert excinfo.value.code == 1
    assert "模型配置问题" in capsys.readouterr().out


def test_run_turn_propagates_config_error_not_turn_limit():
    """回归：LLMConfigError 不能被 _run_turn 的 RuntimeError 分支吞成"步数超限"。"""
    from types import SimpleNamespace

    from wovra.cli import _run_turn
    from wovra.llm import LLMConfigError

    finalized = []

    def _raise(*args, **kwargs):
        raise LLMConfigError("模型配置问题")

    agent = SimpleNamespace(run=_raise, finalize_round=finalized.append)
    with pytest.raises(LLMConfigError):
        _run_turn(agent, "hi")
    assert finalized == ["open"]  # 轮次已收尾为开放，异常原样上抛


def test_usage_line_shows_context_window():
    """usage 行显示上下文占用与窗口大小（常见工具的上下文余量显示）。"""
    from wovra import ui

    stats = {"seconds": 0, "total_tokens": 100, "prompt_tokens": 80,
             "completion_tokens": 20, "cached_tokens": 0, "cache_miss_tokens": 80}
    line = ui.usage_line(stats, maint={}, context=12_000, window=1_000_000)
    assert "上下文 12,000 tok（1.2%，窗口 1M）" in line


def test_resolve_mode_defaults_and_persistence(monkeypatch, tmp_path):
    """模式解析：显式 --mode > 会话记录 > 默认 managed；切换写回会话。"""
    _use_tmp_root(monkeypatch, tmp_path)
    from wovra.cli import _resolve_mode

    task = Task.create(goal="g")
    task.save()
    assert _resolve_mode(None, task) == "managed"
    assert task.mode == "managed"

    task.mode = "baseline"
    task.save()
    assert _resolve_mode(None, task) == "baseline"  # 恢复时沿用会话记录
    assert _resolve_mode("managed", task) == "managed"  # 显式指定优先
    assert task.mode == "managed"  # 切换已写回


def test_resume_command_carries_baseline_mode():
    """续用命令：baseline 会话带 --mode；managed 是默认值可省略。"""
    from wovra.cli import _resume_command

    task = Task.create(goal="g")
    task.mode = "baseline"
    assert _resume_command(task) == f"wovra chat {task.id} --mode baseline"
    task.mode = "managed"
    assert _resume_command(task) == f"wovra chat {task.id}"


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


# ---- 人机协同报告（组织运行时 V1） -------------------------------------------


def test_workspace_instructions_injected_from_agents_md(monkeypatch, tmp_path):
    """工作区指令包（1.3）：AGENTS.md 追加进系统提示词，缺失则跳过。"""
    (tmp_path / "AGENTS.md").write_text(
        "测试用 uv run pytest；不要动 .wovra/ 目录", encoding="utf-8"
    )
    monkeypatch.setattr(cli_module, "PROJECT_ROOT", tmp_path)

    prompt = cli_module._system_prompt("managed")

    assert "[工作区指令]" in prompt
    assert "uv run pytest" in prompt and "不要动 .wovra/" in prompt


def test_workspace_instructions_absent_is_silent(monkeypatch, tmp_path):
    monkeypatch.setattr(cli_module, "PROJECT_ROOT", tmp_path)
    prompt = cli_module._system_prompt("managed")
    assert "[工作区指令]" not in prompt


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


def test_report_command_renders_task(monkeypatch, tmp_path, capsys):
    """wovra report 命令：加载任务 + 扫描子任务 + 机械渲染。"""
    from types import SimpleNamespace

    from wovra.cli import cmd_report

    _use_tmp_root(monkeypatch, tmp_path)
    task = Task.create(goal="报告命令冒烟")
    task.save()

    cmd_report(SimpleNamespace(task_id=task.id))

    out = capsys.readouterr().out
    assert "报告命令冒烟" in out
    assert "里程碑线" in out
    assert "子任务" in out


# ---- 本地命令（\ 前缀，组织运行时 V1） ---------------------------------------


def test_local_command_help_and_unknown(monkeypatch, tmp_path, capsys):
    from wovra.cli import _local_command

    _use_tmp_root(monkeypatch, tmp_path)
    task = Task.create(goal="x")
    task.save()

    _local_command("\\help", task)
    assert "本地命令" in capsys.readouterr().out

    _local_command("\\what", task)
    assert "未知本地命令" in capsys.readouterr().out


def test_local_command_bg_list_smoke(monkeypatch, tmp_path, capsys):
    from wovra.cli import _local_command

    _use_tmp_root(monkeypatch, tmp_path)
    task = Task.create(goal="x")
    task.save()

    _local_command("\\bg", task)  # 无后台任务也不抛异常
    assert capsys.readouterr().out


def test_local_command_undo_and_slash_prefix(monkeypatch, tmp_path, capsys):
    """undo 撤销开放轮（成本记录保留）；斜杠前缀等价；闭合轮拒撤。"""
    from types import SimpleNamespace

    from wovra.cli import _local_command

    _use_tmp_root(monkeypatch, tmp_path)
    persisted = {}
    agent = SimpleNamespace(
        rounds=[
            {"seq": 1, "end_state": "completed", "events": [1, 2]},
            {"seq": 2, "end_state": "open", "events": [1, 2, 3, 4]},
        ],
        _persist_rounds=lambda: persisted.update(n=len(agent.rounds)),
    )
    task = Task.create(goal="x")
    task.save()

    _local_command("/undo", task, agent)  # / 与 \ 等价
    out = capsys.readouterr().out
    assert "已撤销" in out and "4 条事件" in out
    assert persisted["n"] == 1
    assert agent.rounds[-1]["end_state"] == "completed"

    _local_command("/undo", task, agent)  # 闭合轮拒撤
    assert "只撤销开放中的轮次" in capsys.readouterr().out


def test_local_command_bg_numeric_id(monkeypatch, tmp_path, capsys):
    """bg 1 == bg bg-1：编号短写也能看后台输出。"""
    from wovra.cli import _local_command
    from wovra import tools as tools_module

    _use_tmp_root(monkeypatch, tmp_path)
    task = Task.create(goal="x")
    task.save()
    log = tmp_path / "bg-1.log"
    log.write_text("子任务进展：引擎层完成 40%", encoding="utf-8")

    class FakeProc:
        returncode = 0

        def poll(self):
            return 0

    from wovra import tools as tools_module

    tools_module.set_current_session(task.id)
    tools_module._BACKGROUND_TASKS["bg-1"] = {
        "proc": FakeProc(), "log": log, "pos": 0, "command": "",
        "label": "子任务 x", "session": task.id, "keep_alive": False,
    }
    try:
        _local_command("\\bg 1", task, None)
        out = capsys.readouterr().out
        assert "引擎层完成" in out
    finally:
        tools_module._BACKGROUND_TASKS.clear()
