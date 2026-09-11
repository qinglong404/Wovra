"""工具层测试（自 test_tools.py 拆分，2026-09-11）。

本模块：test_background。"""

import os as _os
from wovra.tools import (
    check_background,
    list_background,
    run_background,
    stop_background,
)

# 共享夹具/工具（_helpers.py 是原文件的公共头部）
from ._helpers import *  # noqa: F401,F403

def test_background_lifecycle():
    """后台任务：启动即返回 → 增量输出可见 → 停止/未知 ID 报错。"""
    import re as _re
    import time as _time

    started = run_background("echo bg-marker-424242")
    assert "已启动" in started
    task_id = _re.search(r"bg-\d+", started).group(0)

    out = ""
    for _ in range(30):  # 子进程写日志需要一点时间，轮询等待
        out = check_background(task_id)
        if "bg-marker-424242" in out:
            break
        _time.sleep(0.1)
    assert "bg-marker-424242" in out

    stop = stop_background(task_id)
    assert "已停止" in stop
    # 幂等：对已退出的任务再次停止，仍返回停止状态而非报错
    assert "已停止" in stop_background(task_id)
    assert "未找到后台任务" in stop_background("bg-999999")
    assert "未找到后台任务" in check_background("bg-999999")


def test_background_rejects_dangerous_patterns():
    assert "已拒绝执行危险命令" in run_background("rm -r something")


def test_list_background_reports_empty_or_tasks():
    """list_background：无任务时报告为空；有任务时列出状态。"""
    import time

    from wovra import tools as tools_module

    tools_module.background._BACKGROUND_TASKS.clear()
    assert "当前没有后台任务" in list_background()
    started = run_background("echo bg-list-marker")
    task_id = _re_search_id(started)
    listing = ""
    for _ in range(30):  # 等子进程退出后再断言状态
        listing = list_background()
        if "已退出" in listing:
            break
        time.sleep(0.1)
    assert task_id in listing and "已退出" in listing
    tools_module.background._BACKGROUND_TASKS.clear()


def test_background_ownership_prevents_cross_session_management():
    """后台任务归属会话：跨会话查看/停止都被拒，并列出归属。"""
    from wovra import tools as tools_module

    tools_module.background._BACKGROUND_TASKS.clear()
    tools_module.set_current_session("session-A")
    started = run_background("echo owned-by-A")
    task_id = _re_search_id(started)

    tools_module.set_current_session("session-B")
    assert f"由会话 session-A 启动" in check_background(task_id)
    assert f"由会话 session-A 启动" in stop_background(task_id)
    assert f"[session-A]" in list_background()

    # 回到启动会话 → 可以管理
    tools_module.set_current_session("session-A")
    assert "已退出" in check_background(task_id) or "运行中" in check_background(task_id)
    tools_module.background._BACKGROUND_TASKS.clear()
    tools_module.set_current_session(None)


def test_stop_session_backgrounds_kills_owned_but_spares_keep_alive(monkeypatch):
    """会话退出：本会话的后台任务全部关闭；keep_alive 常驻任务除外。"""
    import os as _os
    import time as _time

    from wovra import tools as tools_module

    tools_module.set_current_session("session-A")
    tools_module.background._BACKGROUND_TASKS.clear()
    sleeper = "ping -n 30 127.0.0.1" if _os.name == "nt" else "sleep 30"
    normal = run_background(sleeper)
    resident = run_background(sleeper, keep_alive=True)
    id_normal = _re_search_id(normal)
    id_resident = _re_search_id(resident)
    _time.sleep(0.3)  # 等子进程起来

    stopped = tools_module.stop_session_backgrounds()

    assert stopped == 1
    # 普通任务：已停止并从注册表移除；常驻任务：继续运行
    assert "未找到后台任务" in check_background(id_normal)
    assert "运行中" in check_background(id_resident)
    # 清理测试残留
    tools_module.set_current_session("session-A")
    stop_background(id_resident)
    tools_module.background._BACKGROUND_TASKS.clear()
