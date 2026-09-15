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
# 注意：`PROJECT_ROOT` 的**属主是 safety.py**，且它只是**进程默认**工作区
# （启动目录 / `WOVRA_WORKSPACE`）；本包这一份还是 import 时的值快照。
# 工具层现在一律读 `safety.workspace_root()`（线程绑定优先、进程默认兜底）；
# 要重定向整个进程请改 `wovra.tools.safety.PROJECT_ROOT`（测试 / 探针均如此），
# 要给某个会话绑文件世界请调 `safety.bind_workspace()`（见 cli/prompt.py）。

#
#     eyes.py         眼睛：screenshot（无头浏览器截图）+ view_image（多模态看图）
# 上面这行刻意与其它子模块分开写：提交边界上要与并行会话的改动拉开行距，
# git 才会把它算成独立 hunk（`scripts/git_stage_hunks.py`）。
# 子模块（可直接访问）
from . import limits
from . import permissions  # noqa: E402——文件权限守卫（工具层强制）
from . import safety, files, shell, background, web, interaction  # noqa: F401,E402
from . import status  # noqa: E402,F401——工具结果成败判定的唯一权威口径

# ---- 公开 API 再导出 ---------------------------------------------------------
# cli/prompt.py 与 agent/core.py 的工具注册清单、实验探针的
# getattr(tools_module, name) 都依赖这层形态：名字与重写前完全一致。
from .safety import (  # noqa: E402
    PROJECT_ROOT,
    set_audit_recorder,
    user_input_pending,
)
# 成败判定（2026-09-14 起唯一权威）：`classify` / `failed` / `denied` / `label`。
# 旧的 `FAILURE_MARKERS`（全文子串）已删除——正文里出现"工具执行出错"等字样
# 就判失败，实测 42 条假阳性。
from .status import (  # noqa: E402
    DENY,
    ERROR,
    LABELS,
    OK,
    classify as result_status,
    denied as result_denied,
    failed as result_failed,
    label as result_label,
)
from .permissions import set_file_guard  # noqa: E402
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

# ---- 眼睛（2026-09-13）------------------------------------------------------
# 单独放在文件末尾（而不是紧挨 web 检索那两行）：提交边界上要与并行会话的
# 改动拉开距离——git 会把相邻改动并成同一个 hunk，那样就没法只提交自己那份
# （见 `scripts/git_stage_hunks.py`）。
from .eyes import page_text, screenshot, view_image  # noqa: E402
