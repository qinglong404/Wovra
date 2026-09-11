"""Agent 运行时测试（自 test_agent.py 拆分，2026-09-11）。

本模块：test_structure。"""

from wovra.agent import Agent

# 共享夹具/工具（_helpers.py 是原文件的公共头部）
from ._helpers import *  # noqa: F401,F403

def test_agent_mixins_have_no_method_name_collisions():
    """四个 mixin 的方法名互不重叠——防静默遮蔽。

    Agent 由 _CoreMixin / _LedgerMixin / _AssemblyMixin / _MaintenanceMixin
    组装（agent/ 包）。若两个 mixin 定义了同名方法，MRO 会静默取前者，
    后者的实现成为死代码且无任何报错——这条测试把它变成显式失败。
    """
    from wovra.agent.core import _CoreMixin
    from wovra.agent.ledger import _LedgerMixin
    from wovra.agent.assembly import _AssemblyMixin
    from wovra.agent.maintenance import _MaintenanceMixin

    seen: dict = {}
    for mixin in (_CoreMixin, _LedgerMixin, _AssemblyMixin, _MaintenanceMixin):
        for name, value in vars(mixin).items():
            if name.startswith("__") or not callable(value):
                continue
            assert name not in seen, (
                f"方法名冲突: {name!r} 同时定义于 "
                f"{seen[name].__name__} 与 {mixin.__name__}——MRO 会静默遮蔽"
            )
            seen[name] = mixin
    # 组装后的 Agent 确实覆盖了全部方法（不是只挂了个空壳）
    from wovra.agent import Agent
    for name in seen:
        assert getattr(Agent, name) is getattr(seen[name], name)
