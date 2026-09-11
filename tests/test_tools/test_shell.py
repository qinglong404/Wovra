"""工具层测试（自 test_tools.py 拆分，2026-09-11）。

本模块：test_shell。"""

import os as _os
import re
import pytest
from wovra.tools import FAILURE_MARKERS, run_command, write_file
from wovra.tools.safety import _ask_yes_no, _confirm_reason

# 共享夹具/工具（_helpers.py 是原文件的公共头部）
from ._helpers import *  # noqa: F401,F403

def test_run_command_executes_and_returns_output():
    result = run_command("echo hello-wovra")
    assert "exit_code=0" in result
    assert "hello-wovra" in result
    # 耗时进结果（DeepSeek 点评采纳）：模型看得到命令代价才有自调节信号
    assert re.search(r"耗时 \d+\.\d+s", result)


def test_run_command_reports_failure_exit_code():
    # 用界内路径制造失败（界外绝对路径现在会被安全层拒绝，测不到退出码）
    result = run_command("ls nonexistent-path-wovra")
    assert "命令执行失败" in result
    assert any(marker in result for marker in FAILURE_MARKERS)


def test_run_command_timeout_kills_whole_tree(monkeypatch):
    """超时后返回失败消息、不留孤儿进程：孙进程攥着管道曾把清理阶段
    永久挂死（Windows），整树击杀后限时清理必然快速返回。"""
    import os as _os
    import subprocess as _subprocess
    import time as _time

    from wovra import tools as tools_module

    monkeypatch.setattr(tools_module.shell, "_COMMAND_TIMEOUT", 1)
    # 前台常驻命令：shell 外壳下跑一个远超超时时间的休眠子进程
    sleeper = "ping -n 11 127.0.0.1" if _os.name == "nt" else "sleep 10"

    started = _time.monotonic()
    result = run_command(sleeper)
    elapsed = _time.monotonic() - started

    assert "超时 1 秒被强制终止" in result
    assert elapsed < 8  # 清理若挂死会远超此值

    if _os.name == "nt":
        listing = _subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq ping.exe"],
            capture_output=True,
        ).stdout.decode("utf-8", errors="replace")
        assert "ping.exe" not in listing  # 孙进程不留孤儿


def test_run_command_respects_custom_timeout(monkeypatch):
    """timeout 参数生效：1-600 秒可调，超时消息带实际秒数。"""
    import os as _os

    sleeper = "ping -n 11 127.0.0.1" if _os.name == "nt" else "sleep 10"
    result = run_command(sleeper, timeout=1)
    assert "超时 1 秒被强制终止" in result


def test_run_command_marks_truncated_output(monkeypatch, tmp_path):
    """输出超限时显式标注并保留首尾——静默截断曾让模型误诊白跑一轮。"""
    from wovra import tools as tools_module

    monkeypatch.setattr(tools_module.safety, "PROJECT_ROOT", tmp_path)
    write_file("big.txt", "甲" * 4000 + "尾部关键结论")
    cmd = "type big.txt" if _os.name == "nt" else "cat big.txt"
    result = run_command(cmd)
    assert "字符已省略" in result
    assert "4,00" in result  # 原文体量（4,008 字符）标在省略说明里
    # 首尾都在：尾部常有测试失败/报错结论，只留头部等于丢掉最有用的部分
    assert result.count("甲") > 100
    assert "尾部关键结论" in result


def test_confirm_pattern_matching():
    """敏感操作匹配：安装/提交/删除/移动命中；普通命令不命中。"""
    assert _confirm_reason("git commit -m x")
    assert _confirm_reason("pip install requests")
    assert _confirm_reason("uv add fastapi")
    assert _confirm_reason("rm old.txt")
    assert _confirm_reason("del old.txt")
    assert _confirm_reason("conda install numpy")
    assert _confirm_reason("echo hello") is None
    assert _confirm_reason("git status") is None
    assert _confirm_reason("python main.py") is None


def test_run_command_confirm_rejected_by_user(monkeypatch, tmp_path):
    """交互确认：用户拒绝 → 不执行，返回拒绝提示。"""
    import builtins
    import sys as _sys
    from types import SimpleNamespace as _NS

    from wovra import tools as tools_module

    monkeypatch.setattr(tools_module.safety, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(_sys, "stdin", _NS(isatty=lambda: True))
    monkeypatch.setattr(builtins, "input", lambda prompt: "n")

    result = run_command("git commit -m 'x'")
    assert "用户拒绝" in result


def test_run_command_confirm_allowed_by_user(monkeypatch, tmp_path):
    """用户允许 → 命令实际执行（tmp 工作区里 git 无仓库而失败，证明已执行）。"""
    import builtins
    import sys as _sys
    from types import SimpleNamespace as _NS

    from wovra import tools as tools_module

    monkeypatch.setattr(tools_module.safety, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(_sys, "stdin", _NS(isatty=lambda: True))
    monkeypatch.setattr(builtins, "input", lambda prompt: "y")

    result = run_command("git commit -m 'x'")
    assert "命令执行失败" in result  # 已执行（tmp 目录无 git 仓库，git 报错）


def test_confirm_ctrl_c_interrupts_instead_of_refusing(monkeypatch):
    """确认提示处按 Ctrl+C = 打断本轮（向上传播），而不是吞成'拒绝'。"""
    import builtins
    import sys as _sys
    from types import SimpleNamespace as _NS

    import pytest

    from wovra import tools as tools_module

    monkeypatch.setattr(_sys, "stdin", _NS(isatty=lambda: True))

    def fake_input(prompt):
        raise KeyboardInterrupt

    monkeypatch.setattr(builtins, "input", fake_input)
    with pytest.raises(KeyboardInterrupt):
        _ask_yes_no("确认？")


def test_run_command_child_stdin_is_devnull(monkeypatch):
    """子进程 stdin 必须接 DEVNULL（worklog-20260911.md §5-P2）。

    子进程若继承终端 stdin（isatty=True），命令内部再触发工具层确认门
    就会阻塞在用户**看不见**的提示上（输出被重定向到临时文件），等待
    期被读走的按键还可能变成"盲授权"界外访问。
    """
    import subprocess as _subprocess

    from wovra import tools as tools_module

    captured = {}
    real_popen = _subprocess.Popen

    def spy(*args, **kwargs):
        captured.update(kwargs)
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(tools_module.shell.subprocess, "Popen", spy)
    result = run_command("echo stdin-probe")
    assert "stdin-probe" in result
    assert captured.get("stdin") is _subprocess.DEVNULL
