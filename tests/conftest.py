"""pytest 共享隔离：任何测试默认都把会话数据写进临时目录。

仓库里的 tasks/ 是用户的真实对话记录——此前哪个测试忘了 monkeypatch
TASKS_ROOT，哪次 pytest 就往里面漏一个真实会话（实测发生过：每跑一轮
测试多两个碎片目录）。这里用 autouse 夹具统一重定向，测试**不可能**
再忘记；个别需要自定义路径的测试仍可自行 monkeypatch（后打的补丁
优先生效）。
"""

import pytest

from wovra import task as task_module
from wovra.tools import safety as safety_module


@pytest.fixture(autouse=True)
def _isolate_tasks_root(tmp_path, monkeypatch):
    """把 TASKS_ROOT 重定向到本测试专属的临时目录（函数级，用完即焚）。"""
    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path / "tasks")


@pytest.fixture(autouse=True)
def _legacy_paths_by_default(monkeypatch):
    """测试默认跑**旧链路**（`WOVRA_V4=0`）。

    V4（取消重组 ＋ 整理停用 ＋ 分裂按活性文件）会同时改装配、整理与分裂三条路，
    与本套里大量"按旧链路脚本化"的断言互相干扰。V4 行为由
    `tests/test_agent/test_v4_mode.py` 显式打开并覆盖。
    """
    monkeypatch.setenv("WOVRA_V4", "0")


@pytest.fixture(autouse=True)
def _round_note_off_by_default(monkeypatch):
    """默认关掉"每轮一段话"的同步结算。

    它在**每次轮闭合**多打一次 LLM，而本套测试按老链路脚本化响应（多一次调用会
    吃掉下一个脚本项、并改变成本/步数读数）。该功能由 `tests/test_agent/test_round_note.py`
    显式开启并覆盖；生产默认开（`WOVRA_ROUND_NOTE`）。
    """
    monkeypatch.setenv("WOVRA_ROUND_NOTE", "0")


@pytest.fixture(autouse=True)
def _isolate_workspace_binding():
    """每测试清掉**线程绑定的工作区**（thread-local 会在同线程里残留）。

    工作区现在是线程绑定（`safety.bind_workspace`，见 safety.py 模块头）：
    只要有一个测试绑过（`_build_agent` 会绑），后面的测试就会读到那份残留而
    不是自己刚 monkeypatch 的 `safety.PROJECT_ROOT`——测试间互相串味。
    这里前后各清一次，绑定只在**测试自己内部**有效。
    """
    safety_module.unbind_workspace()
    yield
    safety_module.unbind_workspace()
