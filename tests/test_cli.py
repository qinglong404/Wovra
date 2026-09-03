"""CLI 的离线测试：new / list / show 不依赖模型调用，可以完整覆盖。

run / chat 会发起真实模型调用，不属于单元测试范围——它们的逻辑
（任务加载、Agent 绑定）已被其他测试覆盖。
"""

import json

from wovra import task as task_module
from wovra.cli import main as cli_main


def _use_tmp_root(monkeypatch, tmp_path):
    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path)


def test_new_creates_task_and_prints_id(monkeypatch, tmp_path, capsys):
    _use_tmp_root(monkeypatch, tmp_path)

    cli_main(["new", "测试目标"])

    out = capsys.readouterr().out
    assert "已创建新会话" in out
    # 输出里的任务 id 应与磁盘上的目录对应
    task_id = out.split(":")[1].split()[0]
    data = json.loads((tmp_path / task_id / "task.json").read_text(encoding="utf-8"))
    assert data["goal"] == "测试目标"


def test_new_without_goal_starts_blank(monkeypatch, tmp_path, capsys):
    """新会话不强制目标：目标由 AI 随对话逐步成形。"""
    _use_tmp_root(monkeypatch, tmp_path)

    cli_main(["new"])

    out = capsys.readouterr().out
    task_id = out.split(":")[1].split()[0]
    data = json.loads((tmp_path / task_id / "task.json").read_text(encoding="utf-8"))
    assert data["goal"] == ""
    assert data["status"] == "in_progress"


def test_list_shows_existing_tasks(monkeypatch, tmp_path, capsys):
    _use_tmp_root(monkeypatch, tmp_path)

    cli_main(["new", "第一个任务"])
    cli_main(["new", "第二个任务"])
    capsys.readouterr()  # 丢弃 new 的输出

    cli_main(["list"])
    out = capsys.readouterr().out
    assert "第一个任务" in out
    assert "第二个任务" in out
    assert "进行中" in out


def test_show_prints_report(monkeypatch, tmp_path, capsys):
    _use_tmp_root(monkeypatch, tmp_path)

    cli_main(["new", "要被展示的目标"])
    task_id = capsys.readouterr().out.split(":")[1].split()[0]

    cli_main(["show", task_id])
    out = capsys.readouterr().out
    assert "# 任务报告：" in out
    assert "要被展示的目标" in out


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

    cli_main(["new", "较早的任务"])
    cli_main(["new", "较新的任务"])
    capsys.readouterr()

    # 同一秒内创建的两个任务 updated_at 相同，排序会退化为目录顺序；
    # 显式把第一个任务的时间改早，让"最近更新" deterministic
    import json as _json

    for directory in task_module.TASKS_ROOT.iterdir():
        state = directory / "task.json"
        data = _json.loads(state.read_text(encoding="utf-8"))
        if data["goal"] == "较早的任务":
            data["updated_at"] = "2020-01-01T00:00:00"
            state.write_text(_json.dumps(data, ensure_ascii=False), encoding="utf-8")

    cli_main(["list"])
    lines = [l for l in capsys.readouterr().out.splitlines() if l.strip()]
    task_lines = [l for l in lines if "较早的任务" in l or "较新的任务" in l]
    assert len(task_lines) == 2
    # 最近更新的排在第 1 位
    assert task_lines[0].strip().startswith("1")
    assert "较新的任务" in task_lines[0]
    assert task_lines[1].strip().startswith("2")


def test_resolve_numeric_id_maps_to_recency_order(monkeypatch, tmp_path, capsys):
    _use_tmp_root(monkeypatch, tmp_path)

    cli_main(["new", "较早"])
    first_id = sorted(
        p.name for p in task_module.TASKS_ROOT.iterdir() if (p / "task.json").exists()
    )[0]
    cli_main(["new", "较晚"])
    capsys.readouterr()

    # 同秒创建导致 updated_at 相同、排序不稳定；把第一个任务时间改早
    import json as _json

    for directory in task_module.TASKS_ROOT.iterdir():
        state = directory / "task.json"
        data = _json.loads(state.read_text(encoding="utf-8"))
        if data["goal"] == "较早":
            data["updated_at"] = "2020-01-01T00:00:00"
            state.write_text(_json.dumps(data, ensure_ascii=False), encoding="utf-8")

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
