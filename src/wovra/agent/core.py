"""Agent 核心：构造/注册、Round 生命周期、run 循环、工具分发、用量记账。

工具方法（todo/notify/consult/submit 守卫）在 ledger.py；上下文装配在
assembly.py；水位维护管线在 maintenance.py——四者由 __init__.py 组装为
同一个 Agent 类（mixin）。方法体里的跨模块调用走 self，与拆分前一致。
"""
import copy
import inspect
import json
from pathlib import Path
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Optional
from .. import blocks as blocks_module
from .. import observed as observed_module
from .. import registry as registry_module
from .. import tools as tools_module
from .. import tokens as tokens
from .. import truncate as truncate
from ..llm import LLM, LLMStreamError, cached_tokens_of, reasoning_of
from ..llm import APIError as _llm_APIError
from ..task import Task, sanitize_surrogates
from .. import views as views_module
from .support import (
    MODE_BASELINE,
    MODE_MANAGED,
    _DEFAULT_CONTEXT_LIMIT,
    _DEFAULT_MAX_TURNS,
    _IMAGE_VIEW_HARD,
    _IMAGE_VIEW_SOFT,
    _MAINTENANCE_PURPOSES,
    _ORG_COOLDOWN_ROUNDS_DEFAULT,
    _ORG_GRACE_ROUNDS_DEFAULT,
    _NOTE_BATCH_MAX_DEFAULT,
    _NOTE_TIMEOUT_DEFAULT,
    _FOLD_KEEP_ROUNDS_DEFAULT,
    _FOLD_TARGET_DEFAULT,
    v4_enabled,
    _ORG_MAINT_TIMEOUT_DEFAULT,
    _ORG_WATERMARK_DEFAULT,
    _READ_ONLY_TOOLS,
    _VISION_TOOLS,
    _action_word,
    _runtime_reminder,
    _sanitize_json_strings,
    _schema_of,
    image_budget_refusal,
    image_converge_note,
)
from .prompts import (
    _CONSULT_SCHEMA,
    _JOIN_WITH_SCHEMA,
    _LIST_AGENTS_SCHEMA,
    _NOTIFY_SCHEMA,
    _ORG_DOMAINS_SCHEMA,
    _ORG_SUBMIT_SCHEMA,
    _RESPONSIBILITY_SCHEMA,
    _ROUND_NOTE_SCHEMA,
    _ROUTE_TO_SCHEMA,
    _TODO_SCHEMA,
)


_THROTTLE_MARKERS = (
    "system protection", "rate limit", "too many requests", "slow down",
    "requests burst", "429", "tpm limit", "rpm limit",
)


def _is_throttle_error(error: BaseException) -> bool:
    """端点限流/突发保护类错误（**可安全退避重试**：连接期失败无副作用）。

    实测文案（火山/方舟）："System protection triggered by request burst.
    Please slow down traffic growth and increase requests gradually before
    retrying."——2026-09-09 一批整理整批死在这上面（没有任何重试）。
    """
    text = str(error).lower()
    return any(m in text for m in _THROTTLE_MARKERS)


def _signature_hint(fn, parsed: dict, error: TypeError) -> str:
    """TypeError 时补一句"正确用法"（TOOLING_REVIEW.md §4.4）。

    参数名不统一（文件类是 `path`，遍历类是 `directory`/`pattern`）是现实，
    但错误信息不该只说"unexpected keyword argument"就完事——把签名与最接近
    的正确参数名一并给出来，省掉一轮"翻文档/试参数"。同类名（`path` vs
    `file`/`dir`）给指路；实在没得猜就只列签名。
    """
    try:
        signature = inspect.signature(fn)
    except (TypeError, ValueError):
        return ""
    accepted = [name for name in signature.parameters
                if signature.parameters[name].kind in (
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                    inspect.Parameter.KEYWORD_ONLY)]
    bad = [key for key in parsed if key not in accepted]
    usage = f"{fn.__name__}({', '.join(accepted)})"
    hint = f"\n  正确参数名：{usage}"
    for key in bad:
        near = [name for name in accepted if _same_kind_of_arg(key, name)]
        if near:
            hint += f"\n  你给的 `{key}` 应该写成 `{near[0]}`。"
        else:
            hint += f"\n  没有 `{key}` 这个参数——请按上面的名单换一个。"
    return hint


def _same_kind_of_arg(given: str, accepted: str) -> bool:
    """两个参数名是否**强同类**（file/path、dir/directory、regex/pattern…）。

    只认两种强信号：一个名字完整包含另一个（`dir` ⊂ `directory`），或
    公共前缀 ≥4 个字符。**故意不认 3 个字符的前缀**——实测 `path` 与
    `pattern` 前三个字母都是 "pat"，据此建议"`path` 应写成 `pattern`"是
    错的（正确的那个是 `directory`），错误建议比不给建议更坏。
    """
    given, accepted = given.lower(), accepted.lower()
    if given == accepted:
        return True
    if len(given) >= 3 and (given in accepted or accepted in given):
        return True
    shared = 0
    for a, b in zip(given, accepted):
        if a != b:
            break
        shared += 1
    return shared >= 4


