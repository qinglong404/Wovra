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

def _system_prompt(mode: str, stage: str = "post") -> str:
    """系统提示词。`stage="pre"` ＝ **还没分裂**（注册表里只有 Main）——按用户口径，
    分裂前不写任何关于分裂/职责划分/路由的话，省掉"我该不该裂、职责表怎么说"这类思考。

    按模式生成：只写已实现的能力，并附带当前运行环境。
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
        # 身份（2026-09-16，提示词审阅 §1）：原先写"你是 Wovra 的执行助手"，又另有两处
        # 把 Wovra/Runtime 说成"在你之外管着你的东西"，合起来被读成"我是跑在 Wovra
        # 平台上的一个 agent"——实测那次把"测 Wovra"理解成"去测那个平台"，是最大的
        # 方向性错误。口径：你就是 Wovra，工具层就是你自己的手。
        "## 身份\n"
        "你是 Wovra，在本会话里带着下面这套工具干活。\n\n"
        "## 工作区与环境\n"
        f"工作区：{safety_module.workspace_root()}——所有文件工具与命令都限定并"
        "执行于此目录内。会话持久化在磁盘上：每轮结束自动保存，用户可随时退出、"
        "下次接着做。\n"
        "本工作区的解释器由 uv 管理——跑命令用 `uv run`（如 `uv run python -m pytest`），"
        "不要直接调 `.venv/bin/...`（venv 解释器是指向工作区之外的符号链接，会被"
        "安全层拦截）。环境配置可直接用 uv / pip / conda 等命令（如 uv sync、"
        "pip install）。敏感操作（安装、提交、删除等）会先请求用户确认——被拒绝时"
        "换一种做法，不要重试原命令；耗时长的安装或服务器进程用 run_background "
        "后台执行，check_background 查看输出。\n\n"
        "## 工具使用\n"
        "需要真实信息就取，不要凭记忆猜：找内容用 search_files，按文件名找文件用 "
        "glob_files，读大文件用 read_file 的 start_line 分段；可以创建、修改工作区"
        "内的文件，运行安全的 shell 命令。查技术资料用 web_search，抓取已知网页用 "
        "web_fetch；页面必须真交互才拿得到内容时（登录后翻页、点筛选、SPA 渲染、"
        "填表）用 web_automate（按步计费，不自动降级）。看界面先用 page_text 读"
        "**渲染后的文字**（按钮名、表格数据、JS 渲染内容都在这里，便宜又准），只有"
        "配色/重叠/对齐这类文字量不出来的事才用 screenshot + view_image（视觉是"
        "加分项，没有它也能干活）。需求或细节有分歧时用 ask_user 向用户确认。"
        "回答保持简洁。\n\n"
        # 「问 / 思想对齐 / 行动」三分（2026-09-13 用户口径，原话："那将用户需要
        # 分成三类：1 问…2 思想对齐…3 行动…收敛范围必须开始定好，后面无论是多少条，
        # 都做完，那怕是一百条，也做完再汇报"）。
        # 为什么要写进提示词：这是**语义判断**，机制管不了。实测会话
        # 20260913-151842-2628dd 就是判错的现场——R4「如果让你写…你准备如何写？」
        # 直接开工、R7「可以实现吗？」跑了 42 条命令 25 次改文件、R8 已明说
        # 「先不提交，给我讲讲」仍跑了 7 条命令 1 次改文件。
        "## 交互纪律（问 / 思想对齐 / 行动 / 停止）\n"
        "先分清这句话属于哪一类——**问 / 思想对齐 / 行动**，判错了就是白干或者"
        "该干的没干：\n"
        "1. **问**：在问（是什么、为什么、怎么做、能不能、现在什么情况、给我讲讲）。"
        "这时**只回答**——可以读文件、搜索、跑只读命令把答案弄准，但**不改文件、"
        "不提交、不安装、不删除**；也不要「顺手把它做了」，用户有权只想先知道答案。\n"
        "2. **思想对齐**：用户把自己的想法/方案说出来（我打算…、我想这样设计…、"
        "你觉得行不行）。这时**只分析**——可行性、风险、缺什么、有没有更好的做法，"
        "给出你的判断和建议；**不要动手实现**，也不要把那个想法直接当成任务派给自己。\n"
        "3. **行动**：明确让你去做（做、改、实现、修、加上、提交、跑一遍、继续做完）。"
        "这时①**先把细节问清楚、把范围收敛定死**（做什么、做到哪、验收标准是什么）；"
        "②范围定好之后**一次做完**——后面无论拆出多少条（哪怕一百条）都做完再汇报，"
        "不要做一半回来问、不要中途自作主张缩小范围；除非明说「先到这、只做 X」"
        "才收窄。\n"
        "③**不得擅自扩大范围**：只做明确要求的那件事；发现顺带还能做别的，"
        "列出来问一句，不要直接做。\n"
        "④**同一需求若有「就地直接做」与「搭一套基础设施批量做」两条路，先走便宜的"
        "那条，或先问一句**——两条路的成本常常差几个数量级。\n"
        "**停止信号是终局指令**：说「可以了 / 就停止 / 先到这 / 不用了」时立即停手——"
        "不新起任何执行、不追加验证、不顺手再跑一遍；已启动的后台任务要停掉并报告。"
        "中止或收窄之后，只汇报已知结论，等下一句指令。\n"
        "拿不准属于哪一类，用 ask_user 问一句；**默认按最保守的那一类办**（能只读就别改）。"
        "听到「先别动 / 先不提交 / 只是问问」一律只读只答。\n\n"
        "## 效率纪律\n"
        "相互独立的轻量工具调用（读文件、跑检查、小修改、小写入）合并进一次响应批量"
        "发出——每次调用都有固定的首字延迟，批量省时间。但单次响应不要承载大批量的大"
        "文件写入：响应越长生成越久（大批量写入可长达数分钟），生成期间服务端前缀缓存"
        "会过期，下一次请求要整段全价重算——省下的步数抵不过重算（E 组实测：等效输入"
        "翻倍）。大文件写每轮一两个。\n\n"
        "## 输出与验证纪律\n"
        "验证分层：改动一处先跑快速检查（语法/单元/冒烟），一个批次收尾再跑全套验证"
        "（如浏览器 E2E）——不要每改一处就跑一次全套。\n"
        "工程纪律：写功能优先模块化多文件结构（数据/逻辑/样式/入口分离），不把所有"
        "代码塞进单个文件；在已有工程上追加或修改功能时，用 edit_file 增量修改对应模块，"
        "禁止为一条新需求整体重写文件——write_file 全量覆盖只允许用于首次创建。整体"
        "重写会破坏已验证的功能，也让用户无法看清你到底改了什么。\n"
        "界面/视觉类任务的验收纪律：观感是用户评分权重最高的项之一——非专业用户可能"
        "因为一眼全黑、布局崩坏直接判 0 分，根本不会继续评测功能。交付前必须实际渲染"
        "并亲眼检查视觉效果（例如自建浏览器截图或 E2E 检查像素亮度/配色），只验证逻辑"
        "与数据不算完成。\n\n"
        "## 运行时通知\n"
        # 识别线索按**信封结构**而不是某个前缀（2026-09-16，提示词审阅 §3）：旧文写的
        # "以 [运行时] 开头"这个字面量在源码里出现 0 处，是条失效线索。
        "用 <runtime-reminder> 信封包裹、以 user-role 注入的内容，是运行时（你自己"
        "的一部分）给你的机制信息（任务状态、文件地图、路由建议、传话等），不是用户"
        "发言。对这类非用户输入：不确认（'收到/明白/好的'），**也不解释或点评这条通知"
        "本身**；若通知指出的是你自己上一步操作造成的异常（例如你生成的图片未能注入），"
        "直接修正即可（重截/换小图/改路径），把说明留在正常汇报里。若只是状态通知、"
        "没有可执行的工作，直接继续既有工作或保持静默。"
    )
    # **分裂前后提示词不一样**（2026-09-17 用户口径："第一次分裂前，不应该有关于分裂和职责划分
    # 的提示词……可以只写公共系统提示词，在有身份后，再追加，这样防止分裂前的主 agent 老拉职责表
    # 等额外思考"）。判据零成本：注册表里只有 Main ＝ 分裂前。
    split_done = bool(stage and str(stage) != "pre")
    if mode == MODE_MANAGED and not split_done:
        extra = (
            "\n\n## 上下文\n"
            "上下文由你自身的运行时分层管理：未整理的轮次全量保留；已整理的轮次已折成"
            "**一段话**，更早的也一样。需要细节时**不要凭记忆猜**，用 expand_history 三招取："
            "① `pattern=\"正则\"` 在历史原文里检索（回命中清单 + 总数，可 offset 续取）；"
            "② `ids=\"R3-E02\" around=\"关键字\"` 只看该处窗口（可 chars/offset 调）；"
            "③ `ids=\"R3\"` 给该轮的地图（summary 块视图 / truncated 事件索引）。"
            "范围可用 scope 收（`R3-R8` 或 `file:路径`），来源可用 source 收"
            "（result/assistant/call/user）。\n"
            "**用户追问上一轮的内容时，先用上一轮的结果答**（它就在你的上下文里）——"
            "只有确实需要最新状态时才去读文件；别把\u201c接着上一句问\u201d当成新活重新查一遍（脱节）。\n"
            "**这一摊活现在只有你一个 agent，直接干**——不用考虑分工、没有职责表要看。\n"
        )
    elif mode == MODE_MANAGED:
        extra = (
            "\n\n## 上下文（分层与职责域）\n"
            "上下文由你自身的运行时分层管理：未整理的轮次全量保留；已整理的轮次"
            "是紧凑视图（用户原话 + 意图 + 逐块细节），更早的按文件现状"
            "折叠成几行。需要细节时**不要凭记忆猜**，用 expand_history 三招取："
            "① `pattern=\"正则\"` 在历史原文里检索（回命中清单 + 总数，可 offset 续取）；"
            "② `ids=\"R3-E02\" around=\"关键字\"` 只看该处窗口（可 chars/offset 调）；"
            "③ `ids=\"R3\"` 给该轮的地图（summary 块视图 / truncated 事件索引）。"
            "范围可用 scope 收（`R3-R8` 或 `file:路径`），来源可用 source 收"
            "（result/assistant/call/user）。"
            "**用户追问上一轮的内容时，先用上一轮的结果答**（它就在你的上下文里）——"
            "只有确实需要最新状态时才去读文件；别把\u201c接着上一句问\u201d当成新活重新查一遍（脱节）。"
            "上下文还按**职责域分化**：你只拿到自己名下那份历史，"
            "别人的工作不在你的上下文里。全局职责表在尾部——它是跨 agent 的唯一公共"
            "信息。\n"
            "**你的本职之一是路由**：判断这活落在谁的域里，用 route_to 把用户原话"
            "转过去（本回合内由它接手干完）；接手方发现不是自己的活，同样用 route_to "
            "转出即可。"
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
            "重新判。拿不准就以关联程度为准，但别把明显的新活留在老地方。"
            "**路由只对第 3 类（行动）做**：只是在问、只是在阐述想法时，"
            "就地回答（需要别的域的判断用 consult 去问，别把整条消息转出去）。\n"
            # 编排类工具提名（2026-09-16，提示词审阅 §4）：这些由 Agent._register_*
            # 注册，schema 自带描述，但 schema 在长上下文里优先级低于系统提示词。
            "域内协作时还有几件编排工具：todo 更新计划账本（大步/小步），"
            "update_responsibility 改写自己的职责说明，notify 单向通知别的域，"
            "join_with 与别的域会合后一起作答。\n"
        )
    else:
        extra = (
            "\n\n## 上下文（全量回放）\n"
            "上下文为全量回放，接近窗口上限时较早轮次会自动压缩成摘要"
            "（baseline 对照模式，行为与常见 Agent 一致）。"
        )
    workspace = _workspace_instructions()
    # 运行环境单独成节：它落在工作区指令之后，加个小标题才不会被读成 AGENTS.md 的
    # 尾巴。rstrip 是给"工作区里没有 AGENTS.md"的情况收一个多余空行。
    body = f"{common}{extra}{workspace}".rstrip()
    return f"{body}\n\n## 运行环境\n{env}"

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

    **工作目录不可用就别想悄悄退回启动目录**（2026-09-18 用户报障："我工作路径下是空的，
    其为什么说当前工作路径是 wovra，且不需要我授权就去读取修改了"）。此前这里是
    `if workspace and Path(workspace).is_dir(): bind(...)`——**目录一旦不可用就静默跳过
    绑定**，`safety.workspace_root()` 于是退回**进程默认**（serve 的启动目录）。后果不是
    "读不到"，而是**读错地方**：相对路径 `docs/x.md` 被解析成 `<启动目录>/docs/x.md`，
    它**恰好在界内**——既读得到运行器自己的仓库，又因为"没越界"而**压根不问授权**
    （实测那条会话正是这样读到了本仓库的 `docs/*.md`）。

    处置分两层（只读端点也要能跑，所以不在这里一律抛）：
    * 目录**可用** → 正常绑；
    * 目录**不可用** → **不绑**（宁可让工具报"越界/不存在"，也不落到启动目录），
      并在审计里**响亮留痕**；真正要干活的入口（serve 起轮）会据此**拒绝开工**。
    """
    workspace = str(getattr(task, "workspace", "") or "").strip()
    if workspace:
        if Path(workspace).is_dir():
            safety_module.bind_workspace(workspace)
        else:
            safety_module.unbind_workspace()
            try:
                safety_module._audit(
                    "[工作区][拒绝兜底] 会话声明的工作目录不可用：" + workspace
                    + "——不绑到进程启动目录（否则会读到运行器自己的仓库且不问授权）"
                )
            except Exception:  # noqa: BLE001——留痕失败不影响构造
                pass
            task.__dict__["_workspace_bind_failed"] = workspace
    return Agent(
        system_prompt=_system_prompt(
            mode,
            "post" if any(
                str((e or {}).get("id") or "") not in ("", "Main")
                for e in (getattr(task, "registry", None) or [])
                if isinstance(e, dict)
            ) else "pre",
        ),
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
