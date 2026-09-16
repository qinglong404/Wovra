"""系统提示词组装与 Agent 构造：模式化提示词、AGENTS.md 工作区指令、
工具清单装配（_build_agent 是组合根）。
"""

from pathlib import Path

from ..agent import Agent, MODE_MANAGED
from ..task import Task
from ..tools import safety as safety_module
from ..tools import (
    ask_user,
    check_background,
    delete_file,
    edit_file,
    get_current_time,
    glob_files,
    list_background,
    list_files,
    move_file,
    page_text,
    read_file,
    replace_lines,
    restore_file,
    run_background,
    run_command,
    search_files,
    screenshot,
    stop_background,
    web_automate,
    web_fetch,
    view_image,
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
        f"你是 Wovra 的执行助手。工作区：{safety_module.workspace_root()}——"
        "所有文件工具与"
        "命令都限定并执行于此目录内。会话持久化在磁盘上：每轮结束自动"
        "保存，用户可随时退出、下次接着做。需要时使用工具获取真实信息或"
        "完成任务：找内容用 search_files，按文件名找文件用 glob_files，"
        "读大文件用 read_file 的 start_line 分段；可以创建、修改项目内的"
        "文件，运行安全的 shell 命令。查技术资料用 web_search，抓取已知"
        "网页用 web_fetch；看界面先用 page_text 读**渲染后的文字**（按钮名、"
        "表格数据、JS 渲染内容都在这里，便宜又准），只有配色/重叠/对齐这类"
        "文字量不出来的事才用 screenshot + view_image（视觉是多模态模型的"
        "加分项，没有它也能干活）；需求或细节有分歧时用 ask_user 向用户确认。"
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
        # 「问 / 思想对齐 / 行动」三分（2026-09-13 用户口径，原话："那将用户需要
        # 分成三类：1 问…2 思想对齐…3 行动…收敛范围必须开始定好，后面无论是多少条，
        # 都做完，那怕是一百条，也做完再汇报"）。
        # 为什么要写进提示词：这是**语义判断**，机制管不了。实测会话
        # 20260913-151842-2628dd 就是判错的现场——R4「如果让你写…你准备如何写？」
        # 直接开工、R7「可以实现吗？」跑了 42 条命令 25 次改文件、R8 已明说
        # 「先不提交，给我讲讲」仍跑了 7 条命令 1 次改文件。
        "先分清用户这句话属于哪一类——**问 / 思想对齐 / 行动**，判错了就是白干"
        "或者该干的没干：\n"
        "1. **问**：用户在问（是什么、为什么、怎么做、能不能、现在什么情况、"
        "给我讲讲）。这时**只回答**——可以读文件、搜索、跑只读命令把答案弄准，"
        "但**不改文件、不提交、不安装、不删除**；也不要「顺手把它做了」，"
        "用户有权只想先知道答案。\n"
        "2. **思想对齐**：用户把自己的想法/方案说出来（我打算…、我想这样设计…、"
        "你觉得行不行）。这时**只分析**——可行性、风险、缺什么、有没有更好的"
        "做法，给出你的判断和建议；**不要动手实现**，也不要把他的想法直接当成"
        "任务派给自己。\n"
        "3. **行动**：用户明确让你去做（做、改、实现、修、加上、提交、跑一遍、"
        "继续做完）。这时①**先把细节问清楚、把范围收敛定死**（做什么、做到哪、"
        "验收标准是什么）；②范围定好之后**一次做完**——后面无论拆出多少条"
        "（哪怕一百条）都做完再汇报，不要做一半回来问、不要中途自作主张缩小范围；"
        "除非用户主动说「先到这、只做 X」才收窄。\n"
        "拿不准属于哪一类，用 ask_user 问一句；**默认按最保守的那一类办**"
        "（能只读就别改）。用户说了「先别动/先不提交/只是问问」一律只读只答。"
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
                        # 「与上一条的关联程度」优先（2026-09-13 用户口径，原话："给主agent的
            # 提示词中，添加考虑与上一次用户询问关联程度，特别一些接近于续说的，
            # 优先考虑路由给上一次路由的选择。这个是看情况而定的…还是得交给 LLM 来"）。
            # 实测现场（会话 20260913-151842-2628dd）：R7 送给 A 之后，R8 只是顺着
            # 追问"外面有什么现成的"，主 agent 按职责表字面改判给了 B——同一话题
            # 换了个人答，接手方还把 A 已测过的通道重跑了一遍。
            "**路由先看「与上一条的关联程度」**（常常比抠职责表更管用）：用户"
            "在**续说**——顺着上一句追问、补充条件、要细节、接着刚才那个话题"
            "往下讲——就**优先沿用上一轮那个域**；同一摊活连着干是常态，用户"
            "视角里那是同一个「你」，换人会让接手方把已知事实再查一遍。换了"
            "话题或换了对象（点名别人的活、提到别的域的文件、又开了一摊）才"
            "重新判。**这是你按上下文做的判断，不是硬规则**——拿不准就以关联"
            "程度为准，但也别把明显的新活硬留在老地方。"
            "**路由只对第 3 类（行动）做**：用户只是在问、只是在阐述想法时，"
            "就地回答（需要别的域的判断用 consult 去问，别把整条消息转出去）。"
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
    doc = safety_module.workspace_root() / "AGENTS.md"
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

    **工作区绑定点**（2026-09-15）：这里是会话与文件世界的唯一绑定点——
    谁要带着工具干活（CLI 的一轮、serve 的一轮、serve 的预览/命令），
    谁在自己的线程里经此绑上该会话的工作区。绑定放在提示词/AGENTS.md
    读取之前，否则系统提示词里的"工作区：…"会和工具实际解析的目录不一致。
    """
    workspace = str(getattr(task, "workspace", "") or "").strip()
    if workspace and Path(workspace).is_dir():
        safety_module.bind_workspace(workspace)
    return Agent(
        system_prompt=_system_prompt(mode),
        tools=[
            get_current_time,
            list_files,
            glob_files,
            read_file,
            search_files,
            # 眼睛（2026-09-13）：放这里而不是紧挨 web 那一带——提交边界上要与
            # 并行会话的改动拉开行距，git 才会把它算成独立 hunk，从而能只提交
            # 自己这一份（`scripts/git_stage_hunks.py`）。
            # page_text（2026-09-15）：读页面**文字**的无头路径，不依赖视觉通道
            # ——"多模态是加分项，没有它也能干活"（用户口径）。
            page_text,
            screenshot,
            view_image,
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
            # 云浏览器自动化（2026-09-16）：页面必须真交互才拿得到东西时用它
            # （登录后翻页、点筛选、SPA 渲染、填表）。按步计费，故不做自动降级。
            web_automate,
            ask_user,
        ],
        task=task,
        context_mode=mode,
        async_organization=async_organization,
    )
