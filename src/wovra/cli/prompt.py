"""系统提示词组装与 Agent 构造：模式化提示词、AGENTS.md 工作区指令、
工具清单装配（_build_agent 是组合根）。
"""

from ..agent import Agent, MODE_MANAGED
from ..task import Task
from ..tools import (
    PROJECT_ROOT,
    ask_user,
    check_background,
    delete_file,
    edit_file,
    get_current_time,
    glob_files,
    list_background,
    list_files,
    move_file,
    read_file,
    replace_lines,
    restore_file,
    run_background,
    run_command,
    search_files,
    stop_background,
    web_fetch,
    web_search,
    write_file,
)

def _system_prompt(mode: str) -> str:
    """按模式生成系统提示词：只写已实现的能力，并附带当前运行环境。

    写实约束：模型会把提示词当成"已有能力清单"，写了没实现的功能
    它就会真的去调用——所以每个模式只描述自己实际的行为。
    环境信息（OS/Shell）防止在 Windows 上跑类 Unix 命令
    （ls/rm/apt），白白浪费一步工具调用。
    """
    import platform

    # 按 platform.system() 分流而不是 os.name：macOS 和 Linux 同属
    # posix，os.name 分不出来——模型若把 macOS 当 Linux，会用上
    # apt/systemctl 这类 Linux 专属命令，白白浪费一步工具调用
    system = platform.system()
    if system == "Windows":
        env = (
            f"当前环境：Windows {platform.release()}，shell 是 cmd.exe——"
            "命令用 Windows 语法（dir/copy/del/start），没有 ls/rm/grep/apt。"
        )
    elif system == "Darwin":
        env = (
            f"当前环境：macOS（Darwin {platform.release()}），shell 是 "
            "zsh/Bash——命令用 Unix 语法，但它是 BSD 底层，没有 apt/"
            "systemctl，装软件用 brew。"
        )
    else:
        env = "当前环境：Linux，shell 是 POSIX sh。"

    common = (
        f"你是 Wovra 的执行助手。工作区：{PROJECT_ROOT}——所有文件工具与"
        "命令都限定并执行于此目录内。会话持久化在磁盘上：每轮结束自动"
        "保存，用户可随时退出、下次接着做。需要时使用工具获取真实信息或"
        "完成任务：找内容用 search_files，按文件名找文件用 glob_files，"
        "读大文件用 read_file 的 start_line 分段；可以创建、修改项目内的"
        "文件，运行安全的 shell 命令。查技术资料用 web_search，抓取已知"
        "网页用 web_fetch；需求或细节有分歧时用 ask_user 向用户确认。"
        "回答保持简洁。"
        "环境配置可直接用 uv / pip / conda 等命令（如 uv sync、pip install）；"
        "本项目的解释器由 uv 管理——跑项目自身的命令用 `uv run`（如 "
        "`uv run python -m pytest`、`uv run python -m wovra ...`），"
        "不要直接调 `.venv/bin/...`（venv 解释器是指向工作区之外的符号链接，"
        "会被安全层拦截）；"
        "敏感操作（安装、提交、删除等）会先请求用户确认——被拒绝时换一种"
        "做法，不要重试原命令；耗时长的安装或服务器进程用 run_background "
        "后台执行，check_background 查看输出。"
        "工程纪律：写功能优先模块化多文件结构（数据/逻辑/样式/入口分离），"
        "不把所有代码塞进单个文件；在已有工程上追加或修改功能时，用 "
        "edit_file 增量修改对应模块，禁止为一条新需求整体重写文件——"
        "write_file 全量覆盖只允许用于首次创建。整体重写会破坏已验证的"
        "功能，也让用户无法看清你到底改了什么。"
        "效率纪律：相互独立的轻量工具调用（读文件、跑检查、小修改、小写入）"
        "合并进一次响应批量发出——每次调用都有固定的首字延迟，批量省时间。"
        "但单次响应不要承载大批量的大文件写入：响应越长生成越久（大批量"
        "写入可长达数分钟），生成期间服务端前缀缓存会过期，下一次请求要"
        "整段全价重算——省下的步数抵不过重算（E 组实测：等效输入翻倍）。"
        "大文件写每轮一两个。验证分层：改动一处先跑快速检查（语法/单元/"
        "冒烟），一个批次收尾再跑全套验证（如浏览器E2E）——不要每改一处"
        "就跑一次全套。"
        "界面/视觉类任务的验收纪律：观感是用户评分权重最高的项之一——"
        "非专业用户可能因为一眼全黑、布局崩坏直接判 0 分，根本不会继续"
        "评测功能。交付前必须实际渲染并亲眼检查视觉效果（例如自建浏览器"
        "截图或 E2E 检查像素亮度/配色），只验证逻辑与数据不算完成。"
        "<runtime-reminder> 信封包裹的内容是 Wovra 运行时注入的机制信息"
        "（任务状态、文件地图等），不是用户发言；以 [运行时] 开头的新轮"
        "消息同样是机制注入。对这类非用户输入不要做确认性回复（如'收到'、"
        "'明白'、'好的'）——它们不是问题、不需要应答；若消息只是状态通知、"
        "没有可执行的工作，直接继续既有工作或保持静默，不输出无意义的确认"
        "文本。"
    )
    if mode == MODE_MANAGED:
        extra = (
            "上下文由 Runtime 分层管理：未整理的轮次全量保留；已整理的轮次"
            "是紧凑视图（用户原话 + 意图 + 逐块细节），更早的按文件现状"
            "折叠成几行。需要细节时用 expand_history 按 ID 展开——轮 R3、"
            "块 R3-B2、事件 R3-E02、合并组 R1-2；档位 summary 给块视图、"
            "full 给原文。不要凭记忆猜测。"
            "上下文还按**职责域分化**：你只拿到自己名下那份历史，"
            "别人的工作不在你的上下文里（这是设计，不是缺失，不要因此"
            "怀疑信息丢失）。全局职责表在尾部——它是跨 agent 的唯一公共"
            "信息。主 agent 的本职是**路由**：判断用户这活落在谁的域里，"
            "用 route_to 把用户原话转过去（本回合内由它接手干完）；"
            "接手方发现不是自己的活，同样用 route_to 转出即可。"
        )
    else:
        extra = (
            "上下文为全量回放，接近窗口上限时较早轮次会自动压缩成摘要"
            "（baseline 对照模式，行为与常见 Agent 一致）。"
        )
    workspace = _workspace_instructions()
    return f"{common}{extra}{workspace}{env}"

