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
    _NOTIFY_SCHEMA,
    _ORG_DOMAINS_SCHEMA,
    _ORG_SUBMIT_SCHEMA,
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
        """
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
        }
        self.rounds.append(self.current_round)
        self.messages = []
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
        达标且轮已闭合才批量整理，轮进行中永不打扰；整理产物暂存到
        下一轮开启才生效（_promote_org_results），对用户静默。
        """
        if self.current_round is None:
            return
        self.current_round["end_state"] = "completed"
        # Block 结构化（机制一，零 LLM）：以写/改为截止的确定性分块，
        # 随轮次落盘——整理输入、文件地图、追溯导航都吃这份结构
        self.current_round["blocks"] = blocks_module.segment_round(self.current_round)
        self.current_round = None
        self._persist_rounds()
        if self.context_mode == MODE_MANAGED and self.task is not None:
            self._maybe_organize_batch()

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
                self._record_event(
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
                self.last_stats["tool_calls"] += len(ordered)
                self._run_tool_batch(ordered)
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
            self._record_event("final_answer", {"role": "assistant", "content": answer})
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

    def _run_tool_batch(self, ordered: list[dict]) -> None:
        """执行一批工具调用。

        纯只读批次（互不依赖）并发执行、按序记录——独立读取串行只是
        白等；含变更类调用时保持顺序执行（写与写之间存在顺序依赖，
        并行写同一文件是竞态）。"""
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

            thinking = reasoning_of(delta)
            if thinking:
                thinking = sanitize_surrogates(thinking)
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
