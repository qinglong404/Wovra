"""Wovra CLI（2026-09-11 重构，纯搬迁）。

    main.py         入口：argparse、子命令分发、run/report/list/delete
    session.py      会话锁、任务/模式解析、残留记账
    prompt.py       系统提示词与 Agent 构造（组合根）
    render.py       _run_turn（流式渲染 + 看门狗）、回放、状态行
    interactive.py  cmd_chat 主循环、本地命令

包 __init__ 再导出全部原名——`wovra.cli:main` 入口点与外部
（tests/scripts/experiments）对 cli 模块级名字的引用保持不变。
"""

from .main import (  # noqa: F401
    cmd_delete,
    cmd_list,
    cmd_report,
    cmd_run,
    main,
)
from .session import (  # noqa: F401
    _acquire_session_lock,
    _all_tasks,
    _child_summaries,
    _load_task,
    _process_alive,
    _record_leftover_maintenance,
    _release_session_lock,
    _resolve_mode,
    _resolve_task_id,
    _resume_command,
    _session_lock_path,
)
from .prompt import (  # noqa: F401
    PROJECT_ROOT,
    _build_agent,
    _system_prompt,
    _workspace_instructions,
)
from .render import (  # noqa: F401
    _drain_status,
    _replay_history,
    _run_turn,
    _split_call,
)
from .interactive import (  # noqa: F401
    _LOCAL_HELP,
    _chat_help,
    _flush_stdin,
    _local_command,
    _make_prompt_session,
    _read_input,
    _toolbar_text,
    cmd_chat,
)