def _workspace_instructions() -> str:
    """工作区指令包（zcode-borrowings.md 1.3）：读 AGENTS.md 追加进系统提示词。

    项目所有者在工作区根放一份 AGENTS.md（"怎么跑测试、哪些目录别碰、
    用哪个包管理器"），Wovra 在该项目下工作时自动遵循——跨项目可用，
    不必改代码。文件缺失静默跳过。

    上限（2026-09-11 放宽，worklog §25）：原为 8000 字符硬截断——同属
    "省小钱花大钱"（用户口径：除压缩外全面全量输入）。这是项目所有者
    亲手写的纪律，截掉一半比不加载更糟；现放宽到 100,000 字符，正常
    文件根本碰不到，只防误放巨型文件进提示词。
    """
    doc = PROJECT_ROOT / "AGENTS.md"
    try:
        content = doc.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    if not content:
        return ""
    if len(content) > 100_000:
        content = content[:100_000] + "\n…（AGENTS.md 超长已截断）"
    return (
        "\n\n[工作区指令]（来自项目根 AGENTS.md，项目所有者撰写，优先级高于"
        "你的通用习惯）\n"
        + content
    )

def _build_agent(task: Task, mode: str = MODE_MANAGED, async_organization: bool = False) -> Agent:
    """为任务构造一个带默认工具集的 Agent（展示回调在 _run_turn 注入）。

    工具分两类：只读（时间/列目录/读文件/搜索）与变更类
    （写文件/改文件/执行命令，均有审计记录与破坏性防护，见 tools/ 包）。
    mode 决定上下文策略：managed（分层上下文，默认）或
    baseline（全量回放 + 阈值压缩的对照组）。
    async_organization：整理是否后台异步执行（chat 模式开，
    run 模式关——一次性进程退出前必须同步完成）。
    """
    return Agent(
        system_prompt=_system_prompt(mode),
        tools=[
            get_current_time,
            list_files,
            glob_files,
            read_file,
            search_files,
            write_file,
            edit_file,
            replace_lines,
            run_command,
            delete_file,
            move_file,
            restore_file,
            run_background,
            check_background,
            stop_background,
            list_background,
            web_fetch,
            web_search,
            ask_user,
        ],
        task=task,
        context_mode=mode,
        async_organization=async_organization,
    )