class _CoreMixin:
    def __init__(
        self,
        llm: Optional[LLM] = None,
        system_prompt: str = "",
        tools: tuple = (),
        max_turns: Optional[int] = None,
        task: Optional[Task] = None,
        context_mode: str = MODE_MANAGED,
        context_limit: Optional[int] = None,
        async_organization: bool = False,
        org_watermark: Optional[int] = None,
        org_grace_rounds: Optional[int] = None,
        org_cooldown_rounds: Optional[int] = None,
        org_maint_timeout: Optional[float] = None,
        on_tool_call: Optional[Callable[[str, str], None]] = None,
        on_tool_result: Optional[Callable[[str, str], None]] = None,
        on_progress: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.llm = llm or LLM()
        # 视觉是否可用（2026-09-15 用户口径"多模态是加分项，没有它也能干活"）：
        # 初始 True，**第一次图像请求被端点拒**就翻成 False，之后不再往请求里
        # 塞图（`_strip_images` 已经从"重发兜底"升级成"能力记忆"）。这样换到
        # 不支持视觉的模型时，工具链照常工作，只是走 page_text / read_file 文本路。
        self._vision_ok = True
        self.max_turns = max_turns or _DEFAULT_MAX_TURNS
        # 同步实时进度回调（主线程执行）：等待模型、工具动作的即时提示
        self.on_progress = on_progress
        # **事件级直播回调**（2026-09-13，worklog §92）：每落一个事件推一份直播副本。
        # 与 `on_progress` 的区别：那个是"状态一句话"，这个是**事件本身**——前端
        # 因此能在运行中按时间顺序画出思考/工具卡/正文，而不是只有一段段文本增量。
        self.on_event: Optional[Callable[[dict], None]] = None
        self.task = task
        self.context_mode = context_mode
        self.context_limit = context_limit or _DEFAULT_CONTEXT_LIMIT
        # 整理是否异步执行：chat 模式开（不阻塞对话），run/测试用同步（确定性）
        self.async_organization = async_organization
        # V3 水位批量整理参数（水位 = 未整理轮原始内容体量阈值；2026-09-09
        # 用户拍板：批次上限删除——触发只看窗口是否到水位，到水位收编全部
        # 未整理轮）
        self._org_watermark = (
            _ORG_WATERMARK_DEFAULT if org_watermark is None else org_watermark
        )
        # 保护机制（2026-09-08 用户拍板）：宽限期 + 冷却间隔。
        # 宽限 = 会话前 N 轮硬豁免维护——开头几轮（项目导览/目标陈述）是
        # 判据二"解释现状的最小历史"的核心，且大项目首查容易瞬间装满，
        # 不豁免会刚开场就压缩（窗口保底紧急折叠不受豁免，是另一条线）。
        # 冷却 = 两次维护之间的最小轮距，适配大小不同的起点、防高频。
        # 分裂后水位按 agent 各自计量、有效容量随分裂增长，阈值无需上调
        # ——保护旋钮主要服务分裂前的单体阶段。
        self._org_grace = (
            _ORG_GRACE_ROUNDS_DEFAULT if org_grace_rounds is None else org_grace_rounds
        )
        self._org_cooldown = (
            _ORG_COOLDOWN_ROUNDS_DEFAULT
            if org_cooldown_rounds is None
            else org_cooldown_rounds
        )
        self._org_maint_timeout = (
            _ORG_MAINT_TIMEOUT_DEFAULT if org_maint_timeout is None else org_maint_timeout
        )
        # 结算（水位处攒批，V4 §6.1）的硬上限与单批轮数上限；超时按失败处理、只留痕
        self._note_timeout = _NOTE_TIMEOUT_DEFAULT
        self._note_batch_max = _NOTE_BATCH_MAX_DEFAULT
        # 换档后保留原文的最近轮数（§3.8/§3.9 的"近 50 轮"）与折到水位的比例
        self._fold_keep = _FOLD_KEEP_ROUNDS_DEFAULT
        self._fold_target = _FOLD_TARGET_DEFAULT
        # 已入队/整理中的轮次 seq：命中率的计量口径里它们不算"未整理"，
        # 避免批量整理排队期间被下一次触发重复收编
        self._org_inflight: set[int] = set()
        # 回合内转交的待办落点（route_to，2026-09-12）：工具方法只把目标
        # 记在这里，由 `_work_loop` 在工具批次跑完后换视图——工具执行期间
        # 改装配会让同一批里后续工具看到错乱的上下文。跳数记在轮上
        # （route_hops）随轮持久化，使 `\c` 续跑不会把上限重置掉。
        self._pending_route: str = ""
        # 视图/产物生效与"开新轮"的互斥锁（2026-09-12，§50）：维护线程跑完
        # 时若用户没在轮里，就立刻让产物生效（子 agent 与重组上下文在下一轮
        # 开场前建好）；而"开新轮"是唯一的切换点——两者必须互斥，否则会出现
        # "正在开轮、产物同时改写装配"的竞选，破坏前缀纪律。
        self._view_lock = threading.RLock()
        # 维护快照的推迟标记（2026-09-11 实测缺陷的落点；§53 之后成**防御性**
        # 路径）：若轮在工具方法体**内部**被闭合（历史上由检查点切轮触发：
        # todo→verify_milestone→close_round），此刻调用方的 assistant(tool_calls)
        # 还没有 tool 结果。若在此刻取装配快照，尾部就是"未被回复的 tool_calls"，
        # 紧接着追加的整理指令以 user 角色出现——严格端点直接 400（实测 21:27
        # 那批 1 秒失败、未计费，靠下一批次补上）。置位后由 _finish_tool_result
        # 在结果落盘后补做水位检查。§53 把检查点改为轮内标记后，内置路径不再在
        # 工具体内闭合轮，但 \c 续跑/中断重放/未来实现仍可能需要它。
        self._maint_deferred = False
        # 整理代次（2026-09-10 用户拍板：视图只保留最近 3 批整理，更早的
        # 按文件状态折叠）：每次成功整理 +1，批次轮打 org_generation 落盘；
        # 旧轮无该字段视为第 1 代（最老，优先折叠）。计数在本类里就地
        # 恢复（见 rounds 加载处）——重启后从 0 重来会与磁盘旧批次撞号，
        # 折叠判定反转（新批次被当最老折叠、旧批次反倒全量）。
        self._org_generation = 0
        # 子任务派发板：每轮刷新的机械状态行（进程/账本/升级计数），
        # 注入装配尾部——主 agent 每轮都"看得见"子任务进展
        self.on_tool_call = on_tool_call
        self.on_tool_result = on_tool_result
        # 后台动作（如 Round 整理）耗时较长，状态消息进入 feed，
        # 由主线程在安全时机（提示输入前）统一打印——后台线程绝不直接
        # print：会打碎输入行，且 patch_stdout 会吞掉 ANSI 颜色码（踩过的坑）
        self._status_feed: list[str] = []

        self._bind_globals()

        self.tools: dict[str, Callable] = {}
        # 传给子 agent 的工具集（纯模块函数）。expand_history 与组织工具
        # 由各 Agent 自己注册、绑定各自的 Task——不能从父级继承，否则
        # 子 agent 的 expand_history 会读到父任务的历史
        self._tool_fns = tuple(tools)
        self._schemas: list[dict] = []
        # 本轮冻结的 tools 数组（None ＝ 未开轮，用当前阶段算）；见 `_stage_schemas`
        self._round_schemas: Optional[list[dict]] = None
        # 本轮已记过观察的 (相对路径, 是否写) —— 同一轮同一个文件只存一份快照（§3.7）
        self._observed_this_round: set[tuple[str, bool]] = set()
        for fn in tools:
            self.register(fn)

        self.turn_count = 0
        self.rounds: list[dict] = [dict(r) for r in (task.rounds if task else [])]
        # 代次计数就地恢复：每次成功整理 +1，重启后必须从磁盘上的最大值
        # 续起——从 0 重来会与旧批次撞号，折叠判定随即反转（新批次被当
        # 最老折叠、旧批次反倒全量）。口径与装配端一致：_assemble_messages
        # 把无 org_generation 的已整理轮视为第 1 代，故恢复时同样按
        # "done 且无字段 → 1"计入，不能只读字段。
        self._org_generation = max(
            (
                r.get("org_generation")
                or (1 if r.get("org_state") == "done" else 0)
                for r in self.rounds
            ),
            default=0,
        )
        self.current_round: Optional[dict] = None
        self.messages: list[dict] = []
        self.system_prompt = system_prompt

        # 异步整理：单线程 FIFO 维护管线（History Maintenance Pipeline）
        self._org_queue: queue.Queue = queue.Queue()
        self._org_thread: Optional[threading.Thread] = None
        self._save_lock = threading.Lock()
        # 维护账本：整理/压缩的用量单独累计（异步、跨轮次边界），
        # 由每次 usage 记账时统一取走；last_maint 存最近一次快照供展示
        self._maint_usage = {
            "organization": {"prompt": 0, "completion": 0, "total": 0, "seconds": 0.0},
            "compaction": {"prompt": 0, "completion": 0, "total": 0, "seconds": 0.0},
            "split": {"prompt": 0, "completion": 0, "total": 0, "seconds": 0.0},
            "note": {"prompt": 0, "completion": 0, "total": 0, "seconds": 0.0},
        }
        self._maint_lock = threading.Lock()
        self.last_maint: dict = {}
        # 最近一次装配的上下文体量估算（每步更新；轮结束时即该轮峰值）
        self.last_context_estimate = 0
        # 端点不支持整理参数的降级提示：每次会话只提示一次，避免每轮刷屏
        self._degrade_warned = False
        # 最近一次流式调用的 finish_reason（stop/length/tool_calls/未返回）：
        # 流被掐断时 usage 也缺失，这是唯一诊断线索（空响应防护用）
        self._last_finish_reason: Optional[str] = None
        # ⏹ 协作式取消（网页终止按钮）：返回 True 即抛 KeyboardInterrupt，
        # 语义与 CLI Ctrl+C 一致（轮保持开放）。每步边界 + 流中分片间检查
        self.cancel_check: Optional[Callable[[], bool]] = None
        # 工具批次执行中（维护闸门据此区分"在跑"与"重载遗留悬空尾"）
        self._tools_running = False
        # 最近一步的思考全文（随 tool_call / final_answer 事件落盘，只进
        # event 不进 message——装配读 message，上下文不受影响）
        self._last_thinking = ""

        # baseline 记账：累计输入 token（触发阈值压缩）
        self._baseline_prompt_used = task.baseline_prompt_used if task else 0

        self.last_stats = self._fresh_stats()
        # 账本按**轮**累计（供显示），落账写**增量**：这两个是增量基线与轮号标记
        # （2026-09-13，见 `_work_loop` / `_usage_record_and_drain`）
        self._stats_drained: Optional[dict] = None
        self._stats_round: Optional[int] = None

        if self.context_mode == MODE_MANAGED:
            self.register(self.expand_history)
            # 每轮一段话的出口（V4 §3.2）：常驻注册，tools 数组恒定；
            # 工作期误调用由方法体守卫拒绝（见 ledger.submit_round_notes）
            self.register(self.submit_round_notes, schema=_ROUND_NOTE_SCHEMA)
            # 整理提交工具常驻：所有请求的 tools 数组恒定（工作对话与整理
            # 调用同序列化），前缀缓存才不会在 tools 区分叉。工作期误调用
            # 由方法体守卫拒绝（见 submit_organization / submit_domains）。
            self.register(self.submit_organization, schema=_ORG_SUBMIT_SCHEMA)
            self.register(self.submit_domains, schema=_ORG_DOMAINS_SCHEMA)
            # 阶段/工作项计划账本（工作工具，深度恒 1，见 todo-milestone-tool.md）
            self.register(self.todo, schema=_TODO_SCHEMA)
            # 跨 agent 通信（机制五）：单向 notify / 双向 consult
            self.register(self.notify, schema=_NOTIFY_SCHEMA)
            self.register(self.consult, schema=_CONSULT_SCHEMA)
            # 路由（Level 1 第三步）：拉职责表（跨 agent 唯一的公共信息面）
            self.register(self.list_agents, schema=_LIST_AGENTS_SCHEMA)
            # 回合内转交（2026-09-12 用户拍板）：主 agent 的**本职动作**——
            # 收到消息、对职责表、把用户原话转给对应 agent，由它在本回合内
            # 直接接续干活（不需要再回主 agent 转述）。只在 managed 下注册：
            # baseline 装配不看视图，转交没有落点。
            self.register(self.route_to, schema=_ROUTE_TO_SCHEMA)
            # 职责/文件清单：**自己改自己的，干完立马生效**（2026-09-12 用户口径）
            self.register(self.update_responsibility,
                          schema=_RESPONSIBILITY_SCHEMA)
            # 会合：把谁排进本轮的参与者队列（都干完才收轮）
            self.register(self.join_with, schema=_JOIN_WITH_SCHEMA)

    def _bind_globals(self) -> None:
        """把进程级全局绑定对准本会话（审计记录器、后台任务归属）。

        在 Agent 构造时对准本会话；后台任务按会话归属治理。
        """
        tools_module.set_audit_recorder(
            lambda detail: self.task.record("file_change", detail) if self.task else None
        )
        # 文件权限守卫（工具层强制，2026-09-12 用户口径：干不干看有没有
        # 改写删权）：不在构造时粘性绑定，而是每次工具调用用 `guard_scope`
        # 临时绑定"此刻是谁在干活"——见 `_invoke_tool`。这里只做自检性的
        # 清空，避免上一个会话的守卫残留到本会话的第一次调用前。
        # 后台任务按会话归属：启动/查看/停止都限定在本会话内
        tools_module.set_current_session(self.task.id if self.task else None)

    def _fresh_stats(self) -> dict:
        return {
            "seconds": 0.0,
            "turn": self.turn_count,
            "llm_calls": 0,
            "tool_calls": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "reasoning_tokens": 0,
            "total_tokens": 0,
            "cached_tokens": 0,
            "cache_miss_tokens": 0,
            # 首 token 延迟（TTFT）：基座的第二张面孔（延迟乘数）的仪器。
            # seconds = 本轮各步 TTFT 之和（用户感知的等待），max = 最卡的一步
            "ttft_seconds": 0.0,
            "ttft_max": 0.0,
            "mode": self.context_mode,
            # **按调用方分账**（2026-09-12 用户口径：「钱，谁花的，消费时就记录啊」）：
            # 一次调用落一次账，键是那一刻的执行方（视图名/`Main`）。轮的总消费
            # = 各调用方之和（A 300K + B 400K = 700K）。整理/压缩/分裂的开销
            # 不在这里——它们走 `_maint_usage`（运行时账），不摊给任何 agent。
            "by_agent": {},
            "purpose": {
                "working": {"prompt": 0, "completion": 0, "total": 0, "seconds": 0.0},
                "organization": {"prompt": 0, "completion": 0, "total": 0, "seconds": 0.0},
                "compaction": {"prompt": 0, "completion": 0, "total": 0, "seconds": 0.0},
            },
            "prompt_breakdown": dict.fromkeys(tokens.CATEGORIES, 0),
        }

    def register(self, fn: Callable, schema: Optional[dict] = None) -> None:
        """把一个 Python 函数注册为模型可调用的工具。

        schema 缺省由函数签名自动生成；传入则整体覆盖（submit_organization
        的嵌套产出契约用手工 schema，_schema_of 只会生成扁平结构）。
        """
        if fn.__name__ in self.tools:
            raise ValueError(f"工具重复注册: {fn.__name__}")
        self.tools[fn.__name__] = fn
        self._schemas.append(schema or _schema_of(fn))

    # **身份类工具**：只有存在别的 agent 才有意义（转交/咨询/会合/通知/看名册/改自己职责）。
    _IDENTITY_TOOLS = ("route_to", "consult", "join_with", "notify",
                       "list_agents", "update_responsibility")

    def _has_sub_agents(self) -> bool:
        """分裂落过地没有：注册表里出现过非 Main 的条目（与 `_note_per_round` 同一信号）。"""
        if self.task is None:
            return False
        return any(
            isinstance(e, dict) and str(e.get("id") or "") not in ("", "Main")
            for e in (self.task.registry or [])
        )

    def _stage_schemas(self) -> list[dict]:
        """本轮用的 tools 数组：**按轮冻结**（AGENTS §2：tools 序列化在一轮内不许变）。

        为什么要冻结：身份类工具的广告随"有没有子 agent"变，而**就地新建**会在轮中长出
        第一个子 agent——不冻的话，这一轮后半段的请求就换了 tools 数组，整段前缀作废。
        冻结后的变化点落在**下一次开轮**（本就是允许断前缀的边界）。
        """
        if self._round_schemas is not None:
            return self._round_schemas
        return self._compute_stage_schemas()

    def _compute_stage_schemas(self) -> list[dict]:
        """按阶段给 tools 数组：**分裂前不广告身份类工具**（2026-09-17 用户口径）。

        同一阶段内数组恒定（AGENTS §2 硬线）；分裂落地那一刻整段换一次——那本来就是
        允许断前缀的断点，不额外付费。分裂前那 6 个工具一个都用不上（没有别的 agent
        可转、可问），广告它们只会让模型去想"我是谁、该转给谁"。

        注意：**只影响"广告"**（请求里的 tools 序列化），工具方法仍然注册着，直接调用
        照旧可用（守卫与既有测试不受影响）。
        """
        if self._has_sub_agents():
            return list(self._schemas)
        return [
            s for s in self._schemas
            if str((s.get("function") or {}).get("name") or "") not in self._IDENTITY_TOOLS
        ]

    def _open_or_reuse_round(self, user_input: str) -> bool:
        """开启新 Round，或续上未闭合的开放 Round（V2 闭合规则）。

        上一轮若因中断/异常/无回复而未闭合（end_state=open），
        本轮输入并入同一个 Round——直到 AI 产出最终回答才算完整一轮。
        返回是否开启了新 Round：开启时要把上一轮暂存的整理产物生效
        （_promote_org_results），续轮则不动——本轮装配必须保持原样。

        整体持 `_view_lock`（§50）：与后台维护线程的"产物即时生效"互斥，
        保证"当前有没有开放轮"这个判定与开轮动作不会交错。
        """
        with self._view_lock:
            self._round_schemas = self._compute_stage_schemas()   # 轮内冻结（见 _stage_schemas）
            self._observed_this_round = set()
            return self._open_or_reuse_round_locked(user_input)

    def _open_or_reuse_round_locked(self, user_input: str) -> bool:
        last = self.rounds[-1] if self.rounds else None
        if last is not None and last.get("end_state") in ("", "open"):
            self.current_round = last
            # 协议消息从事件的 Full 中重建（它们就是事实来源）
            self.messages = [e["message"] for e in last["events"]]
            return False
        seq = len(self.rounds) + 1
        self.current_round = {
            "seq": seq,
            "user_input": {"original": user_input, "normalized": ""},
            "events": [],
            "refined_index": {},
            "end_state": "open",
            "org_state": "",
            # 本轮归谁（Level 1 第三步路由，2026-09-12）：路由只在**轮开启**
            # 这一个切换点做一次，结果随轮持久化（冻结——同一视图连续两轮
            # 除尾部追加外字节不变）；开关关闭或缺省时恒为主 agent，装配
            # 与今天逐字节相同。若紧接着有分裂产物生效，`_settle_views`
            # 会按新职责表补判（渐近归属：先归位、再干活）。
            # 2026-09-12 改版：**恒由主 agent 起手**，规则结果只作为
            # `route_hint` 建议（见 `_route_view`）。
            "active_view": "",
            "route_hint": {},
            # 本回合已转交次数（route_to 跳数上限的落点，随轮持久化——
            # `\c` 续跑不会把上限重置掉，见 support._MAX_ROUTE_HOPS）
            "route_hops": 0,
            # 本回合已"看图"次数（view_image 预算的落点，同样随轮持久化——
            # 防视觉题反复裁图空转，见 support._IMAGE_VIEW_SOFT/HARD）
            "image_views": 0,
        }
        self.rounds.append(self.current_round)
        self.messages = []
        # 暂判一次（产物若恰好在此刻生效，由调用方在 promote 之后用
        # `_settle_and_route` 重判——先归位、再干活；此刻的判是纯函数，
        # 覆盖代价为零，且让"没有待生效产物"的常规路径一次到位）
        self._route_view(user_input)
        return True

    def _view_file_hints(self) -> dict[str, list[str]]:
        """域 → 该域真实出现过的文件（路由的文件名命中判据；机械、带缓存）。

        职责表里的 `file_domains` 常写目录，而用户提问常只提文件名——把材料
        里真实归属过该域的文件收进来，路由才不至于"只有写全路径时才命中"。
        惰性缓存按（轮数, 注册表条目数）失效：轮内多次调用只算一次，
        而分块是现场重算（有成本），故不放在每步路径上。
        """
        from .. import views as views_module

        key = (len(self.rounds), len((self.task.registry if self.task else None) or []))
        cache = getattr(self, "_hints_cache", None)
        if cache is not None and cache[0] == key:
            return cache[1]
        domains = views_module.latest_domains(self.rounds)
        hints = views_module.files_by_domain(self.rounds, domains) if domains else {}
        self._hints_cache = (key, hints)
        return hints

    def _rule_view(
        self, user_input: str, target: Optional[dict] = None, *, explicit: str = ""
    ) -> dict:
        """规则路由的**建议**（纯函数、零 LLM）：显式转交 → 文件命中 → 粘滞 → 主 agent。

        见 `routing.route`。2026-09-12 改版后它不再是"直接落地"的判定，
        而是喂给主 agent 的起点建议（`route_hint`）——判断权归主 agent 的模型。
        """
        from .. import routing as routing_module

        if self.task is None:
            return {
                "view": views_module.MAIN_AGENT_ID,
                "reason": "无任务绑定",
                "matched": [],
            }
        return routing_module.route(
            user_input,
            self.task.registry,
            sticky=self._sticky_view_before(target or self.current_round or {}),
            explicit=explicit,
            file_hints=self._view_file_hints(),
        )

    def _route_view(
        self, user_input: str, round_: Optional[dict] = None, *, record: bool = True
    ) -> str:
        """给某一轮定**起手视图**——2026-09-12 用户口径：**每轮恒由主 agent 起手**。

        固定流程（用户原话：「分裂后，每次输入，都是主agent先触发，然后路由
        原话给对应agent，然后子agent进行回复。每轮都是这个流程」）：

        ```text
        用户输入 → 主 agent（Main）收到 → 用 route_to 把原话转给对应域
                 → 子 agent 在本回合内直接回答
        ```

        故规则路由（文件命中/粘滞）**不再直接落子域**，只作为 `route_hint`
        塞进主 agent 的运行时信封（[路由建议]）供它参考。**没有任何例外**：
        原来的"显式转交"（`switch_view`：上一轮预约下一轮归谁）已按用户口径
        删除——agent 不能决定下一轮归谁，所有权只由"这一轮实际是谁在做"
        产生（轮上的 `active_view`）。

        调用点两处：新轮开启（产物生效之后，见 `_settle_and_route`）与渐近
        归属补判（`_settle_views`，record=False 时只补判不逐条留痕）。
        """
        from .. import routing as routing_module

        target = round_ if round_ is not None else self.current_round
        if target is None:
            return views_module.MAIN_AGENT_ID
        if self.task is None or not routing_module.active_view_enabled():
            target["active_view"] = views_module.MAIN_AGENT_ID
            target["route_hint"] = {}
            return views_module.MAIN_AGENT_ID
        result = self._rule_view(user_input, target)
        target["active_view"] = views_module.MAIN_AGENT_ID
        if result["view"] == views_module.MAIN_AGENT_ID:
            target["route_hint"] = {}
        else:
            target["route_hint"] = {
                "view": str(result["view"]),
                "reason": str(result.get("reason") or ""),
                "matched": list(result.get("matched") or []),
            }
            if record:
                self.task.record(
                    "route",
                    f"R{target.get('seq')} 起手 {views_module.MAIN_AGENT_ID}；"
                    f"规则建议 → {result['view']}（{result.get('reason')}）",
                )
        return views_module.MAIN_AGENT_ID

    def _sticky_view_before(self, target: dict) -> str:
        """target 之前最近一个已归域的轮（粘滞判据；时序号为准）。"""
        try:
            seq = int(target.get("seq") or 0)
        except (TypeError, ValueError):
            seq = 0
        sticky = ""
        for r in self.rounds:
            if r is target:
                continue
            try:
                if int(r.get("seq") or 0) >= seq:
                    continue
            except (TypeError, ValueError):
                continue
            view = str(r.get("active_view") or "")
            if view:
                sticky = view
        return sticky

    def _settle_and_route(self, user_input: str) -> int:
        """产物生效之后：先给未归域的轮补判（渐近归属），再判本轮。

        顺序不能颠倒（2026-09-12 用户口径）：维护窗口内到达的几轮是用**旧**
        注册表判的（那时分裂产物还没生效），必须先按同一套逻辑补判归位，
        本轮才能"粘"到补判结果上——否则新一轮会按主 agent 起头，接不上
        那几轮的归属。
        """
        settled = self._settle_views()
        self._route_view(user_input, self.current_round)
        return settled

    def _settle_views(self) -> int:
        """渐近归属：分裂产物生效后，给尚未归域的轮补判视图（只归位、不重做活）。

        为什么需要：整理/分裂在后台跑的那段时间里，用户可能又发了几轮输入；
        它们到达时活跃域树还是空的，路由只能判给主 agent。产物一生效就必须
        按同一套路由逻辑补判——把属于某域的上下文归过去（那几轮此后由该域
        视图承载），归完才处理新一轮输入。

        补判是**纯函数、零 LLM、不改任何消息字节、不重做活**：只写轮上的
        `active_view` 这一个标记，故不会带来任何模型成本，也不会打断正在
        进行的活。已归域的轮不反复推翻（归属只随新产物前进）；判定结果不变
        时不写盘，故反复 promote 无抖动。
        """
        from .. import routing as routing_module

        if self.task is None or not routing_module.active_view_enabled():
            return 0
        if not views_module.latest_domains(self.rounds):
            return 0
        changed: list[str] = []
        for r in self.rounds:
            if r is self.current_round:
                continue  # 本轮由 _settle_and_route 在补判之后单独判（顺序要求）
            if str(r.get("active_view") or "") not in ("", views_module.MAIN_AGENT_ID):
                continue  # 已归域：不推翻
            text = str((r.get("user_input") or {}).get("original") or "")
            before = str(r.get("active_view") or "")
            # 补判用**规则建议**（不是 `_route_view`）：新一轮流程是"恒由主
            # agent 起手"，而补判补的是**材料归属**——那几轮到达时域树还不存在，
            # 只能由主 agent 答；产物生效后按规则把它们的材料归到对应域视图。
            # `route_explicit` 只作**历史数据**的读取（那个工具已删）：老会话
            # 里由它预约过归属的轮，补判按当时的意志走，不改写历史归属。
            after = str(
                self._rule_view(
                    text, r, explicit=str(r.get("route_explicit") or "")
                )["view"]
            )
            if after != before:
                r["active_view"] = after
                changed.append(f"R{r.get('seq')}→{after}")
        if changed:
            self.task.record(
                "route",
                "渐近归属：产物生效后补判 " + "、".join(changed[:12])
                + ("…" if len(changed) > 12 else "") + "（只归位，不重做活）",
            )
            self._persist_rounds()
        return len(changed)

    def _file_guard_object(self):
        """工具层调用的权限守卫对象（`__call__` 查权限，`claim` 记新文件归属）。"""
        agent = self

        class _Guard:
            def __call__(self, op: str, path: str):
                return agent._file_permission(op, path)

            def claim(self, op: str, path: str) -> None:
                agent._claim_new_file(path)

        return _Guard()

    @staticmethod
    def _rel_path(path: str) -> str:
        """把模型给的路径归一到**工作区相对 POSIX 串**（清单里存的就是这个形态）。"""
        raw = str(path or "").strip()
        if not raw:
            return ""
        try:
            target = tools_module.safety._safe_path_lexical(raw)
            return target.relative_to(tools_module.safety.workspace_root()).as_posix()
        except Exception:  # noqa: BLE001——越界/畸形路径交给工具层原有检查
            return raw.replace("\\", "/").strip("/")

    def _file_permission(self, op: str, path: str) -> Optional[str]:
        """**工具层硬权限**（用户口径：干不干看有没有改写删权，不看"该不该"）。

        ```text
        P1 自己的文件（清单内）：读/写/改/删
        P2 别人的文件：只读
        P4 分裂之前（注册表里只有主 agent）：主 agent 全权
        P5 分裂之后：主 agent 权限与子 agent 相同
        F5 新文件：谁创建谁拥有（放行，创建成功后由 claim 落册）
        ```
        没有任何域认领**已存在**的文件 = 分裂/整理的缺陷（用户口径：不存在
        "未认领文件"）→ 拒绝并叫人，不静默吸进谁的桶。
        """
        if self.task is None:
            return None
        registry = [e for e in (self.task.registry or []) if isinstance(e, dict)]
        subs = [e for e in registry
                if str(e.get("id") or "") != registry_module.MAIN_AGENT_ID]
        if not subs:
            return None                       # P4：分裂前主 agent 全权
        rel = self._rel_path(path)
        view = self._active_view()
        mine = self._registry_entry_for(view)
        if mine is not None and registry_module.file_owned_by(mine, rel):
            return None                       # P1：自己的文件，全权
        owner = registry_module.owner_of_file(registry, rel)
        if op == "read":
            # **分裂后主 agent 连读也不干**（2026-09-17 用户口径："主 agent 不可以干任何活，
            # 除了路由，不然会污染上下文"；"有主的活，例如读，让对应 agent 来，也许可以节省
            # 一次读呢"）——主人手里往往已经有那份内容。子 agent 之间读还是只读放行（P2）。
            if str(view or "") == registry_module.MAIN_AGENT_ID and owner is not None:
                return (
                    f"权限拒绝：{path} 是 {owner} 的活——**读也算它的活**，它手里往往"
                    f"已经有这份内容（还能省一次读）。用 route_to 把用户原话转给它；"
                    f"没有主的新活就用 route_to(agent=\"new\") 就地起一个。"
                )
            return None                       # P2：别人的文件只读
        if owner is not None:
            who = str((mine or {}).get("name") or view or "你")
            return (
                f"权限拒绝：{path} 不属于 {who}（属 {owner}）——**改写删只能动"
                f"自己维护的文件，别人的文件只能读**。这活该它干：用 route_to "
                f"把用户原话转给它（本回合内生效），或 consult 问它要判断。"
            )
        try:
            exists = tools_module.safety._safe_path_lexical(path).exists()
        except Exception:  # noqa: BLE001
            exists = False
        if not exists:
            return None                       # F5：新文件，谁创建谁拥有
        return (
            f"权限拒绝：{path} 已存在但**没有任何域认领**它——按口径这不该发生"
            f"（分裂/整理的缺陷）。停下来把这件事报告给用户，不要自己改写它。"
        )

    def _claim_new_file(self, path: str) -> None:
        """F5：新文件归属创建者——立即写进它自己条目的文件清单并落盘。

        顺带机械生一条**占位的一句话描述**（零 LLM，取首行/标题前 30 字）：
        用户口径要的是"文件描述永远新鲜"，而 agent 可能忘记写 → 先有占位，
        它下一轮用 `update_responsibility(file_notes=…)` 换成正式描述。
        """
        if self.task is None:
            return
        entry = self._registry_entry_for(self._active_view())
        rel = self._rel_path(path)
        if entry is None or not rel:
            return
        files = entry.setdefault("files", [])
        if rel in files:
            return
        files.append(rel)
        note = self._guess_file_note(rel)
        if note:
            entry.setdefault("file_notes", {})[rel] = note
        # 记账用独立的 kind：`file_change` 那条流水被 lifecycle/blocks 当状态信号读，
        # 归属变更不该混进去（它不改变文件状态，只改变"谁维护它"）。
        self.task.record(
            "ownership",
            f"[归属] {rel} 由 {entry.get('name')} 新建 → 计入它的文件清单",
        )
        self.task.save()

    @staticmethod
    def _guess_file_note(rel: str) -> str:
        """机械占位描述（零 LLM）：首行注释 / markdown 标题 / docstring 首句。"""
        try:
            target = tools_module.safety.workspace_root() / str(rel)
            head = target.read_text(encoding="utf-8", errors="ignore")[:600]
        except (OSError, ValueError):
            return ""
        for raw in head.splitlines():
            line = raw.strip()
            if not line:
                continue
            if line.startswith("#"):
                line = line.lstrip("#").strip()
            elif line.startswith(('"""', "'''")):
                line = line.strip("\"'").strip()
            elif line.startswith(("//", "*", "--", ";")):
                line = line.lstrip("/*-;").strip()
            if not line:
                continue
            return line[:30]
        return ""

    def _inject_file_map_if_changed(self) -> bool:
        """**变了才注入**文件地图（2026-09-12 用户口径）。

        用户的问法与要求：「是不是得有一个环境，整理、分裂写一个每个文件的一句
        话简单描述？然后注入，我希望是通过代码判断其是否变化，如果变化了才注入。」
        ——描述来自整理写的块摘要（不新造机制）；**注入口径**是：

        * 每次轮开启算一遍签名（`views.file_map_lines`，零 LLM）；
        * 与 `task.file_map_sig` 相同 → 什么都不做（不占位、不重复）；
        * 不同 → 把新地图作为一条 `runtime_note` 事件**追加进轮**（= 进历史），
          于是它此后是**前缀的一部分**（只付一次钱、且模型每轮都看得到；
          若只塞进尾部信封，下一轮就没了——信封不进历史）。

        只在轮开启时注入：轮进行中改装配会打断当前协议序列。

        **门：还没分裂就不注入**（2026-09-13 用户口径）——用户原话："现在还没有
        触发一次分裂，不需要路由，因此不需要这个东西"。地图的用处是"谁维护哪些
        文件"（路由与权限的依据）；注册表里只有主 agent 时没有归属歧义、没有
        路由可走，注入纯属噪声（还会白占一段前缀）。分裂之后才注入。
        """
        if self.task is None or self.current_round is None:
            return False
        if v4_enabled():
            # V4：这份地图（追加进历史那条）与"段落 ＋ 尾部职责表"重复，且它一变就是
            # 一次历史追加（前缀变化）——停用。
            return False
        subs = [e for e in (self.task.registry or [])
                if isinstance(e, dict) and e.get("name")
                and str(e.get("id") or "") != registry_module.MAIN_AGENT_ID]
        if not subs:
            return False
        lines, sig = views_module.file_map_lines(self.rounds, self.task.registry)
        if not lines or not sig or sig == str(self.task.file_map_sig or ""):
            return False
        self.task.file_map_sig = sig
        shown = lines[:120]
        # 抬头只说格式，不解释机制（同 §80 的产品口径：机制说明不进产品文案）
        body = "[文件地图]（每行 = 文件｜状态｜归属域｜一句话描述）\n"
        body += "\n".join(shown)
        if len(lines) > len(shown):
            body += f"\n…（共 {len(lines)} 个文件，只列前 {len(shown)} 行）"
        self._record_event("runtime_note", _runtime_reminder(body))
        self.task.record("maintenance", f"文件地图注入（签名 {sig}，{len(lines)} 个文件）")
        return True

    def _relay_to_next_participant(self, answer: str) -> bool:
        """会合交棒：本轮还有排队的参与者就把接力棒交给下一个（返回 True）。

        只在 `join_with` 排过队时生效（`round["participants"]` 非空）——没排过队
        的轮走原来那条路（干活 → 收轮），逐字节不变。交棒复用 `route_to` 的落地
        机制（登记 `_pending_route`，由批次边界换视图），区别是**谁触发**：
        那个是模型转交，这个是"我干完了，轮到你了"的机械接力。
        """
        r = self.current_round or {}
        queue = list(r.get("participants") or [])
        if not queue:
            return False
        nxt = queue.pop(0)
        target = str((nxt or {}).get("agent") or "")
        r["participants"] = queue
        if not target:
            return self._relay_to_next_participant(answer)
        me = self._active_view()
        if self.task is not None:
            self.task.chat.append({
                "round": r.get("seq"), "from": str(me or ""), "to": target,
                "text": f"[我已干完我这部分] {answer.strip()[:2000]}",
                "kind": "handoff",
            })
        self._pending_route = target
        self._apply_pending_route()
        ent = self._registry_entry_for(target)
        if ent is not None:
            ent.setdefault("inbox", []).append({
                "from": str(me or ""),
                "message": "[会合] 轮到你了：接着干你那部分（上一手的产出见这条线程）。",
            })
        if self.on_progress:
            self.on_progress(f"🤝 交棒 → {target}（本轮还有 {len(queue)} 位待干）")
        return True

    def _registry_entry_for(self, view: str) -> Optional[dict]:
        """按视图名或 ID 取注册表条目（`active_view` 两种形态都可能出现）。"""
        want = str(view or "").strip()
        if not want or self.task is None:
            return None
        for entry in self.task.registry or []:
            if not isinstance(entry, dict):
                continue
            if want in (str(entry.get("id") or ""), str(entry.get("name") or "")):
                return entry
        return None

    def _agent_window(self) -> int:
        """该 agent 的上下文窗口（3a）：模型上下文上限（WOVRA_CONTEXT_LIMIT，
        默认 1M）。此前误取整理水位——水位 100K 只是整理阈值，不是窗口
        （2026-09-12 用户纠正：展示口径应为 16.8K/1M，而不是 /100K）。"""
        return int(self.context_limit)

    def _touch_view_context(self, view: str, size: int) -> None:
        """记下某视图这次装配的体量（3a：每个 agent 自己的上下文与占比）。

        每次装配都调（一轮内多步、每步都装配）——故只更新最新值与峰值，
        不落盘；落盘由轮闭合时的 `save()` 一并带走（零额外 I/O）。
        """
        size = int(size or 0)
        window = self._agent_window()
        # **V4 取消重组 → 观察口径也跟着改**（2026-09-17）：所有 agent 装配的是
        # 同一份共享历史，所以每个条目登记的观察值就是**这一份的体量**——只写
        # 当前视图那一条，会让其余 agent 在页面/账本上显示 0（读起来像"没有
        # 上下文"，其实它们共用同一份）。注意这仍是"观察"不是"各自的副本"。
        targets = [entry for entry in (self.task.registry or [])
                   if isinstance(entry, dict)] if (self.task is not None and v4_enabled()) else []
        entry = self._registry_entry_for(view)
        if entry is None and not targets:
            return
        if entry is not None and entry not in targets:
            targets.append(entry)
        if not targets:
            return
        for target in targets:
            target["ctx_cur"] = size
            if size > int(target.get("ctx_peak") or 0):
                target["ctx_peak"] = size
            # 窗口每次都校正（无条件写）：老会话的 registry 里存过水位 100K，
            # 语义纠正后要随活动自然迁移到真实窗口，不能被旧值占住
            target["window"] = window
            if v4_enabled():
                target["shared_ctx"] = True

    # `_account_agent_activity()`（轮闭合时把轮次/步数写进注册表条目）已删除
    # ——2026-09-12 用户拍板：账本**派生、不落盘**。写入式账有两个死结：
    # ①闭合那一刻的快照会被后来的分裂/渐近归属补判回溯作废（存的是不再存在
    # 的归属状态）；②中断轮 `finalize_round` 与续跑闭合 `close_round` 各记一次
    # （实测 Σ14 vs 会话 13 轮）。现在由 `views.agent_ledger` 现场算承载轮/
    # 答复轮/步数，条目上只留 ctx_cur/ctx_peak/window 这些**观测**字段。

    def _prev_landing(self) -> str:
        """上一轮的**落点**（谁在答）——给主 agent 判断"这条是不是续说"用的事实。

        2026-09-13 用户口径：提示词里要让主 agent 考虑"与上一次用户询问的关联程度，
        特别是一些接近于续说的，优先考虑路由给上一次的那个域"。可它要判"是不是
        续说"就得先知道**上一次是谁在答**——这一条由 Runtime 机械给出（事实），
        权重仍归它自己判（用户：「不能说[路由建议]对了就提升其权重，还是得交给
        LLM 来，更灵活」）。
        """
        cur = (self.current_round or {}).get("seq")
        prev: Optional[dict] = None
        for r in (self.rounds or []):
            if not isinstance(r, dict):
                continue
            if cur is not None and r.get("seq") == cur:
                break                      # 到本轮为止，`prev` 就是上一条
            prev = r
        if prev is None:
            return ""
        return str(prev.get("active_view") or "").strip()

    def _route_hint_lines(self) -> list[str]:
        """主 agent 起手时的**路由依据**：上一轮落点（事实）+ 规则层建议（非命令）。

        两段各有分工（2026-09-13 用户口径）：
        * **事实**（上一轮谁在答）——Runtime 机械给，供主 agent 判"这条是不是续说"；
        * **建议**（规则层按文件域/粘滞给的起点）——只是起点，判断权仍在主 agent。
        """
        lines: list[str] = []
        prev = self._prev_landing()
        if prev and prev != views_module.MAIN_AGENT_ID:
            lines.append(
                f"[上一轮落点]（事实）上一轮是 {prev} 在答。"
                "判这一条要不要换人时，先看它**与上一条的关联程度**："
                "顺着上一句追问/补充/要细节/接着那个话题讲（续说）→ **优先沿用"
                "同一个域**；换了话题或换了对象（点名别人的活、提到别的域的文件、"
                "又开了一摊）→ 重新判。"
            )
        hint = (self.current_round or {}).get("route_hint") or {}
        view = str(hint.get("view") or "")
        if view and view != views_module.MAIN_AGENT_ID:
            reason = str(hint.get("reason") or "")
            lines += [
                "[路由建议]（规则层按文件域/粘滞给的起点，**不是命令**）",
                f"这一轮按规则更像 {view} 的活"
                + (f"（{reason}）" if reason else "")
                + "。你若同意就用 route_to 把**用户原话**转给它（它本回合内直接"
                "接手回话）；不同意就自己干——判断权在你，规则只是起点，转错了"
                "对方会自己转出去。",
            ]
        return lines

    def _apply_pending_route(self) -> bool:
        """把 `route_to` 登记的转交落到本轮 `active_view` 上（回合内换视图）。

        只在工具批次跑完后调用（批次执行期间换装配，会让同批里后面的工具
        按错乱的上下文理解自己看到的文件）。换的是**同一个 Round 内的视图**：
        历史段从主 agent 桶变成目标域桶，当前轮事件（用户原文 + 这次转交的
        tool_call 与结果）原样保留——目标接手那一刻看得到用户说了什么，
        也知道这活是怎么转过来的。

        路由留痕照记（人视图可查"这轮为什么给了它"）；跳数记在轮上，
        使 `\\c` 续跑不会把上限重置掉。
        """
        target = str(self._pending_route or "")
        self._pending_route = ""
        if not target or self.current_round is None:
            return False
        from .. import views as views_module

        before = str(self.current_round.get("active_view") or "") or views_module.MAIN_AGENT_ID
        if target == before:
            return False
        self.current_round["active_view"] = target
        self.current_round["route_hops"] = int(
            self.current_round.get("route_hops") or 0
        ) + 1
        # per-agent 参与度**不再记账**（2026-09-12 用户拍板：账本派生）：
        # 转出次数由 `views.agent_ledger` 从事件流里的 route_to 事件数出来
        # （每一次换手都算一次，一轮可转多次），比在这里 +1 更准也更抗补判。
        if self.task is not None:
            self.task.record(
                "route",
                f"R{self.current_round.get('seq')} 回合内转交：{before} → {target}"
                f"（本回合第 {self.current_round['route_hops']} 跳）",
            )
        if self.on_progress:
            self.on_progress(f"🔀 本回合改由 {target} 接手")
        self._persist_rounds()
        return True

    def _record_event(self, type: str, message: dict, tool_name: str = "",  # noqa: A002
                      thinking: str = "") -> dict:
        """把一条协议消息登记为 Event（生成 ID 与 Truncated 索引行）。

        **同时也是直播的事件源**（2026-09-13 大改，worklog §92）：落盘事件后把它的
        **直播副本**推给 `on_event`。此前直播流只推 `think`/`ans`/`status` 三种文本
        增量、**根本不推事件**，于是运行中看不到工具调用（"思考连一块、中间的工具块
        没有"就是这个）——因为**服务端就没推**。所有事件都从这里落账（唯一入口），
        所以这里就是唯一该挂的点。

        `thinking` 必须在**推直播副本之前**挂上（2026-09-14 修，用户报"思考完思考块
        就被清除了、只剩工具块"）：调用方原先在 `_record_event` 返回后才 `ev["thinking"]=…`，
        而直播副本在函数内部已经推出去了——**直播事件永远不带思考**，于是"事件一到、
        在飞缓冲一清，思考就从画面上没了"（刷新/轮结束后正式渲染才有，因为落盘那条带）。
        """
        if self.current_round is None:
            self.messages.append(message)
            return {"id": "", "message": message}
        seq = len(self.current_round["events"]) + 1
        event_id = f"R{self.current_round['seq']}-E{seq:02d}"
        event = truncate.make_event(event_id, type, message, tool_name=tool_name)
        if thinking:
            # 思考过程随事件落盘（零截断口径）；只进 event 不进 message——装配读
            # message，上下文内容不受影响。**先挂再推直播**（顺序是这条修复的全部）。
            event["thinking"] = thinking
        self.current_round["events"].append(event)
        self.messages.append(event["message"])
        if self.on_event is not None:
            try:
                self.on_event(self._live_event(event))
            except Exception:  # noqa: BLE001——直播推不出去不该影响干活
                pass
        return event

    def _live_event(self, event: dict) -> dict:
        """事件的**直播副本**（形状与 `/rounds/{seq}` 给前端的完全一致）。

        为什么要抄一份而不是直接推事件本体：落盘那条含 `timestamp`/`thinking`/
        `message` 嵌套，而前端官方渲染吃的是扁平形状（`serve.round_detail` 的产物）
        ——两边同形状，前端才能**共用同一个"事件 → 条目"映射**，工具卡、思考、
        正文才会按同一套规则、按时间顺序排出来。

        长正文按 8000 字截断：工具结果可以几百 KB，而直播流是内存里的分片列表。
        **截的是副本**——落盘那条是完整的，轮结束后正式渲染照旧给全文。
        """
        msg = event.get("message") or {}
        content = str(msg.get("content") or "")
        cap = 8000
        if len(content) > cap:
            content = content[:cap] + f"\n…（直播只显示前 {cap} 字；完整内容轮结束后可读）"
        return {
            "id": event.get("id"), "type": event.get("type"),
            "agent": self._active_view(),
            "time": event.get("timestamp", ""),
            "thinking": event.get("thinking", ""),
            "status": event.get("status", ""),
            "role": msg.get("role"), "content": content,
            "tool_calls": msg.get("tool_calls"),
            "tool_call_id": msg.get("tool_call_id"),
        }

    def _maint_snapshot(self) -> Optional[list[dict]]:
        """维护调用的输入快照；装配协议不完整时返回 None（不取快照）。

        整理/分裂是一次**追加式对话**：整段装配原样 + 尾部追加指令（user
        角色）。因此装配序列必须协议完整——一旦以"未被回复的
        assistant tool_calls"收尾，追加的 user 消息就是非法序列，严格
        端点直接 400。实测（worklog §16.4）：里程碑闭合撞水位维护时，
        快照取在"工具方法体内闭合轮、结果尚未落盘"的瞬间，21:27 那批
        1 秒失败、未计费，产物只能等下一批次补上。

        这里只做机械校验（零 LLM）：assistant 声明的 tool_call 必须在
        任何后续 user 消息之前拿到 tool 结果，且序列不能以悬空收尾。

        **快照取全量材料，不取当前视图**（2026-09-12 修复，worklog §44）：
        原实现取 `self._assemble_messages()`，而视图分化后它是"当前 active_view
        那一桶"，域产物一出现维护输入就从 333 条掉到 9 条 → 整理连续失败。
        整理/分裂面对的是所有未整理轮的全部材料，故走 `_assemble_full_messages()`。
        """
        msgs = self._assemble_full_messages()
        pending: set[str] = set()
        for m in msgs:
            role = m.get("role")
            if role == "tool":
                pending.discard(m.get("tool_call_id"))
            elif role == "assistant":
                for call in m.get("tool_calls") or []:
                    if call.get("id"):
                        pending.add(call["id"])
            elif role == "user" and pending:
                return None  # user 消息打断了未被回复的 tool_calls
        return None if pending else msgs

    def _emit_status(self, text: str) -> None:
        """后台线程往状态队列里投递一条消息（线程安全：list.append 原子）。"""
        self._status_feed.append(text)

    def drain_status(self) -> list[str]:
        """主线程取走全部待打印的后台状态（打印时机由主线程决定）。"""
        out = []
        while self._status_feed:
            out.append(self._status_feed.pop(0))
        return out

    def _rebase_if_stale(self) -> bool:
        """盘上被别人写过就先重读、**并集合并**，再落盘（worklog §86）。

        **为什么需要**（用户 2026-09-13 质疑"整理、分裂不是异步吗？怎么会卡我"）：
        serve 是**每轮新建一个 agent**（chat 是整会话一个 agent），所以
        `async_organization=True` 之后，后台维护线程（旧 agent）与下一轮的 agent
        会**同时写同一份 `task.json`**，而两边写的都是**整份文件**——后写的会把
        先写的盖掉。最坏的一种：维护刚 promote 出来的分裂产物（`pending_org` +
        注册表条目）被下一轮的保存静默抹掉，下次再花一遍整理论文的钱。

        判据是**一次 stat**（`Task._saved_sig` 与盘上当前签名比）：只有真被
        别人改过才重读整份文件——常规路径（自己保存自己）零额外成本。

        合并口径：
        * **轮**：按 `seq` 并集——盘上有、我没有的（下一轮刚开的）并进来，不丢别人的轮；
          两边都有的取并集（我这边的新键覆盖，别人的新键保留）；
        * **注册表**：按 `id` 并集——我没有的条目（别人新认领的文件/新域）并进来，
          我有的以我为准（我这边的职责更新是刚写的）。

        **这是"两边都重读再写"才能成立的口径**：谁最后写，谁写下去的都是双方数据的
        并集，于是不存在"后写的把先写的盖掉"。
        """
        if self.task is None:
            return False
        from .. import task as task_module
        path = task_module.TASKS_ROOT / str(self.task.id) / "task.json"
        try:
            st = path.stat()
            now = (st.st_mtime_ns, st.st_size)
        except OSError:
            return False
        if now == getattr(self.task, "_saved_sig", None):
            return False                     # 盘上还是我那一版：不用动
        try:
            fresh = task_module.Task.load(str(self.task.id))
        except Exception:  # noqa: BLE001——重读失败就按自己那份写（不比原来更差）
            return False
        disk = {r.get("seq"): r for r in (fresh.rounds or [])
                if isinstance(r, dict)}
        mine = {r.get("seq"): r for r in self.rounds if isinstance(r, dict)}
        for seq, r in disk.items():
            if seq not in mine:
                self.rounds.append(r)        # 别人的轮：并进来（不丢）
        for _seq, r in mine.items():
            d = disk.get(_seq)
            if d is None or d is r:
                continue
            for key, value in d.items():
                # **谁有取谁**：我这边空/缺的键，用别人的补上——维护产物
                # （`pending_org` 等）就是这么活下来的；我这边有值的以我为准
                if key not in r or r.get(key) in (None, "", [], {}):
                    r[key] = value
        try:
            self.rounds.sort(key=lambda x: x.get("seq") or 0)
        except Exception:  # noqa: BLE001
            pass
        ids = {str(e.get("id")) for e in (self.task.registry or [])
               if isinstance(e, dict)}
        for e in (fresh.registry or []):
            if isinstance(e, dict) and str(e.get("id")) not in ids:
                self.task.registry.append(e)     # 别人的新条目：并进来
        # **history 也要并集合并**（2026-09-15 修，用户问"为什么没有留痕"）：
        # 原先只合并 rounds/registry，而两个写者（维护线程所在旧实例 / 正在跑的轮
        # 的**新**实例，serve 每轮新建 agent）都写**整份**文件——维护线程写的
        # "结束/超时/失败"记录会被轮的下一次保存静默覆盖。实测会话
        # 20260914-181519-0d3875：只有 09:06:23 的"启动"活着（新实例是在它之后
        # 才加载的），之后 split 的结束/超时/失败全没了——维护失败于是永远查不出来。
        # 合并口径：按 (time, kind, detail) 去重后取并集，再按时间稳定排序。
        seen = {(str(e.get("time")), str(e.get("kind")), str(e.get("detail")))
                for e in (self.task.history or [])}
        extra = [
            e for e in (fresh.history or [])
            if (str(e.get("time")), str(e.get("kind")), str(e.get("detail"))) not in seen
        ]
        if extra:
            self.task.history.extend(extra)
            try:
                self.task.history.sort(key=lambda e: str(e.get("time") or ""))
            except Exception:  # noqa: BLE001——排序失败不该挡住合并
                pass
        if self.task is not None:
            self.task.record(
                "maintenance",
                "盘上已被另一方写过 → 重读并集合并后再落盘（后台整理与下一轮"
                "同时写 task.json，防互相覆盖）",
            )
        return True

    def _persist_rounds(self) -> None:
        if self.task is not None:
            with self._save_lock:
                # 落盘前先看盘上有没有**别人写的**（一次 stat；真被改过才重读整份
                # 并并集）。两个写者都得这么做才成立：后台维护线程与下一轮的 agent
                # 同时写同一份 task.json，而最后写的通常是**下一轮**（它跑得久），
                # 只让维护那边重读挡不住覆盖（§86）。
                self._rebase_if_stale()
                self.task.rounds = self.rounds
                self.task.baseline_prompt_used = self._baseline_prompt_used
                self.task.save()

    def close_round(self) -> None:
        """闭合当前 Round（仅最终回答路径调用）；managed 模式做水位检查。

        水位口径 = 当前上下文窗口体量（本轮装配峰值 last_context_estimate）：
        达标且轮已闭合才批量整理，轮进行中永不打扰。

        **轮闭合是"产物生效点"**（2026-09-12 用户口径：把重组上下文与子 agent
        放在「分裂之后、下一轮对话之前」）——同步维护（run / serve）在这里已经
        把整理+分裂跑完，故随即 `_settle_after_maintenance()` 让产物立刻落地：
        注册表长出子 agent、历史轮补判归位、每个域的重组上下文可派生。于是
        **下一轮开场只剩对话这一件事**（不再开场建账）。异步维护（chat 后台
        线程）此刻还没有产物，由线程跑完时自己结算（同样是"没有开放轮就立刻
        生效"）。对用户静默。
        """
        if self.current_round is None:
            return
        self.current_round["end_state"] = "completed"
        # Block 结构化（机制一，零 LLM）：**按文件聚合**的确定性分块（v3，
        # segment_round_by_file），随轮次落盘——整理输入、文件地图、追溯
        # 导航都吃这份结构。
        #
        # 2026-09-11 收口（废双轨）：此前这里落 v1（以写/改为截止的粗分块），
        # 而整理产物/紧凑视图/expand_history 用的是 v3。两套分块器编号空间
        # 相同（R{n}-B{k}）但切法不同——实测全会话 45 个同 ID 块的覆盖区间
        # **零一致**，任何"按 ID 查表"的地方都会静默错位或丢描述（R1 纯读轮
        # 曾因此丢掉 23/24 条块描述）。落盘口径统一到 v3 后，编号空间唯一。
        # v1（segment_round）保留为兼容/离线检视用途，不再是落盘主线。
        self.current_round["blocks"] = blocks_module.segment_round_by_file(
            self.current_round
        )
        # 每轮一段话：**分裂成多 agent 之后**在轮闭合处结算——一轮一条、一轮多 agent
        # 就每段一条（§3.2/§6.1）；分裂前不在这里发，由水位处的攒批一次写完整批。
        try:
            self._settle_round_notes_of_round(self.current_round)
        except Exception as error:  # noqa: BLE001——结算绝不能拖垮轮闭合
            self._note_failed(
                self.current_round, f"结算异常：{type(error).__name__}: {str(error)[:120]}"
            )
        # 阶段锚点回填（§53）：块切分完成才能说清"这次阶段验收落在哪个块"。
        self._backfill_stage_anchors()
        self.current_round = None
        self._persist_rounds()
        if self.context_mode == MODE_MANAGED and self.task is not None:
            self._maybe_organize_batch()
            # 产物即时生效（§50）：同步维护已跑完 → 此刻（无开放轮）落地；
            # 异步维护还没产物 → 空转，由维护线程跑完时自行结算。
            self._settle_after_maintenance()
            # 换档线（V4 §6.2）：水位到了就把超龄且有 note 的轮一次换成段落。
            self._advance_fold_line()
        self._round_schemas = None          # 轮闭合：解冻（下一轮按新阶段重新冻结）

    def finalize_round(self, end_state: str = "open") -> None:
        """CLI 异常/中断路径：Round 保持开放（不闭合、不整理），仅持久化。

        中断/超限轮的成本照记（带"轮未闭合"标记）——失败尝试花的
        也是真金白银，而且正是上下文管理最该优化的对象。
        """
        if self.current_round is None:
            return
        self.current_round["end_state"] = "open"
        self._usage_record_and_drain(closed=False)
        self._persist_rounds()
        self.current_round = None

    def run(
        self,
        user_input: str,
        on_thinking: Optional[Callable[[str], None]] = None,
        on_answer_delta: Optional[Callable[[str], None]] = None,
    ) -> str:
        """处理一条用户输入（开启/续上 Round 并完成工作），返回最终回答。"""
        new_round = self._open_or_reuse_round(user_input)
        if new_round:
            # 新 Round 开启才让上一轮暂存的整理产物生效（精修索引/
            # Normalized/状态补丁）；续上开放轮则不动——本轮对话期间
            # 装配必须保持原样（连贯性 + 缓存前缀稳定）
            self._promote_org_results()
            # 顺序（2026-09-12 用户口径）：产物生效后**先**给维护窗口内到达
            # 的轮补判归属（渐近归属——那几轮到达时还没有域树，只能判给主
            # agent），**再**判本轮，本轮才能粘到补判结果上。补判是纯函数、
            # 零 LLM、不改消息字节，只归位不重做活。
            self._settle_and_route(user_input)
        # 轮次与会话绑定（rounds 的 seq 随会话持久化）——进程内计数会在
        # 退出重开后归零，长会话的"第 N 轮"就错了（实测教训）
        self.turn_count = self.current_round["seq"]
        self._record_event("user", {"role": "user", "content": user_input})
        if self.task is not None:
            self.task.record("user_input", user_input)
            self._deliver_user_input(user_input)
            # **变了才注入**文件地图（用户口径）：在用户消息之后、干活之前，
            # 追加一条 runtime_note 进历史；没变化就什么都不做。
            self._inject_file_map_if_changed()
            self._persist_rounds()
        return self._work_loop(on_thinking, on_answer_delta)

    def _deliver_user_input(self, user_input: str) -> None:
        """用户插话**像群聊**（2026-09-12 用户口径）：默认广播给正在干活的，
        `@X` 则只给被 @ 的（其他人不受影响、继续干自己的）。

        收件人 = 本轮的参与者（`round["participants"]` 里还排着队的 + 当前接手方）；
        `@` 匹配注册表的 id 或名称（多个 @ 就是多个收件人）。用户的消息本身
        已经记在轮里（`_record_event` 之前那步），这里只做**投递**——写进收件箱
        （装配时以 [传话] 出现）与公开线程 `task.chat`。
        """
        assert self.task is not None
        import re as _re

        text = str(user_input or "")
        mentions = _re.findall(r"@([0-9A-Za-z_\-\u4e00-\u9fff]+)", text)
        targets: list[str] = []
        for token in mentions:
            ent = self._registry_entry_for(token)
            if ent is not None:
                name = str(ent.get("name") or ent.get("id"))
                if name not in targets:
                    targets.append(name)
        r = self.current_round or {}
        if not targets:
            # 没 @ → 广播给本轮参与者（含还排着队的）
            cur = str(r.get("active_view") or "") or views_module.MAIN_AGENT_ID
            targets = [cur] + [str(p.get("agent") or "")
                               for p in (r.get("participants") or [])]
            targets = [t for t in dict.fromkeys(targets) if t and t != cur]
            if not targets:
                return                    # 只有当前接手方：走正常路径，不用投递
        for name in targets:
            ent = self._registry_entry_for(name)
            if ent is None:
                continue
            ent.setdefault("inbox", []).append({
                "from": "用户（插话）",
                "message": text.strip()[:2000],
            })
            self.task.chat.append({
                "round": r.get("seq"), "from": "用户", "to": name,
                "text": text.strip()[:2000], "kind": "user",
            })
        self.task.save()

    def resume(
        self,
        on_thinking: Optional[Callable[[str], None]] = None,
        on_answer_delta: Optional[Callable[[str], None]] = None,
    ) -> str:
        """续上最近一个开放 Round（\\继续 命令）：不注入任何新的用户消息。

        步数超限 / Ctrl+C 中断后，轮保持开放但没有新信息——再发一句
        "继续"只会往历史里塞一条噪音用户消息。本方法重建协议消息后
        直接进工作循环，装配与轮内上下文原样继续。没有开放轮时报错。
        """
        last = self.rounds[-1] if self.rounds else None
        if last is None or last.get("end_state") not in ("", "open"):
            raise RuntimeError("没有可继续的开放轮次")
        self.current_round = last
        # 协议消息从事件的 Full 中重建（它们就是事实来源）
        self.messages = [e["message"] for e in last["events"]]
        self.turn_count = last["seq"]
        return self._work_loop(on_thinking, on_answer_delta)

    def _work_loop(
        self,
        on_thinking: Optional[Callable[[str], None]] = None,
        on_answer_delta: Optional[Callable[[str], None]] = None,
    ) -> str:
        """单轮的工具调用主循环：run 与 resume 共用。

        **账本按"轮"不按"段"**（2026-09-13 修，实测缺陷）：`last_stats` 原先每次
        进入本方法就清零，于是同一轮的**后续段**（步数超限后的 `\\c` 续跑、会合交棒
        后的第二段）会把前一段的按调用方分账**连同它的 token 一起丢掉**——
        现场会话 `20260913-151842-2628dd` 的 R7：轮总 `steps=43`，分账只有
        `[web 网络工具… steps=25]`，且该行 `prompt=2,104,250` **恰好等于**那一段
        的分账额，说明那 18 步的钱在轮账里整个消失（汇总与分账同时少钱）。
        现在：**同一轮再次进入就接着累**；清零只发生在"新的一轮"或"刚落过账"
        （`_usage_record_and_drain`）——后者保证行与行不重叠。
        """
        seq = (self.current_round or {}).get("seq")
        if getattr(self, "_stats_round", None) != seq:
            self.last_stats = self._fresh_stats()
            self._stats_drained = None      # 新轮的增量基线归零
            self._stats_round = seq
        # 空响应护栏：流被端点/代理掐断时只有思考没有正文，连续空响应计数
        empty_streak = 0
        # 回调暂存：跨 agent 通信工具（consult）流式展示时借用同一管线，
        # 让子 agent 的输出直接进用户窗口（不打回主 agent 再路由）
        self._stream_cbs = {"thinking": on_thinking, "answer": on_answer_delta}

        # 步数按轮累计（2026-09-09 用户拍板：同一轮被打断后 \c 续跑要
        # 续上——预算属于轮而不属于段；记在 round 上随持久化，进程重启
        # 后也续。里程碑轮开新轮 = 新预算）
        steps_used = (self.current_round or {}).get("steps_used", 0)
        self.last_stats["llm_calls"] = steps_used  # 展示口径同步续上
        while steps_used < self.max_turns:
            steps_used += 1
            if self.current_round is not None:
                self.current_round["steps_used"] = steps_used
            # 步归**执行它的那个 agent**（2026-09-12 用户口径）：一次模型调用
            # = 一步，在同一轮里可以有多家（主 agent 走路由那一步算它的，
            # 接手方走的算接手方的）。轮的总步 = 各执行方之和。
            self.last_stats["by_agent"].setdefault(
                self._active_view(),
                {"steps": 0, "calls": 0, "prompt": 0, "cached": 0, "miss": 0,
                 "completion": 0},
            )["steps"] += 1
            if self.cancel_check is not None and self.cancel_check():
                raise KeyboardInterrupt   # ⏹ 与 Ctrl+C 同语义：轮保持开放
            if self.on_progress:
                self.on_progress("等待模型响应…")
            messages = self._assemble_messages()
            try:
                content, ordered, _usage = self._stream_call(
                    messages,
                    tools=self._stage_schemas() or None,
                    purpose="working",
                    on_thinking=on_thinking,
                    on_answer_delta=on_answer_delta,
                    on_progress=self.on_progress,
                )
            except LLMStreamError as error:
                # 服务端流中途报错：与"流被掐断"同一失败家族——都是没有
                # 正文的异常终止，并入空响应护栏自动重试。真实错误文本
                # （含 request id）落 history，finish_reason 记 stream_error
                if self.task is not None:
                    self.task.record("empty_stream", f"stream_error: {error}")
                content, ordered, _usage = "", [], None

            if ordered:
                empty_streak = 0
                ev = self._record_event(
                    "tool_call",
                    {
                        "role": "assistant",
                        "content": content,
                        "tool_calls": [
                            {
                                "id": tc["id"],
                                "type": "function",
                                "function": {
                                    "name": tc["name"],
                                    "arguments": tc["arguments"] or "{}",
                                },
                            }
                            for tc in ordered
                        ],
                    },
                    thinking=self._last_thinking,
                )
                if self.task is not None:
                    # 工具卡即时上屏：调用已发出即落盘，结果回来再补——
                    # 网页端不必等工具跑完（几分钟的命令）才看到卡片
                    self._persist_rounds()
                self.last_stats["tool_calls"] += len(ordered)
                self._run_tool_batch(ordered)
                # 回合内转交（route_to）：工具批次跑完才换视图——批次执行
                # 期间改装配会让同批后续工具看到错乱的上下文。换完继续
                # 循环，下一步就是新视图自己装配、自己动手。
                self._apply_pending_route()
                continue

            # 模型不再请求工具 → 产出最终回答 → Round 闭合。
            answer = content
            if not answer.strip():
                # 流被端点/代理中途掐断：思考流完了，正文与 usage 都没到
                # （2026-09-08 实测：活跃思考流 13 分钟后被干净掐断，空串
                # 曾被当成 final_answer 闭合轮次）。空串不是最终回答。
                # length = 输出上限（重试必再撞，直接上报）；其余按瞬时
                # 断流自动重试，仍空则轮保持开放交回调用方。
                empty_streak += 1
                fr = self._last_finish_reason or "未返回"
                if self.task is not None:
                    self.task.record("empty_stream", f"finish_reason={fr}")
                # 中断通知记为持久轮事件（runtime-reminder 信封，每轮只记
                # 一次）：重试与 \c 续跑时模型都知道此前响应失败过，重试
                # 不再是盲目的从头再想——思考无法回传（协议 400），能给的
                # 只有 steering：压缩思考、直接迈出第一个工作项
                if empty_streak == 1:
                    self._record_event(
                        "runtime_note",
                        _runtime_reminder(
                            "上一次响应在生成中途被异常终止，未产出任何正文"
                            "（长思考流被服务端掐断）。请大幅压缩思考，不要"
                            "重做完整规划，直接迈出第一个工作项（一次工具调用），"
                            "分多步边做边验证。"
                        ),
                    )
                if self._last_finish_reason == "length" or empty_streak > 2:
                    self._persist_rounds()
                    raise RuntimeError(
                        f"流式响应异常结束（finish_reason={fr}，正文为空）。"
                        f"Round 保持开放，\\c 可直接接着干"
                    )
                if self.on_progress:
                    self.on_progress("响应为空（流被中断），自动重试…")
                continue
            ev_ans = self._record_event(
                "final_answer", {"role": "assistant", "content": answer},
                thinking=self._last_thinking)
            if self.task is not None:
                self.task.record("final_answer", answer)
            # **会合**（2026-09-12 用户口径）：本轮还有排队的参与者 →
            # 我这一份干完了，但**不收轮**，把接力棒交给下一个（串行交棒）。
            # 我的回答落进对齐线程，接手方据此知道"上一手干了什么"；全部
            # 参与者干完才闭合，用户会分别看到各自的回答（不汇总）。
            if self._relay_to_next_participant(answer):
                continue
            self.close_round()
            if self.task is not None:
                self._usage_record_and_drain(closed=True)
                self._persist_rounds()
            return answer

        # 步数超限：Round 保持开放（失败尝试并入本轮，不产生整理成本），
        # 由调用方决定后续（重试/人工介入）。
        self._persist_rounds()
        raise RuntimeError(
            f"本轮已累计工作 {steps_used} 步（达到上限 {self.max_turns}）仍未给出"
            f"最终回答（Round 保持开放，\\c 续跑会接着这个步数计数）"
        )

    def label_blocks(self, rounds: Optional[list[dict]] = None) -> dict:
        """机制二（上下文分化运行时）：低频批量语义标注——零截断的块摘要
        输入 + 一次 LLM 调用，产出每块路由式摘要与大类归类。

        与水位整理的关系：机制二发生在整理时（同一口锅），本方法是其
        可独立试跑的形态（scripts/label_blocks.py 离线检查用）。
        返回 {"categories": [...], "labels": {block_id: {...}}}，不落盘。
        """
        rounds = rounds if rounds is not None else self.rounds
        digests = []
        for r in rounds:
            for b in r.get("blocks") or []:
                digests.append(blocks_module.block_digest(r, b))
        if not digests:
            return {"categories": [], "labels": {}}
        prompt = (
            "你是工作块标注器。以下是一个长会话里全部工作块的摘要（按时间序）。\n"
            "职责（最后只输出一个 JSON 对象，不要代码块围栏）：\n"
            '1. "categories"：把本质同域的块归成少数几个大类（通常 3-8 个，'
            '如"页面搭建/布局主题/多会话功能/测试建设"），每个为 '
            '{"id": "A", "name": "大类名", "description": "一句话范围说明"}；\n'
            '2. "blocks"：每个块一条 {"id": "块id", "category": "大类id", '
            '"summary": "路由式一句话"}——summary 写明动作与对象'
            "（如 write_file → css/style.css（14KB 初版）、edit_file → "
            "js/app.js:448（+720B）、run_command → node tests/run.js（5 套件通过）），"
            "不要复制内容。块id 必须原样使用输入里给出的 id。\n"
            "分类只分大类，宁少勿多；相邻块同域是常态。\n\n"
            + "\n\n".join(digests)
        )
        state: Optional[dict] = None
        for _ in range(2):  # 解析失败重试一次
            content, _ordered, _usage = self._stream_call(
                [{"role": "user", "content": prompt}],
                tools=None,
                purpose="organization",
                extra_body={"thinking": {"type": "disabled"}},
            )
            state = self._parse_state_json(content)
            if isinstance(state, dict) and state.get("blocks"):
                break
        if not isinstance(state, dict):
            return {"categories": [], "labels": {}}
        labels = {
            item["id"]: item
            for item in state.get("blocks") or []
            if isinstance(item, dict) and item.get("id")
        }
        categories = [c for c in state.get("categories") or [] if isinstance(c, dict)]
        return {"categories": categories, "labels": labels}

    def _invoke_tool(self, name: str, arguments: str) -> str:
        """解析参数并执行工具，返回结果文本（不含展示与落盘）。"""
        try:
            parsed = json.loads(arguments or "{}")
        except json.JSONDecodeError as error:
            return f"工具参数不是合法 JSON: {error}"
        # 模型偶发把 emoji 拆成不成对 \uD83D 转义：解析合法但无法
        # 编码落盘——进工具与进历史前一律清洗（实测崩溃教训）
        parsed = _sanitize_json_strings(parsed)
        fn = self.tools.get(name)
        if fn is None:
            return f"未知工具: {name}，可用工具: {list(self.tools)}"
        # 观察记录按 agent 分开（§3.7）：工具层读 `safety.current_agent()` 当键的一段。
        tools_module.safety.bind_agent(self._active_view())
        # 用户钩子（zcode-borrowings.md 1.4）：前置可拦截（理由回传模型），
        # 后置可附反馈——扩展者的规则与观测不进 Wovra 代码
        blocked = tools_module.run_pre_hook(name, parsed)
        if blocked:
            return blocked
        # 看图预算（2026-09-16，GAIA §2）：到硬上限就不再执行——不注入新图、
        # 不产生新开销，让模型就地收口，而不是靠超时把整轮掐死
        if name in _VISION_TOOLS and self._image_view_count() >= _IMAGE_VIEW_HARD:
            return image_budget_refusal(self._image_view_count(), _IMAGE_VIEW_HARD)
        try:
            # 文件权限守卫**按调用作用域**生效（不是构造时一绑到底）：粘性绑定
            # 会在 Agent 收工后继续拦别人（脚本/CLI 直接调文件工具、同进程里
            # 换会话），也会让测试互相污染。作用域内绑定 = "谁在干活就按谁的
            # 权限"，出栈即还原。
            with tools_module.permissions.guard_scope(self._file_guard_object()), \
                    tools_module.abort.abort_scope(self.cancel_check):
                result = fn(**parsed)
        except TypeError as error:
            # 参数名/数量不对时**给出正确参数名**（TOOLING_REVIEW.md §4.4）：
            # 实测调用方按直觉写 search_files(path=…) 只拿到一句
            # "unexpected keyword argument 'path'"，还得自己去翻签名。
            return f"工具执行出错: {error!r}{_signature_hint(fn, parsed, error)}"
        except Exception as error:  # noqa: BLE001——错误回传给模型而不是中断循环
            return f"工具执行出错: {error!r}"
        if not isinstance(result, str):
            result = json.dumps(result, ensure_ascii=False, default=str)
        feedback = tools_module.run_post_hook(name, parsed, result)
        if feedback:
            result = f"{result}\n[hooks 反馈] {feedback}"
        result = sanitize_surrogates(result)
        if name in _VISION_TOOLS:               # 记账 + 软线收敛提示
            result = self._count_image_view(result)
        return result

    def _image_view_count(self) -> int:
        """本回合已看图次数（没有 round 时按 0 计——脚本/测试直调的场景）。"""
        return int((self.current_round or {}).get("image_views") or 0)

    def _count_image_view(self, result: str) -> str:
        """看图计数，并在**恰好**到软线时把"该收敛了"写进结果。

        只提示一次：到硬线还有一次拒绝（见 `_invoke_tool`），中间每次都贴
        会白烧 token。计数落在 round 上，随轮持久化。
        """
        count = self._image_view_count() + 1
        if self.current_round is not None:
            self.current_round["image_views"] = count
        if count == _IMAGE_VIEW_SOFT and count < _IMAGE_VIEW_HARD:
            return f"{result}\n{image_converge_note(count, _IMAGE_VIEW_HARD)}"
        return result

    def _execute(self, call_id: str, name: str, arguments: str) -> None:
        """执行单个工具调用，并把结果作为 tool 消息追加到当前 Round。"""
        if self.on_tool_call:
            self.on_tool_call(name, arguments)
        self._finish_tool_result(
            call_id, name, arguments, self._invoke_tool(name, arguments)
        )

    # 观察类工具（读过/写过谁）——用于"文件变更"通知（§3.7）。读记快照，写记快照 ＋ 写入日志。
    _READ_TOOLS = ("read_file", "search_files", "page_text")
    _WRITE_TOOLS = ("write_file", "edit_file", "replace_lines", "restore_file")

    def _observe_tool_effect(self, name: str, arguments: str, result: str) -> None:
        """把"这次工具让 agent 看到了/改了哪个文件"记进观察快照（零 LLM，§3.7）。

        轮内每个文件只记一次（`self._observed_this_round`）——同一步里连读三次不必存三份。
        """
        if name not in self._READ_TOOLS + self._WRITE_TOOLS:
            return
        if self.task is None or self.current_round is None:
            return
        text = str(result or "")
        if text.startswith("权限拒绝") or text.startswith("未知工具"):
            return
        try:
            parsed = json.loads(arguments or "{}")
        except json.JSONDecodeError:
            return
        rel = str(parsed.get("path") or parsed.get("directory") or "").strip()
        if not rel or parsed.get("directory"):
            return                      # 目录级搜索不进观察（它不构成"你手里那份内容"）
        rel = self._rel_path(rel)
        if not rel or rel.startswith(".wovra") or rel.startswith(".."):
            return
        key = (rel, name in self._WRITE_TOOLS)
        if key in self._observed_this_round:
            return
        self._observed_this_round.add(key)
        workspace = Path(tools_module.safety.workspace_root())
        target = workspace / rel
        try:
            content = target.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return
        view = self._active_view()
        if name in self._WRITE_TOOLS:
            observed_module.note_write(workspace, view, rel,
                                       int((self.current_round or {}).get("seq") or 0))
            observed_module.record(workspace, view, rel, content, by="edit")
        else:
            observed_module.record(workspace, view, rel, content, by="read")

    def _finish_tool_result(self, call_id: str, name: str,
                            arguments: str, result: str) -> None:
        if self.on_tool_result:
            self.on_tool_result(name, result)
        self._observe_tool_effect(name, arguments, result)

        event = self._record_event(
            "tool_result", {"role": "tool", "tool_call_id": call_id, "content": result},
            tool_name=name,
        )
        result_for_context = event["message"]["content"] if event.get("id") else result

        if self.task is not None:
            self.task.record("tool_call", f"{name}({arguments})")
            self.task.record("tool_result", f"{name} -> {result_for_context[:500]}")
            self._persist_rounds()

        # 补做被推迟的水位检查（2026-09-11 400 实测的收尾；§53 之后为防御性
        # 路径）：轮若在工具方法体内被闭合，当时快照协议不全，检查被置为
        # deferred；现在这条调用的 tool 结果已落盘、装配合法，正是补取的
        # 时机——水位口径与触发条件都不变，只是晚了半拍。
        if self._maint_deferred:
            self._maint_deferred = False
            self._maybe_organize_batch()

    def _run_tool_batch(self, ordered: list[dict]) -> None:
        """执行一批工具调用。

        纯只读批次（互不依赖）并发执行、按序记录——独立读取串行只是
        白等；含变更类调用时保持顺序执行（写与写之间存在顺序依赖，
        并行写同一文件是竞态）。"""
        self._tools_running = True
        try:
            self._run_tool_batch_inner(ordered)
        finally:
            self._tools_running = False

    def _run_tool_batch_inner(self, ordered: list[dict]) -> None:
        """（_run_tool_batch 的实体，见其 docstring。）"""
        if len(ordered) > 1 and all(tc["name"] in _READ_ONLY_TOOLS for tc in ordered):
            if self.on_tool_call:
                for tc in ordered:
                    self.on_tool_call(tc["name"], tc["arguments"])
            with ThreadPoolExecutor(max_workers=min(4, len(ordered))) as pool:
                results = list(pool.map(
                    lambda tc: self._invoke_tool(tc["name"], tc["arguments"]), ordered))
            for tc, result in zip(ordered, results):
                self._finish_tool_result(tc["id"], tc["name"], tc["arguments"], result)
            return
        for tc in ordered:
            self._execute(tc["id"], tc["name"], tc["arguments"])

    @staticmethod
    def _is_image_rejection(error: BaseException) -> bool:
        """服务端是不是在抱怨图片（400）——**文案常写成"格式不支持"**。

        实测（2026-09-15，会话 20260915-155432-b13727）：一张合法的 1440×9000
        PNG 被回 "unsupported image ... formats: webp, png, jpeg, and gif"，
        真实原因是**每边超过 8192px**（8192 接受、8193 拒绝）。别被文案骗了去
        查格式——超尺寸才是主因。
        """
        text = str(error)
        return ("unsupported image" in text
                or ("image" in text.lower() and "invalid_request_error" in text))

    @staticmethod
    def _strip_images(messages: list[dict]) -> Optional[list[dict]]:
        """把带图的消息换成纯文本说明（图片被拒时降级重发用）；本来没图返回 None。

        不原地改（`messages` 就是轮的消息，改了会污染后续装配）。
        """
        found = False
        out: list[dict] = []
        for message in messages:
            content = message.get("content")
            if isinstance(content, list) and any(
                    isinstance(p, dict) and p.get("type") == "image_url"
                    for p in content):
                found = True
                kept = [p for p in content
                        if not (isinstance(p, dict) and p.get("type") == "image_url")]
                kept.append({"type": "text", "text": (
                    "（本消息里的图片被服务端拒绝，已跳过——你现在**看不到**它，"
                    "不要描述其内容；要看请用更小的尺寸重截或分段截图后再 view_image）"
                )})
                out.append({**message, "content": kept})
            else:
                out.append(message)
        return out if found else None

    def _stream_request(self, messages: list[dict], tools,
                        extra_body: Optional[dict] = None):
        """发起一次流式请求；**图片被服务端拒**时去掉图片重发一次。

        为什么必须有这层（2026-09-15 用户报"续跑也没用"）：图片是在装配尾部
        每步重建注入的，所以只要轮里有一张服务端不收的图，**这一轮之后的每一次
        请求都会 400**，续跑也永远失败——一次截图就能把整轮永久锁死。装配层
        已按尺寸拦（`eyes.load_image_part`），这里是兜底：无论图从哪来（别的
        生产者、别家端点、更严的上限），都退化为"这轮不要图"继续干活。
        """
        try:
            return self.llm.chat(messages, tools=tools, stream=True,
                                 extra_body=extra_body)
        except Exception as error:
            stripped = (self._strip_images(messages)
                        if self._is_image_rejection(error) else None)
            if stripped is None:
                raise
            # 记成**能力事实**（2026-09-15 用户口径："我不能确保每个模型都支持
            # 视觉"）：一次被拒就说明这个端点/模型不吃图。此后装配不再注入图片
            # （`_eye_image_message` 查 `vlm_ready()`），也不必每步都白试一次
            # 400 —— 多模态从"必经之路"降级为"有就用、没有就换文本路"。
            self._vision_ok = False
            self._emit_status("该模型不支持图像输入——已跳过图片继续本轮（改用文本路）")
            if self.task is not None:
                self.task.record(
                    "image_skipped",
                    f"服务端拒绝图片，已去图重发并关闭本轮图像注入：{str(error)[:160]}",
                )
            return self.llm.chat(stripped, tools=tools, stream=True)

    def _stream_call(
        self,
        messages: list[dict],
        tools: Optional[list[dict]] = None,
        purpose: str = "working",
        extra_body: Optional[dict] = None,
        on_thinking: Optional[Callable[[str], None]] = None,
        on_answer_delta: Optional[Callable[[str], None]] = None,
        on_progress: Optional[Callable[[str], None]] = None,
    ) -> tuple[str, list[dict], Any]:
        """发一次流式补全，聚合分片，返回 (内容, 工具调用列表, usage)。

        purpose ∈ working / organization / compaction：成本按用途分账，
        实验才能回答"管理机制自身贵不贵"。
        """
        # "步数"只统计干活的步（agent 循环）；整理/压缩是维护开销，
        # 在用途分账与"整理 X tok"里单独体现，不混进步数
        if purpose == "working":
            self.last_stats["llm_calls"] += 1
        # **端点限流退避重试**（2026-09-15）：维护调用动辄 20 万 tok 的输入，
        # 与干活轮并发时端点会回"System protection triggered by request burst /
        # rate limit"（实测 2026-09-09 一批整理整批死在这上面）——一次失败就
        # 让整批白算。这里按 15s/30s/60s 退避重试（连接期错误，无副作用；
        # 流中途的错误仍走上层空响应护栏，不在这里重试）。
        #
        # 计时**按尝试**（2026-09-15 用户报"TTFT 虚大"）：`attempt_start` 放在
        # 每次尝试内部，退避的 sleep 不再计进 ttft/dur——此前 start 在循环之外，
        # 一次限流重试就把首字延迟抬高 15s（两次 45s），均值随之虚高。等待时间
        # 不藏着：记进账本行（`retry=n wait=Ns`）与轮耗时。
        delay = 15.0
        retries = 0
        waited = 0.0
        for _throttle_try in range(4):
            attempt_start = time.monotonic()
            try:
                try:
                    stream = self._stream_request(messages, tools, extra_body)
                except Exception:
                    if not extra_body:
                        raise
                    # 部分端点不支持 extra_body 里的参数（如关闭思考的 thinking 开关），
                    # 降级为不带该参数重发——宁可让整理调用多思考，也不能直接失败。
                    # 提示每次会话只发一次，避免每轮刷屏
                    if not self._degrade_warned:
                        self._degrade_warned = True
                        self._emit_status(
                            "后台整理：当前模型不支持整理用的参数，已自动降级重试（本次会话仅提示一次）"
                        )
                    stream = self._stream_request(messages, tools)
                break
            except Exception as _error:
                if _throttle_try >= 3 or not _is_throttle_error(_error):
                    raise
                self._emit_status(
                    f"端点限流（{str(_error)[:60]}）——{int(delay)}s 后自动重试"
                )
                time.sleep(delay)
                waited += delay
                retries += 1
                delay *= 2
        content_parts: list[str] = []
        thinking_parts: list[str] = []
        self._last_thinking = ""   # 流错误中途抛出时不得残留上一步的思考
        tool_calls_acc: dict[int, dict] = {}
        usage = None
        first_token_at: Optional[float] = None
        finish_reason: Optional[str] = None
        chunk_iter = iter(stream)
        while True:
            try:
                chunk = next(chunk_iter)
            except StopIteration:
                break
            except _llm_APIError as error:
                # 服务端在流中途报错（实测：internal error 打断思考流，
                # openai.APIError 不是 RuntimeError，穿透 CLI 直接打崩进程）。
                # 只捕获迭代器自己抛的错——回调/清洗里的 bug 不能被伪装成
                # 流错误触发重试。转 LLMStreamError：主循环并入空响应护栏，
                # 重试耗尽后 CLI 按"轮保持开放"收尾。
                self._last_finish_reason = "stream_error"
                raise LLMStreamError(str(error)) from error
            if getattr(chunk, "usage", None):
                usage = chunk.usage
            if not getattr(chunk, "choices", None):
                continue
            choice = chunk.choices[0]
            if getattr(choice, "finish_reason", None):
                finish_reason = choice.finish_reason
            delta = choice.delta
            if delta is None:
                continue
            if self.cancel_check is not None and self.cancel_check():
                raise KeyboardInterrupt   # 流中也可终止（长思考不必等完）

            thinking = reasoning_of(delta)
            if thinking:
                thinking = sanitize_surrogates(thinking)
                thinking_parts.append(thinking)
                if on_thinking:
                    on_thinking(thinking)

            if delta.content:
                text = sanitize_surrogates(delta.content)
                content_parts.append(text)
                if on_answer_delta:
                    on_answer_delta(text)

            for fragment in delta.tool_calls or []:
                index = fragment.index or 0
                if index not in tool_calls_acc:
                    tool_calls_acc[index] = {"id": "", "name": "", "arguments": ""}
                acc = tool_calls_acc[index]
                if fragment.id:
                    acc["id"] = fragment.id
                if fragment.function and fragment.function.name:
                    acc["name"] = fragment.function.name
                    # 名字一分片到达就提示"正在<动作>…"——用户要的是
                    # 等待时刻的即时反馈，而不是等参数全部输完
                    if on_progress:
                        on_progress(f"正在{_action_word(acc['name'])}…")
                if fragment.function and fragment.function.arguments:
                    acc["arguments"] += fragment.function.arguments

            if first_token_at is None and (
                thinking or delta.content or (delta.tool_calls or [])
            ):
                # 首 token 延迟（TTFT）：prefill + 排队时间，基座的第二张
                # 面孔（延迟乘数）靠它测量——与总耗时分开记
                first_token_at = time.monotonic()

        elapsed = time.monotonic() - attempt_start
        # 首字延迟：**成功那次尝试**的"发请求 → 第一个分片"。口径澄清
        # （2026-09-15 实测，用户问"是不是把思考时间算进去了"）：不是。
        # 思考是**流式**到达的（实测首个分片就是思考分片 1.5s，首个正文
        # 分片 2.7s），所以 ttft = prefill + 排队，不含思考时长；限流退避的
        # sleep 也不再算进来（见上）。
        has_first = first_token_at is not None
        ttft = (first_token_at - attempt_start) if has_first else 0.0
        self._last_finish_reason = finish_reason
        self._last_thinking = "".join(thinking_parts)
        if usage is not None:
            self._accumulate_usage(usage, purpose)
            # 逐调用用量落账（2026-09-09 缓存法医的产物）：usage 行按轮
            # 聚合，逐调用粒度缺失正是 D/E 归因要做事件重建的原因——
            # 从今往后每次调用自带 prompt/cached/miss/ttft 对账数据，
            # provider 上报的可信度可直接用 TTFT 交叉验证
            if self.task is not None:
                cached, _miss = cached_tokens_of(usage)
                extra = ""
                if retries:
                    extra += f" retry={retries} wait={waited:.1f}s"
                if not has_first:
                    # 一个分片都没收到（空响应/被护栏切断）：**没有首字**，
                    # 不是"首字延迟 = 全程耗时"。标出来，否则它会以一个大值
                    # 混进 TTFT 均值（用户报的"虚大"来源之一）。
                    extra += " nofirst=1"
                self.task.record(
                    "llm_call",
                    f"[{purpose}] prompt={usage.prompt_tokens or 0:,} "
                    f"cached={cached:,} miss={(usage.prompt_tokens or 0) - cached:,} "
                    f"completion={usage.completion_tokens or 0:,} "
                    f"ttft={ttft:.1f}s dur={elapsed:.1f}s "
                    f"finish={self._last_finish_reason or '未返回'}{extra}",
                )
        if purpose in _MAINTENANCE_PURPOSES:
            # 维护性开销异步执行、可能跨越轮次边界，混进 last_stats 会
            # 漏记（会话结束丢失）或错记进下一轮（实测教训）
            with self._maint_lock:
                self._maint_usage[purpose]["seconds"] += elapsed + waited
        else:
            # 耗时按**墙上时间**算（含退避等待）：那段时间用户确实在等；
            # 首字延迟只取成功尝试（不是同一件事，别混）。
            self.last_stats["seconds"] += elapsed + waited
            if has_first:      # 没收到首字的调用不是延迟样本，不进均值/峰值
                self.last_stats["ttft_seconds"] += ttft
                self.last_stats["ttft_max"] = max(self.last_stats["ttft_max"], ttft)
            self.last_stats["purpose"].setdefault(
                purpose, {"prompt": 0, "completion": 0, "total": 0, "seconds": 0.0}
            )["seconds"] += elapsed
        ordered = [tool_calls_acc[i] for i in sorted(tool_calls_acc)]
        return "".join(content_parts), ordered, usage

    def drain_maintenance_usage(self) -> dict:
        """取走并清零维护账本（整理/压缩的累计用量），线程安全。

        快照交给调用方记账；同时存入 last_maint 供展示层（usage_line）。
        """
        with self._maint_lock:
            snapshot = {k: dict(v) for k, v in self._maint_usage.items()}
            for v in self._maint_usage.values():
                for key in v:
                    v[key] = 0
        self.last_maint = snapshot
        return snapshot

    def _usage_record_and_drain(self, closed: bool) -> None:
        """把本轮成本写入任务历史并清空维护账本。

        开放轮（超限/中断）同样记账——失败尝试的成本恰恰是上下文
        管理最该优化的对象，不能因为轮没闭合就在账本上隐身。
        """
        if self.task is None:
            return
        if self.context_mode == MODE_BASELINE:
            self._baseline_accounting()
        maint = self.drain_maintenance_usage()
        stats = self.last_stats
        # **落账写"增量"**（2026-09-13）：`last_stats` 是**轮内累计**（`cli/render.py`
        # 在轮收尾后读它显示本轮花费，故不许清零），而同一轮可能落多次账（中断一次、
        # 续跑收尾再一次）——所以这里减去上次落账时的快照，行与行不重叠；
        # `round_usage_map` 按轮把多行相加即得整轮。一轮只落一次账时快照为空，
        # 等价于原先的全量写法。
        prev = self._stats_drained or {}

        def grew(key: str) -> int:
            return int(stats.get(key, 0) or 0) - int(prev.get(key, 0) or 0)

        def grew_purpose(name: str) -> int:
            cur = ((stats.get("purpose") or {}).get(name) or {}).get("total", 0)
            old = ((prev.get("purpose") or {}).get(name) or {}).get("total", 0)
            return int(cur or 0) - int(old or 0)

        prompt = grew("prompt_tokens")
        cached = grew("cached_tokens")
        miss = grew("cache_miss_tokens")
        cache_info = ""
        if prompt:
            cache_info = (
                f" 缓存命中 {cached:,} tok（{cached / prompt:.1%}）"
                f" 未命中 {miss:,} tok（{miss / prompt:.1%}）"
                f" 等效输入 {miss + cached / tokens.CACHE_RATE:,.0f} tok"
            )
        suffix = "" if closed else "（轮未闭合：超限/中断，成本照记）"
        ttft_info = ""
        if stats.get("ttft_max"):
            ttft_info = f" ttft={stats['ttft_seconds']:.1f}s（峰值 {stats['ttft_max']:.1f}s）"
        self.task.record(
            "usage",
            f"[{self.context_mode}] round={self._usage_round_seq()} "
            f"steps={grew('llm_calls'):,} "
            f"context={self.last_context_estimate:,} "
            f"working={grew_purpose('working'):,} "
            f"org={maint['organization']['total']:,} "
            f"compaction={maint['compaction']['total']:,} "
            f"split={maint['split']['total']:,} "
            f"note={maint['note']['total']:,} "
            f"prompt={prompt:,} completion={grew('completion_tokens'):,} "
            f"total={grew('total_tokens'):,}（思考 {grew('reasoning_tokens'):,}）"
            f"{cache_info}{ttft_info}{suffix}"
            + self._by_agent_segment(prev),
        )
        # 记下"已经写出去到哪儿了"：`last_stats` 本身**不清零**（它是轮内累计视图）。
        self._stats_drained = copy.deepcopy(stats)

    def _usage_round_seq(self) -> int:
        """本条落账行属于哪一轮（**写进行里**，消费方按号归属）。

        为什么要写：此前靠"按 `steps_used` 逐轮吞行"的事后推断，一旦某一轮的
        `steps_used` 与实际步数不符（旧检查点轮实测记 13 步、实际 1 个事件），
        整条链就错位一格——末轮直接分不到账（实测 `20260912-181611-886f42`
        的 R13 因此不显示成本）。轮号落进行里，归属就不再依赖推断。

        `close_round` 之后 `current_round` 已是 None，故退回 `turn_count`
        （它在开轮时就被设成本轮序号）。
        """
        seq = (self.current_round or {}).get("seq")
        if seq:
            return int(seq)
        return int(self.turn_count or 0)

    def _by_agent_segment(self, prev: Optional[dict] = None) -> str:
        """落账行尾的**按调用方分账**段（零 LLM；这一段谁都没花就不加尾巴）。

        形态：` by=[Main steps=12 prompt=1000 cached=900 miss=100 completion=50] [A …]`
        ——每个执行方一段，段内键值固定（数字不带千分位，便于机械解析）。
        轮的总消费 = 各段之和（用户口径：A 300K + B 400K = 700K）。

        `prev` = **上次落账时的快照**：只写增量，行与行不重叠（`round_usage_map`
        按轮把多行相加）。一轮只落一次账时 `prev` 为空、等价于全量。
        """
        buckets = (self.last_stats or {}).get("by_agent") or {}
        # `prev` 是整份 stats 快照 → 分账要从它的 `by_agent` 里取（踩过：直接
        # `prev.get(name)` 拿到的是 None，于是每行都写全量、行与行重复计）
        prev_by = (prev.get("by_agent") if isinstance(prev, dict) else None) or {}
        keys = ("steps", "prompt", "cached", "miss", "completion")
        parts: list[str] = []
        for name, b in buckets.items():
            p = prev_by.get(name) or {}
            d = {k: int(b.get(k, 0) or 0) - int(p.get(k, 0) or 0) for k in keys}
            if not any(d.values()):
                continue                 # 这一段它没花钱：不写空段
            parts.append(
                f"[{name} steps={d['steps']} prompt={d['prompt']}"
                f" cached={d['cached']} miss={d['miss']}"
                f" completion={d['completion']}]"
            )
        return (" by=" + " ".join(parts)) if parts else ""

    def _accumulate_usage(self, usage, purpose: str) -> None:
        """把一次调用的 usage 记进账本。

        working 记入本轮 last_stats（最终回答即记账）；整理/压缩是
        维护性开销且可能异步跨越轮次边界，记入独立维护账本，由
        _usage_record_and_drain 在记账时统一取走，不与干活的成本混账。
        """
        details = getattr(usage, "completion_tokens_details", None)
        reasoning_tokens = getattr(details, "reasoning_tokens", None)
        cached, _miss = cached_tokens_of(usage)

        if purpose in _MAINTENANCE_PURPOSES:
            with self._maint_lock:
                bucket = self._maint_usage[purpose]
                bucket["prompt"] += usage.prompt_tokens or 0
                bucket["completion"] += usage.completion_tokens or 0
                bucket["total"] += usage.total_tokens or 0
            return

        self.last_stats["prompt_tokens"] += usage.prompt_tokens or 0
        self.last_stats["completion_tokens"] += usage.completion_tokens or 0
        self.last_stats["total_tokens"] += usage.total_tokens or 0
        bucket = self.last_stats["purpose"].setdefault(
            purpose, {"prompt": 0, "completion": 0, "total": 0, "seconds": 0.0}
        )
        bucket["prompt"] += usage.prompt_tokens or 0
        bucket["completion"] += usage.completion_tokens or 0
        bucket["total"] += usage.total_tokens or 0

        if reasoning_tokens:
            self.last_stats["reasoning_tokens"] += reasoning_tokens
        self.last_stats["cached_tokens"] += cached
        self.last_stats["cache_miss_tokens"] += max(
            0, (usage.prompt_tokens or 0) - cached
        )
        # 按调用方分账：谁在花，记谁（消费发生时即记，不做事后归属推断）
        bucket = self.last_stats["by_agent"].setdefault(
            self._active_view(),
            {"steps": 0, "calls": 0, "prompt": 0, "cached": 0, "miss": 0, "completion": 0},
        )
        bucket["calls"] += 1
        bucket["prompt"] += usage.prompt_tokens or 0
        bucket["cached"] += cached
        bucket["miss"] += max(0, (usage.prompt_tokens or 0) - cached)
        bucket["completion"] += usage.completion_tokens or 0
