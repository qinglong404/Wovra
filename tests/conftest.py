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
