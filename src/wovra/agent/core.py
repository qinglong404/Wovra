"""Agent 核心：构造/注册、Round 生命周期、run 循环、工具分发、用量记账。

工具方法（todo/notify/consult/submit 守卫）在 ledger.py；上下文装配在
assembly.py；水位维护管线在 maintenance.py——四者由 __init__.py 组装为
同一个 Agent 类（mixin）。方法体里的跨模块调用走 self，与拆分前一致。
"""
import json
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Optional
from .. import blocks as blocks_module
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
    _MAINTENANCE_PURPOSES,
    _ORG_COOLDOWN_ROUNDS_DEFAULT,
    _ORG_GRACE_ROUNDS_DEFAULT,
    _ORG_MAINT_TIMEOUT_DEFAULT,
    _ORG_WATERMARK_DEFAULT,
    _READ_ONLY_TOOLS,
    _action_word,
    _runtime_reminder,
    _sanitize_json_strings,
    _schema_of,
)
from .prompts import (
    _CONSULT_SCHEMA,
    _LIST_AGENTS_SCHEMA,
    _NOTIFY_SCHEMA,
    _ORG_DOMAINS_SCHEMA,
    _ORG_SUBMIT_SCHEMA,
    _ROUTE_TO_SCHEMA,
    _SWITCH_VIEW_SCHEMA,
    _TODO_SCHEMA,
)


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
        self.max_turns = max_turns or _DEFAULT_MAX_TURNS
        # 同步实时进度回调（主线程执行）：等待模型、工具动作的即时提示
        self.on_progress = on_progress
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
        # 维护快照的推迟标记（2026-09-11 实测缺陷的落点）：里程碑闭合发生
        # 在工具方法体**内部**（todo→verify_milestone→close_round），此刻
        # 调用方的 assistant(tool_calls) 还没有 tool 结果。若在此刻取装配
        # 快照，尾部就是"未被回复的 tool_calls"，紧接着追加的整理指令以
        # user 角色出现——严格端点直接 400（实测 21:27 那批 1 秒失败、
        # 未计费，靠下一批次补上）。置位后由 _finish_tool_result 在结果
        # 落盘后补做水位检查。
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

        if self.context_mode == MODE_MANAGED:
            self.register(self.expand_history)
            # 整理提交工具常驻：所有请求的 tools 数组恒定（工作对话与整理
            # 调用同序列化），前缀缓存才不会在 tools 区分叉。工作期误调用
            # 由方法体守卫拒绝（见 submit_organization / submit_domains）。
            self.register(self.submit_organization, schema=_ORG_SUBMIT_SCHEMA)
            self.register(self.submit_domains, schema=_ORG_DOMAINS_SCHEMA)
            # 大步/小步计划账本（工作工具，深度恒 1，见 todo-milestone-tool.md）
            self.register(self.todo, schema=_TODO_SCHEMA)
            # 跨 agent 通信（机制五）：单向 notify / 双向 consult
            self.register(self.notify, schema=_NOTIFY_SCHEMA)
            self.register(self.consult, schema=_CONSULT_SCHEMA)
            # 路由（Level 1 第三步）：拉职责表 + 显式转交
            # （2026-09-12 用户拍板「可以加工具」——隔离后这是唯一的
            # 跨 agent 公共信息面与纠错通道）
            self.register(self.list_agents, schema=_LIST_AGENTS_SCHEMA)
            self.register(self.switch_view, schema=_SWITCH_VIEW_SCHEMA)
            # 回合内转交（2026-09-12 用户拍板）：主 agent 的**本职动作**——
            # 收到消息、对职责表、把用户原话转给对应 agent，由它在本回合内
            # 直接接续干活（不需要再回主 agent 转述）。只在 managed 下注册：
            # baseline 装配不看视图，转交没有落点。
            self.register(self.route_to, schema=_ROUTE_TO_SCHEMA)

    def _bind_globals(self) -> None:
        """把进程级全局绑定对准本会话（审计记录器、后台任务归属）。

        在 Agent 构造时对准本会话；后台任务按会话归属治理。
        """
        tools_module.set_audit_recorder(
            lambda detail: self.task.record("file_change", detail) if self.task else None
        )
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
            # 显式转交的原始意志（switch_view / notify 写明的那次）——补判时
            # 复用，使"转交"不会因为中间插了一次 promote 而丢失。
            "route_explicit": self._take_pending_view(),
        }
        self.rounds.append(self.current_round)
        self.messages = []
        # 暂判一次（产物若恰好在此刻生效，由调用方在 promote 之后用
        # `_settle_and_route` 重判——先归位、再干活；此刻的判是纯函数，
        # 覆盖代价为零，且让"没有待生效产物"的常规路径一次到位）
        self._route_view(user_input)
        return True

    def _take_pending_view(self) -> str:
        """取走并清空显式转交意志（一次性；放在轮上以便补判复用）。"""
        if self.task is None:
            return ""
        pending = str(getattr(self.task, "pending_view", "") or "")
        if pending:
            self.task.pending_view = ""
        return pending

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
        塞进主 agent 的运行时信封（[路由建议]）供它参考。唯一例外是**显式
        转交**（`switch_view` / `notify` 写下的意志）——那是上一轮已经做出的
        决定（"以后这摊都交给 X"），直接生效，不走主 agent 绕一圈。

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
        explicit = str(target.get("route_explicit") or "") or self._take_pending_view()
        entry = (
            routing_module.resolve_agent(self.task.registry, explicit)
            if explicit else None
        )
        if entry is not None:
            view = str(entry.get("name") or entry.get("id"))
            target["active_view"] = view
            target["route_hint"] = {}
            if record and view != views_module.MAIN_AGENT_ID:
                self.task.record(
                    "route",
                    f"R{target.get('seq')} → {view}"
                    f"（显式转交 {entry.get('id')}）",
                )
            return view
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
        entry = self._registry_entry_for(view)
        if entry is None:
            return
        size = int(size or 0)
        entry["ctx_cur"] = size
        if size > int(entry.get("ctx_peak") or 0):
            entry["ctx_peak"] = size
        # 窗口每次都校正（无条件写）：老会话的 registry 里存过水位 100K，
        # 语义纠正后要随活动自然迁移到真实窗口，不能被旧值占住
        entry["window"] = self._agent_window()

    def _account_agent_activity(self) -> None:
        """轮闭合时的 per-agent 记账（3a）：轮次 + 步数落到**最终接手方**。

        口径：一轮算一个轮次、该轮全部步数，都记给轮闭合时 `active_view` 的
        那个 agent（真正答话的那个）；主 agent 的参与度另有 `handoffs`
        （它转出过多少轮）——两本账分开，避免既当轮次又当转出重复计数。
        """
        if self.task is None or self.current_round is None:
            return
        r = self.current_round
        view = str(r.get("active_view") or "") or views_module.MAIN_AGENT_ID
        entry = self._registry_entry_for(view)
        if entry is None:
            return
        entry["rounds"] = int(entry.get("rounds") or 0) + 1
        entry["steps"] = int(entry.get("steps") or 0) + int(r.get("steps_used") or 0)
        # 窗口无条件校正（同 _touch_view_context：老值可能是误存的水位）
        entry["window"] = self._agent_window()

    def _route_hint_lines(self) -> list[str]:
        """主 agent 起手时的**路由建议**（规则层给的起点，不是命令）。"""
        hint = (self.current_round or {}).get("route_hint") or {}
        view = str(hint.get("view") or "")
        if not view or view == views_module.MAIN_AGENT_ID:
            return []
        reason = str(hint.get("reason") or "")
        return [
            "[路由建议]（规则层按文件域/粘滞给的起点，**不是命令**）",
            f"这一轮按规则更像 {view} 的活"
            + (f"（{reason}）" if reason else "")
            + "。你若同意就用 route_to 把**用户原话**转给它（它本回合内直接"
            "接手回话）；不同意就自己干——判断权在你，规则只是起点，转错了"
            "对方会自己转出去。",
        ]

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
        # per-agent 参与度账（3a）：转出记给**转出方**（通常是主 agent）——
        # 与"轮次归最终接手方"分开，两本账各说各的。
        source = self._registry_entry_for(before)
        if source is not None:
            source["handoffs"] = int(source.get("handoffs") or 0) + 1
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

    def _record_event(self, type: str, message: dict, tool_name: str = "") -> dict:  # noqa: A002
        """把一条协议消息登记为 Event（生成 ID 与 Truncated 索引行）。"""
        if self.current_round is None:
            self.messages.append(message)
            return {"id": "", "message": message}
        seq = len(self.current_round["events"]) + 1
        event_id = f"R{self.current_round['seq']}-E{seq:02d}"
        event = truncate.make_event(event_id, type, message, tool_name=tool_name)
        self.current_round["events"].append(event)
        self.messages.append(event["message"])
        return event

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

    def _persist_rounds(self) -> None:
        if self.task is not None:
            with self._save_lock:
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
        self._account_agent_activity()   # 3a：轮次/步数落到最终接手方
        self.current_round = None
        self._persist_rounds()
        if self.context_mode == MODE_MANAGED and self.task is not None:
            self._maybe_organize_batch()
            # 产物即时生效（§50）：同步维护已跑完 → 此刻（无开放轮）落地；
            # 异步维护还没产物 → 空转，由维护线程跑完时自行结算。
            self._settle_after_maintenance()

    def finalize_round(self, end_state: str = "open") -> None:
        """CLI 异常/中断路径：Round 保持开放（不闭合、不整理），仅持久化。

        中断/超限轮的成本照记（带"轮未闭合"标记）——失败尝试花的
        也是真金白银，而且正是上下文管理最该优化的对象。
        """
        if self.current_round is None:
            return
        self.current_round["end_state"] = "open"
        self._usage_record_and_drain(closed=False)
        self._account_agent_activity()   # 3a：中断轮照记（花的钱是真的）
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
            self._persist_rounds()
        return self._work_loop(on_thinking, on_answer_delta)

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
        """单轮的工具调用主循环：run 与 resume 共用。"""
        self.last_stats = self._fresh_stats()
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
            if self.cancel_check is not None and self.cancel_check():
                raise KeyboardInterrupt   # ⏹ 与 Ctrl+C 同语义：轮保持开放
            if self.on_progress:
                self.on_progress("等待模型响应…")
            messages = self._assemble_messages()
            try:
                content, ordered, _usage = self._stream_call(
                    messages,
                    tools=self._schemas or None,
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
                )
                if self._last_thinking:
                    # 思考过程随事件落盘（零截断口径）；只进 event 不进
                    # message——装配读 message，上下文内容不受影响
                    ev["thinking"] = self._last_thinking
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
                # 只有 steering：压缩思考、直接迈第一小步
                if empty_streak == 1:
                    self._record_event(
                        "runtime_note",
                        _runtime_reminder(
                            "上一次响应在生成中途被异常终止，未产出任何正文"
                            "（长思考流被服务端掐断）。请大幅压缩思考，不要"
                            "重做完整规划，直接迈出第一小步（一次工具调用），"
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
                "final_answer", {"role": "assistant", "content": answer})
            if self._last_thinking:
                ev_ans["thinking"] = self._last_thinking
            if self.task is not None:
                self.task.record("final_answer", answer)
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
        # 用户钩子（zcode-borrowings.md 1.4）：前置可拦截（理由回传模型），
        # 后置可附反馈——扩展者的规则与观测不进 Wovra 代码
        blocked = tools_module.run_pre_hook(name, parsed)
        if blocked:
            return blocked
        try:
            result = fn(**parsed)
        except Exception as error:  # noqa: BLE001——错误回传给模型而不是中断循环
            return f"工具执行出错: {error!r}"
        if not isinstance(result, str):
            result = json.dumps(result, ensure_ascii=False, default=str)
        feedback = tools_module.run_post_hook(name, parsed, result)
        if feedback:
            result = f"{result}\n[hooks 反馈] {feedback}"
        return sanitize_surrogates(result)

    def _execute(self, call_id: str, name: str, arguments: str) -> None:
        """执行单个工具调用，并把结果作为 tool 消息追加到当前 Round。"""
        if self.on_tool_call:
            self.on_tool_call(name, arguments)
        self._finish_tool_result(
            call_id, name, arguments, self._invoke_tool(name, arguments)
        )

    def _finish_tool_result(self, call_id: str, name: str,
                            arguments: str, result: str) -> None:
        if self.on_tool_result:
            self.on_tool_result(name, result)

        event = self._record_event(
            "tool_result", {"role": "tool", "tool_call_id": call_id, "content": result},
            tool_name=name,
        )
        result_for_context = event["message"]["content"] if event.get("id") else result

        if self.task is not None:
            self.task.record("tool_call", f"{name}({arguments})")
            self.task.record("tool_result", f"{name} -> {result_for_context[:500]}")
            self._persist_rounds()

        # 补做被推迟的水位检查（2026-09-11 400 实测的收尾）：工具方法体内
        # 闭合轮时（todo→verify_milestone→close_round）快照协议不全，检查
        # 被置为 deferred；现在这条调用的 tool 结果已落盘、装配合法，正是
        # 补取的时机——水位口径与触发条件都不变，只是晚了半拍。
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
        start = time.monotonic()
        try:
            stream = self.llm.chat(messages, tools=tools, stream=True, extra_body=extra_body)
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
            stream = self.llm.chat(messages, tools=tools, stream=True)
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

        elapsed = time.monotonic() - start
        ttft = (first_token_at - start) if first_token_at is not None else elapsed
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
                self.task.record(
                    "llm_call",
                    f"[{purpose}] prompt={usage.prompt_tokens or 0:,} "
                    f"cached={cached:,} miss={(usage.prompt_tokens or 0) - cached:,} "
                    f"completion={usage.completion_tokens or 0:,} "
                    f"ttft={ttft:.1f}s dur={elapsed:.1f}s finish={self._last_finish_reason or '未返回'}",
                )
        if purpose in _MAINTENANCE_PURPOSES:
            # 维护性开销异步执行、可能跨越轮次边界，混进 last_stats 会
            # 漏记（会话结束丢失）或错记进下一轮（实测教训）
            with self._maint_lock:
                self._maint_usage[purpose]["seconds"] += elapsed
        else:
            self.last_stats["seconds"] += elapsed
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
        prompt = stats["prompt_tokens"]
        cached = stats["cached_tokens"]
        miss = stats["cache_miss_tokens"]
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
            f"[{self.context_mode}] steps={stats['llm_calls']:,} "
            f"context={self.last_context_estimate:,} "
            f"working={stats['purpose']['working']['total']:,} "
            f"org={maint['organization']['total']:,} "
            f"compaction={maint['compaction']['total']:,} "
            f"prompt={prompt:,} completion={stats['completion_tokens']:,} "
            f"total={stats['total_tokens']:,}（思考 {stats['reasoning_tokens']:,}）"
            f"{cache_info}{ttft_info}{suffix}",
        )

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
