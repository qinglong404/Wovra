"""Wovra 内置工具箱：文件读写、命令执行、审计与防护。

设计原则——"审计换权限"：

    * 能力上，agent 从"只读研究员"升级为能产出、能执行的执行者；
    * 代价上，每个变更类工具的完整内容都会被 Agent 记入任务历史
      （谁、何时、对哪个文件、做了什么、内容是什么），可追溯；
    * 破坏性操作不做"确认弹窗"（CLI 场景做不到良好交互），
      而是直接硬拒绝——宁可让模型换一种做法，也不赌运气。

所有路径类工具都通过统一的路径安全层限制在项目根目录内，
防止模型读写项目之外的任何东西。安全层两个入口，各工具按语义选用：

    * `_safe_path_lexical`  —— 词法拒绝 `..`，不跟随末段链接
                              （需要操作链接本身的工具用：delete/move）
    * `_safe_write_path` / `_safe_directory` —— 上述 + 拒绝指向界外的链接

遍历通道（search_files / glob_files）额外走 `_walk` 做**逐项**校验：
起点校验不等于遍历逐项校验，rglob 会跟随符号链接穿出工作区——这条
是 agent-test/tool-layer-audit-20260909.md 问题 #1 的教训。
"""


# 包结构（2026-09-11 重构，纯搬迁）：
#
#     safety.py       工作区属主 / 审计 / 路径防护 / 命令越界判定 / 确认门
#     files.py        文件读写、搜索、检查点（restore/delete/move）
#     shell.py        run_command、进程树强杀
#     background.py   后台任务注册表与管理
#     web.py          web_search / web_fetch
#     interaction.py  ask_user / 用户 Hooks / 当前时间
#
# 注意：`PROJECT_ROOT` 的**属主是 safety.py**；本包这一份是 import 时的
# 值快照（cli.py 依赖这份快照语义）。要重定向工作区请改
# `wovra.tools.safety.PROJECT_ROOT`（task.py / 测试 / 探针均如此）。

# 子模块（可直接访问）
from . import safety, files, shell, background, web, interaction  # noqa: F401,E402

# ---- 公开 API 再导出 ---------------------------------------------------------
# cli.py / agent.py 的工具注册清单、实验探针的 getattr(tools_module, name)
# 都依赖这层形态：名字与重写前完全一致。
from .safety import (  # noqa: E402
    FAILURE_MARKERS,
    PROJECT_ROOT,
    set_audit_recorder,
    user_input_pending,
)
from .files import (  # noqa: E402
    delete_file,
    edit_file,
    glob_files,
    list_files,
    move_file,
    read_file,
    replace_lines,
    restore_file,
    search_files,
    write_file,
)
from .shell import run_command  # noqa: E402
from .background import (  # noqa: E402
    check_background,
    list_background,
    run_background,
    set_current_session,
    stop_background,
    stop_session_backgrounds,
)
from .web import web_fetch, web_search  # noqa: E402
from .interaction import (  # noqa: E402
    ask_user,
    get_current_time,
    run_post_hook,
    run_pre_hook,
)
