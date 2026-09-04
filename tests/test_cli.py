"""CLI 的离线测试：list / delete 不依赖模型调用，可以完整覆盖。

chat / run 会发起真实模型调用，不属于单元测试范围——它们的逻辑
（任务加载、Agent 绑定）已被其他测试覆盖。
"""

import json

import pytest

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
