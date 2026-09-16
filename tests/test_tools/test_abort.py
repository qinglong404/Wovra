"""协作式中断（2026-09-16）：停止本轮要让**正在阻塞的工具**立刻罢手。

用户实测症状：命令在跑时点停止，界面卡在"停止中"，非得等命令自己跑完
（默认 60s，最长 600s）。根因是 `cancel_check` 只在步骤之间被查，而
`run_command` 一把 `wait()` 到底。这里覆盖两层：工具层的绑定语义，
以及 run_command 收到中断后**多快**返回（真起子进程，不用替身）。
"""

import sys
import threading
import time

import pytest

from wovra.tools import abort as abort_module


def test_abort_scope_binds_and_restores():
    """默认 False；绑定后跟着检查函数走；出栈还原（含嵌套与异常路径）。"""
    assert abort_module.abort_requested() is False
    flag = {"stop": False}
    with abort_module.abort_scope(lambda: flag["stop"]):
        assert abort_module.abort_requested() is False
        flag["stop"] = True
        assert abort_module.abort_requested() is True
        with abort_module.abort_scope(None):          # 嵌套：内层覆盖，出栈还原
            assert abort_module.abort_requested() is False
        assert abort_module.abort_requested() is True
    assert abort_module.abort_requested() is False      # 出栈还原

    with pytest.raises(RuntimeError):
        with abort_module.abort_scope(lambda: True):
            raise RuntimeError("工具自己炸了")
    assert abort_module.abort_requested() is False      # 异常路径也还原


def test_abort_check_errors_do_not_break_tools():
    """检查函数本身出错时按"没中断"处理——它不该让工具调用崩掉。"""
    with abort_module.abort_scope(lambda: 1 / 0):
        assert abort_module.abort_requested() is False


def test_run_command_stops_quickly_when_aborting(monkeypatch, tmp_path):
    """真起一个睡 30 秒的子进程，中途置中断标记：必须**秒级**返回并整树强杀。

    这就是用户报的那个卡住：改前要等满 30 秒（默认 timeout 是 60 秒）。
    """
    from wovra import tools as tools_module

    monkeypatch.setattr(tools_module.safety, "_audit", lambda detail: None)
    monkeypatch.setattr(tools_module.safety, "PROJECT_ROOT", tmp_path)
    flag = {"stop": False}
    sleeper = f'"{sys.executable}" -c "import time; time.sleep(30)"'
    started = time.monotonic()
    with abort_module.abort_scope(lambda: flag["stop"]):
        timer = threading.Timer(0.4, lambda: flag.__setitem__("stop", True))
        timer.start()
        try:
            out = tools_module.shell.run_command(sleeper)
        finally:
            timer.cancel()
    elapsed = time.monotonic() - started
    assert "已被中止" in out and "停止本轮" in out
    assert elapsed < 5, f"中止没生效：等了 {elapsed:.1f}s"
    # 出栈后标记还原，工具层不再认为处在中断状态
    assert abort_module.abort_requested() is False


def test_run_command_timeout_message_unchanged(monkeypatch, tmp_path):
    """超时路径的文案与口径不变（status.py 靠"命令执行失败（"判失败）。"""
    from wovra import tools as tools_module

    monkeypatch.setattr(tools_module.safety, "_audit", lambda detail: None)
    monkeypatch.setattr(tools_module.safety, "PROJECT_ROOT", tmp_path)
    out = tools_module.shell.run_command(
        f'"{sys.executable}" -c "import time; time.sleep(5)"', timeout=1)
    assert "命令执行失败（超时 1 秒被强制终止）" in out
    assert "已被中止" not in out                       # 没中断标记就不该走中止路径
