"""CLI 测试（自 test_cli.py 拆分，2026-09-11）。

本模块：test_session。"""

from wovra import task as task_module
from wovra.task import Task

# 共享夹具/工具（_helpers.py 是原文件的公共头部）
from ._helpers import *  # noqa: F401,F403

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
