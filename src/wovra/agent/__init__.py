"""Agent 运行时（工具循环）+ V2 上下文生命周期管理。
"""

# 包结构（2026-09-11 重构，纯搬迁）：类由四个 mixin 组装，方法面
# （含私有方法）与拆分前逐字一致——scripts / tests 直接调用
# agent._organize_rounds() 等私有方法不受影响。
from .core import _CoreMixin
from .ledger import _LedgerMixin
from .assembly import _AssemblyMixin
from .maintenance import _MaintenanceMixin

# 兼容再导出：外部（cli/tests/scripts）仍从 wovra.agent 取这些名字
from .support import (  # noqa: F401
    MODE_BASELINE,
    MODE_MANAGED,
    _action_word,
    _clip_quote,
    _runtime_reminder,
    _sanitize_json_strings,
    _schema_of,
)
from .support import _JSON_TYPES  # noqa: F401
# 提示词/schema 常量：拆分前是 agent 模块级名字（测试与脚本按
# `from wovra.agent import _TODO_SCHEMA` 引用），保持原访问路径
from .prompts import (  # noqa: F401
    _CONSULT_SCHEMA,
    _LIST_AGENTS_SCHEMA,
    _NOTIFY_SCHEMA,
    _SWITCH_VIEW_SCHEMA,
    _ORG_DOMAINS_SCHEMA,
    _ORG_META_INFO,
    _ORG_SUBMIT_SCHEMA,
    _ORG_TAG_INSTRUCTIONS,
    _SPLIT_INSTRUCTIONS,
    _TODO_SCHEMA,
)


class Agent(
    _CoreMixin, _LedgerMixin, _AssemblyMixin, _MaintenanceMixin
):
    """Agent 运行时（工具循环）+ V2 上下文生命周期管理。
    """
