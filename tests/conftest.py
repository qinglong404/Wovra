"""pytest 共享隔离：任何测试默认都把会话数据写进临时目录。

仓库里的 tasks/ 是用户的真实对话记录——此前哪个测试忘了 monkeypatch
TASKS_ROOT，哪次 pytest 就往里面漏一个真实会话（实测发生过：每跑一轮
测试多两个碎片目录）。这里用 autouse 夹具统一重定向，测试**不可能**
再忘记；个别需要自定义路径的测试仍可自行 monkeypatch（后打的补丁
优先生效）。
"""

import pytest

from wovra import task as task_module


@pytest.fixture(autouse=True)
def _isolate_tasks_root(tmp_path, monkeypatch):
    """把 TASKS_ROOT 重定向到本测试专属的临时目录（函数级，用完即焚）。"""
    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path / "tasks")
