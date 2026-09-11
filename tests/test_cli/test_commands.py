"""CLI 测试（自 test_cli.py 拆分，2026-09-11）。

本模块：test_commands。"""

from types import SimpleNamespace
from wovra import task as task_module
from wovra.cli import main as cli_main
from wovra.task import Task

# 共享夹具/工具（_helpers.py 是原文件的公共头部）
from ._helpers import *  # noqa: F401,F403

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


def test_bare_command_prints_help(monkeypatch, tmp_path, capsys):
    _use_tmp_root(monkeypatch, tmp_path)

    cli_main([])
    out = capsys.readouterr().out
    assert "usage:" in out
    assert "chat" in out

    cli_main(["help"])
    assert "usage:" in capsys.readouterr().out


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


def test_local_command_help_and_unknown(monkeypatch, tmp_path, capsys):
    from wovra.cli import _local_command

    _use_tmp_root(monkeypatch, tmp_path)
    task = Task.create(goal="x")
    task.save()

    _local_command("\\help", task)
    assert "本地命令" in capsys.readouterr().out

    _local_command("\\what", task)
    assert "未知本地命令" in capsys.readouterr().out


def test_local_command_report_and_todo(monkeypatch, tmp_path, capsys):
    """会话内 \\report / \\todo：与 `wovra report` 同一份机械渲染，零模型成本。"""
    from wovra.cli import _local_command

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

    _local_command("\\todo", task)
    out = capsys.readouterr().out
    assert "骨架可跑" in out and "[ ] 渲染循环" in out

    _local_command("/report", task)  # 斜杠前缀等价
    out = capsys.readouterr().out
    assert "任务报告" in out
    assert "当前阶段（大步 / 小步）" in out and "骨架可跑" in out


def test_toolbar_text_has_stage_and_hotkeys():
    """底栏常驻文本：当前阶段 + F2/F3 提示，且必须是单行纯文本。"""
    from wovra.cli import _toolbar_text

    task = Task.create(goal="x")
    task.todo = {
        "milestone": {"goal": "骨架可跑", "started_seq": 1, "planned": True},
        "steps": [{"text": "a", "done": True}],
    }
    text = _toolbar_text(task)
    assert "骨架可跑" in text and "F2" in text and "F3" in text
    assert "\n" not in text
    assert "\x1b[" not in text  # bottom_toolbar 不解析裸 ANSI，着色码会露出来


def test_make_prompt_session_none_when_not_tty(monkeypatch):
    """非 TTY（管道/重定向）不建 PromptSession，静默退化到 input()。"""
    from types import SimpleNamespace

    import wovra.cli.interactive as interactive

    monkeypatch.setattr(
        interactive.sys, "stdin", SimpleNamespace(isatty=lambda: False)
    )
    assert interactive._make_prompt_session(Task.create(goal="x")) is None


def test_prompt_hotkeys_return_local_commands(monkeypatch):
    """F2/F3 通过 prompt() 返回本地命令（等价于提交 \\report / \\todo）。

    用 prompt_toolkit 的管道输入驱动真实按键解析，不需要真终端。
    """
    from types import SimpleNamespace

    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    import wovra.cli.interactive as interactive

    monkeypatch.setattr(
        interactive.sys, "stdin", SimpleNamespace(isatty=lambda: True)
    )
    task = Task.create(goal="x")
    # xterm 功能键序列：F2 = SS3 Q，F3 = CSI 13~
    for sequence, expected in (("\x1bOQ", "\\report"), ("\x1b[13~", "\\todo")):
        with create_pipe_input() as pipe:
            session = interactive._make_prompt_session(
                task, input=pipe, output=DummyOutput()
            )
            pipe.send_text(sequence)
            assert session.prompt() == expected


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
    tools_module.background._BACKGROUND_TASKS["bg-1"] = {
        "proc": FakeProc(), "log": log, "pos": 0, "command": "",
        "label": "子任务 x", "session": task.id, "keep_alive": False,
    }
    try:
        _local_command("\\bg 1", task, None)
        out = capsys.readouterr().out
        assert "引擎层完成" in out
    finally:
        tools_module.background._BACKGROUND_TASKS.clear()
