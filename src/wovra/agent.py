"""Agent 运行时 + V2 Context Runtime。

设计文档：docs/context-runtime-v2.md（定稿）。核心内容：

* Round/Event：Round 只在 AI 产出最终回答时闭合（开放轮会合并
  中断/无回复期间的多条用户输入，跨会话持久化）；Event 的 message
  原样保存（执行期不截断内容），Truncated 是零 LLM 成本的一行索引
  （供降档索引与整理输入）
* Organization（水位批量整理）：轮闭合不再逐轮整理——实测轻轮的整理
  费可以比干活成本还高。水位按**当前上下文窗口体量**（最近一次装配
  的估算，轮闭合时即本轮峰值）计量：达标且轮闭合才触发一次批量整理
  （调用次数 N→1 摊薄、State Patch 跨轮去重），后台线程执行不阻塞
  对话；产物先暂存，**下一轮开启时才生效**——本轮装配纹丝不动
  （连贯性 + 缓存前缀稳定），过程对用户静默；
  输入 = 用户输入们 + 事件截断索引 + 最终回答全文；
  输出 = Normalized 用户意图 + 精修事件索引 + Task State 补丁
* Context Assembly：近 K 轮全量（Recent Full-Resolution Window），
  更早轮次按预算三档自动降档；按变化频率排序（越易变越靠后），
  保护前缀缓存
* baseline 对照组：全量追加 + 80% × 窗口阈值压缩（市面惯例）
"""

import inspect
import json
import os
import queue
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Optional

from . import task as task_module
from . import tokens
from . import tools as tools_module
from . import truncate
from .llm import LLM, reasoning_of
from .task import Task, sanitize_surrogates
from .tools import (
    ask_user,
    check_background,
    edit_file,
    get_current_time,
    glob_files,
    list_background,
    list_files,
    read_file,
    run_background,
    run_command,
    search_files,
    stop_background,
    web_fetch,
    web_search,
    write_file,
)

MODE_MANAGED = "managed"
MODE_BASELINE = "baseline"

# 上下文预算：全部按窗口百分比设计（亿级使用规模），绝对值可覆盖
_DEFAULT_CONTEXT_LIMIT = int(os.environ.get("WOVRA_CONTEXT_LIMIT", "1000000"))
_DEFAULT_HISTORY_BUDGET_RATIO = float(os.environ.get("WOVRA_HISTORY_BUDGET_RATIO", "0.3"))
_DEFAULT_MAX_RECENT_ROUNDS = int(os.environ.get("WOVRA_MAX_RECENT_ROUNDS", "3"))
_COMPRESS_THRESHOLD = float(os.environ.get("WOVRA_COMPRESS_THRESHOLD", "0.8"))

# 维护性用途：异步执行、可能跨越轮次边界，成本单独记账（不混入 last_stats）
_MAINTENANCE_PURPOSES = ("organization", "compaction")

# 纯只读工具：互不依赖，可在同一批工具调用里并发执行、按序记录
# （ask_user 会阻塞等用户输入，不参与并行）
_READ_ONLY_TOOLS = frozenset(
    {"read_file", "search_files", "list_files", "get_current_time",
     "glob_files", "web_fetch", "web_search", "list_background",
     "check_subtask"}
)

# 组织树最多 _ORG_MAX_DEPTH 层节点（根为第 1 层）：根 → 子 → 孙。
# 防失控生长的安全栏，不是机制
_ORG_MAX_DEPTH = 3


def _org_enabled() -> bool:
    """组织层开关：WOVRA_SOLO=1 时整层禁用（工具不注册、引导不注入）。

    存在的意义是实验对照——"单 agent"必须是运行时结构性保证（模型
    连工具都看不见，想拆也没有手），不能靠提示词恳求。
    """
    return os.environ.get("WOVRA_SOLO", "").strip().lower() not in ("1", "true", "yes")

_ORGANIZE_MAX_CALLS = 4
_ORGANIZE_MAX_READS = 3
# 水位批量整理：水位口径 = **当前上下文窗口体量**（最近一次装配的估算，
# 轮闭合时即本轮峰值）——2026-09-07 用户拍板，替代 V3 初版的"未整理
# 积压量"口径。达标且轮闭合才触发一次批量整理，轮进行中永不打扰；
# 产物暂存、下一轮开启才生效。小会话可能全程不触发——整理成本归零。
_ORG_WATERMARK_DEFAULT = int(os.environ.get("WOVRA_ORG_WATERMARK", "100000"))
_ORG_BATCH_MAX_DEFAULT = int(os.environ.get("WOVRA_ORG_BATCH_MAX", "12"))
# 单轮工具循环的默认步数上限：真实任务步数轻松上两位数，10 步远远不够
_DEFAULT_MAX_TURNS = int(os.environ.get("WOVRA_MAX_TURNS", "40"))

# 工具名 → 进度提示的动作词（"正在<动作>…"）
_ACTION_WORDS = {
    "write_file": "写入文件",
    "edit_file": "修改文件",
    "run_command": "运行命令",
    "read_file": "读取文件",
    "search_files": "搜索内容",
    "list_files": "查看目录",
    "get_current_time": "获取当前时间",
    "expand_history": "展开历史",
    "run_background": "后台启动命令",
    "check_background": "查看后台输出",
    "stop_background": "停止后台任务",
    "glob_files": "按模式找文件",
    "web_fetch": "抓取网页",
    "web_search": "网页搜索",
    "ask_user": "询问用户",
    "list_background": "列出后台任务",
    "spawn_subtask": "创建子任务",
    "dispatch_subtask": "派发子任务",
    "check_subtask": "查看子任务",
    "merge_subtask": "清算子任务",
}

# 相关性筛选在 V2 中不实现（预算充足时所有浓缩视图直接加载），
# 保留函数体注释占位：V3 方向见设计文档第 11 节

_JSON_TYPES = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
}


class Agent:
    """Agent 运行时（工具循环）+ V2 上下文生命周期管理。"""

    def __init__(
        self,
        llm: Optional[LLM] = None,
        system_prompt: str = "",
        tools: tuple = (),
        max_turns: Optional[int] = None,
        task: Optional[Task] = None,
        context_mode: str = MODE_MANAGED,
        context_limit: Optional[int] = None,
        history_budget: Optional[int] = None,
        max_recent_rounds: Optional[int] = None,
        async_organization: bool = False,
        org_watermark: Optional[int] = None,
        org_batch_max: Optional[int] = None,
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
        ratio = float(os.environ.get("WOVRA_HISTORY_BUDGET_RATIO", _DEFAULT_HISTORY_BUDGET_RATIO))
        self.history_budget = history_budget or int(self.context_limit * ratio)
        self.max_recent_rounds = max_recent_rounds or _DEFAULT_MAX_RECENT_ROUNDS
        # 整理是否异步执行：chat 模式开（不阻塞对话），run/测试用同步（确定性）
        self.async_organization = async_organization
        # V3 水位批量整理参数（水位 = 未整理轮原始内容体量阈值；批量上限
        # = 单次整理调用最多处理的轮数，防止整理提示词自身失控）
        self._org_watermark = (
            _ORG_WATERMARK_DEFAULT if org_watermark is None else org_watermark
        )
        self._org_batch_max = (
            _ORG_BATCH_MAX_DEFAULT if org_batch_max is None else org_batch_max
        )
        # 已入队/整理中的轮次 seq：命中率的计量口径里它们不算"未整理"，
        # 避免批量整理排队期间被下一次触发重复收编
        self._org_inflight: set[int] = set()
        # 子任务派发板：每轮刷新的机械状态行（进程/账本/升级计数），
        # 注入装配尾部——主 agent 每轮都"看得见"子任务进展
        self._subtask_board: list[str] = []
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
        }
        self._maint_lock = threading.Lock()
        self.last_maint: dict = {}
        # 最近一次装配的上下文体量估算（每步更新；轮结束时即该轮峰值）
        self.last_context_estimate = 0
        # 端点不支持整理参数的降级提示：每次会话只提示一次，避免每轮刷屏
        self._degrade_warned = False

        # baseline 记账：累计输入 token（触发阈值压缩）
        self._baseline_prompt_used = task.baseline_prompt_used if task else 0

        self.last_stats = self._fresh_stats()

        if self.context_mode == MODE_MANAGED:
            self.register(self.expand_history)
            # 水位批量整理：上次会话遗留的未整理轮（含崩溃时的 pending /
            # failed）由下一次轮闭合触发时一并收编——加载时不立即补跑
            # （小会话可能永远不需要整理）
        if self.task is not None and _org_enabled():
            # 组织运行时 V1（docs/organization-runtime-v1.md）：分形节点的
            # 组织工具——任何节点对上是子、对下是主，同一套行为规则
            self.register(self.spawn_subtask)
            self.register(self.dispatch_subtask)
            self.register(self.check_subtask)
            self.register(self.merge_subtask)
            self._refresh_board()

    def _bind_globals(self) -> None:
        """把进程级全局绑定对准本会话（审计记录器、后台任务归属）。

        子任务在同进程内运行会重绑这些全局；子任务返回后由 run_subtask
        调本方法把绑定交还父会话。
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

    def register(self, fn: Callable) -> None:
        """把一个 Python 函数注册为模型可调用的工具。"""
        if fn.__name__ in self.tools:
            raise ValueError(f"工具重复注册: {fn.__name__}")
        self.tools[fn.__name__] = fn
        self._schemas.append(_schema_of(fn))

    # ---- Round 生命周期：开放 → 闭合 ----------------------------------------

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

    # ---- 主循环 ---------------------------------------------------------------

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
        self.last_stats = self._fresh_stats()
        self._refresh_board()  # 派发板每轮刷新：子任务进展本轮可见
        self._record_event("user", {"role": "user", "content": user_input})
        if self.task is not None:
            self.task.record("user_input", user_input)
            self._persist_rounds()

        for _ in range(self.max_turns):
            if self.on_progress:
                self.on_progress("等待模型响应…")
            messages = self._assemble_messages()
            content, ordered, _usage = self._stream_call(
                messages,
                tools=self._schemas or None,
                purpose="working",
                on_thinking=on_thinking,
                on_answer_delta=on_answer_delta,
                on_progress=self.on_progress,
            )

            if ordered:
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
            f"本轮已连续工作 {self.max_turns} 步仍未给出最终回答（Round 保持开放，"
            f"继续对话即可接着干）"
        )

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
        try:
            result = fn(**parsed)
        except Exception as error:  # noqa: BLE001——错误回传给模型而不是中断循环
            return f"工具执行出错: {error!r}"
        if not isinstance(result, str):
            result = json.dumps(result, ensure_ascii=False, default=str)
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

    # ---- 流式调用（所有 LLM 交互的唯一通道，按用途分账） ------------------------

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
        for chunk in stream:
            if getattr(chunk, "usage", None):
                usage = chunk.usage
            if not getattr(chunk, "choices", None):
                continue
            delta = chunk.choices[0].delta
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
        if usage is not None:
            self._accumulate_usage(usage, purpose)
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
        prompt_details = getattr(usage, "prompt_tokens_details", None)
        cached = getattr(prompt_details, "cached_tokens", None) or 0

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

    # ---- Context Assembly（设计文档第 4 节） ------------------------------------

    def _assemble_messages(self) -> list[dict]:
        """装配本轮请求的上下文，并记录体量估算（终端展示与窗口保底同口径）。"""
        msgs = self._assemble_messages_impl()
        self.last_context_estimate = self._estimate_messages(msgs)
        return msgs

    def _assemble_messages_impl(self) -> list[dict]:
        """按变化频率排序装配上下文（缓存友好布局）。

        [1] system 人设（静态）
        [2] 历史轮次视图（少变：闭合时成形，之后不可变）
        [3] Task State + 降档轮次的一行索引 + 文件地图（每轮变——放尾部）
        [4] 当前 Round 事件（追加式全量，轮内赦免）
        """
        past = [r for r in self.rounds if r is not self.current_round]

        if self.context_mode == MODE_BASELINE:
            msgs: list[dict] = []
            persona = self.system_prompt
            if self.task is not None and self.task.baseline_summary:
                # 阈值压缩产生的历史摘要（市面惯例：摘要 + 最近几轮原文）
                persona = (persona + "\n\n[历史压缩摘要]\n" + self.task.baseline_summary).strip()
            if persona:
                msgs.append({"role": "system", "content": persona})
            for r in past:
                if r.get("compacted"):
                    continue  # 已并入压缩摘要
                msgs.extend(e["message"] for e in r["events"])
            msgs.extend(self._current_round_messages())
            return msgs

        # ---- managed：三档分层 ----
        recent = past[-self.max_recent_rounds:]
        recent_ids = {id(r) for r in recent}
        older = [r for r in past if id(r) not in recent_ids]  # 按时间正序

        budget = self.history_budget
        state_render = ""
        if self.task is not None:
            state_render = self.task.get_state().render(budget=budget // 3)
        used = tokens.estimate(state_render)

        # 更早轮次：从新到旧依次尝试档 1 → 档 2 → 档 3（最老的先降档）
        tiers: dict[int, int] = {}
        for r in reversed(older):
            cost1 = tokens.estimate(self._render_tier1(r))
            if used + cost1 <= budget:
                tiers[id(r)] = 1
                used += cost1
                continue
            cost2 = tokens.estimate(self._render_tier2(r))
            if used + cost2 <= budget:
                tiers[id(r)] = 2
                used += cost2
                continue
            tiers[id(r)] = 3
            used += tokens.estimate(self._render_tier3(r))

        msgs: list[dict] = []
        if self.system_prompt:
            msgs.append({"role": "system", "content": self.system_prompt})

        view_msgs: list[dict] = []
        tier3_lines: list[str] = []
        for r in past:
            if r in recent:
                view_msgs.extend(e["message"] for e in r["events"])
                continue
            tier = tiers.get(id(r), 3)
            if tier == 1:
                view_msgs.append({"role": "user", "content": r["user_input"]["original"]})
                view_msgs.append({"role": "assistant", "content": self._render_tier1(r)})
            elif tier == 2:
                view_msgs.append({"role": "user", "content": r["user_input"]["original"]})
                view_msgs.append({"role": "assistant", "content": self._render_tier2(r)})
            else:
                tier3_lines.append(self._render_tier3(r))

        block = []
        if state_render:
            block.append(state_render)
        if tier3_lines:
            block.append("[历史索引]（已降档轮次，可用 expand_history 展开）")
            block += tier3_lines
        file_map = self._file_map_lines(older)
        if file_map:
            block.append(
                "[历史涉及文件]（内容已随轮次降档，修改前先 read_file 获取现状，"
                "通读时按 num_lines=400 连续分段）"
            )
            block += file_map
        if self._subtask_board:
            block.append(
                "[子任务派发板]（后台进程执行中，非阻塞；dispatch_subtask 派发，"
                "check_subtask 查看账本与升级，完成用 merge_subtask 清算——"
                "未完成不要清算）"
            )
            block += self._subtask_board
        if block:
            view_msgs.append({"role": "user", "content": "\n\n".join(block)})

        msgs.extend(view_msgs)
        msgs.extend(self._current_round_messages())
        return msgs

    def _current_round_messages(self) -> list[dict]:
        """轮内赦免：当前 Round 事件全量进入上下文，不做任何内容截断。

        执行期截断曾两次被实测证明适得其反：折叠诱发"读 → 失忆 →
        重读"死循环（39 次 read_file 烧穿 40 步上限）；2000 字符
        安全截断把模型刚读到的文件内容挡在上下文外。唯一的例外是
        模型窗口本身：估算超过 context_limit 时，把最老的事件折叠
        为索引行直到回线——最后手段，正常任务永远碰不到。
        """
        msgs = list(self.messages)
        if self.current_round is None:
            return msgs
        events = self.current_round["events"]
        if len(events) != len(msgs):
            return msgs  # 结构对不上时不动手（宁超限，不坏数据）
        budget = int(self.context_limit * 0.9)  # 给最终回答留余量
        # 廉价预检：最坏 1 字 ≈ 1 tok（CJK），字符数不超预算必在窗内
        total_chars = sum(len(str(m.get("content") or "")) for m in msgs)
        if total_chars <= budget:
            return msgs
        sizes = [self._estimate_messages([m]) for m in msgs]
        total = sum(sizes)
        if total <= budget:
            return msgs
        # 每条索引行按 150 tok 保守计价（120 字符 CJK 的上界），宁多折不少折
        fold, kept = 0, total
        while fold < len(events) - 1 and kept + fold * 150 + 200 > budget:
            kept -= sizes[fold]
            fold += 1
        lines = [truncate.event_index_line(e) for e in events[:fold]]
        block = {"role": "user", "content": (
            f"[紧急折叠：当前轮上下文估算已超模型窗口（{self.context_limit:,} tok），"
            f"最老 {fold} 条事件折叠为索引；需要细节可用 expand_history 按事件 ID 展开]\n"
            + "\n".join(lines)
        )}
        return [block] + list(msgs[fold:])

    @staticmethod
    def _estimate_messages(msgs: list[dict]) -> int:
        """估算一组协议消息的 token 数（正文 + 工具调用参数）。"""
        total = 0
        for m in msgs:
            total += tokens.estimate(str(m.get("content") or ""))
            for call in m.get("tool_calls") or []:
                total += tokens.estimate(
                    (call.get("function") or {}).get("arguments") or ""
                )
        return total

    def _render_tier1(self, r: dict) -> str:
        """档 1：用户原文 + 意图 + 精修事件索引（预算内的高保真浓缩视图）。"""
        lines = [f"[R{r['seq']}] 用户：{r['user_input']['original']}"]
        if r["user_input"].get("normalized"):
            lines.append(f"意图：{r['user_input']['normalized']}")
        idx = self._round_index_lines(r)
        if idx:
            lines.append("事件索引：")
            lines += idx
        return "\n".join(lines)

    def _render_tier2(self, r: dict) -> str:
        """档 2：用户原文 + 意图（去掉事件索引）。"""
        lines = [f"[R{r['seq']}] 用户：{r['user_input']['original']}"]
        if r["user_input"].get("normalized"):
            lines.append(f"意图：{r['user_input']['normalized']}")
        return "\n".join(lines)

    def _render_tier3(self, r: dict) -> str:
        """档 3：一行话题行（所有降档轮次的集合即历史索引/话题表）。"""
        head = r["user_input"].get("normalized") or r["user_input"]["original"]
        state = "（已完成）" if r.get("end_state") == "completed" else "（进行中）"
        return f"[R{r['seq']}] {_head_text(head, 60)}{state}"

    def _round_index_lines(self, r: dict) -> list[str]:
        """事件的索引行：优先用精修索引，未整理的事件用 Runtime 截断行。"""
        refined = r.get("refined_index") or {}
        out = []
        for e in r["events"]:
            line = refined.get(e["id"]) or e["truncated"]
            status = f"[{e['status']}] " if e.get("status") else ""
            out.append(f"[{e['id']}] {status}{line}")
        return out

    def _file_map_lines(self, rounds: list[dict]) -> list[str]:
        """把降档历史里出现过的文件整理成一张"文件地图"。

        文件内容随轮次降档后，模型曾经只能盲目分片重爬（实测一个
        轮里 39 次 read_file 重读同一文件）。地图只给"哪些文件、
        在哪些轮被写过/读过"，指引精准定位，不携带内容成本。
        """
        touched: dict[str, dict[str, list[int]]] = {}
        for r in rounds:
            for e in r.get("events", []):
                if e["type"] != "tool_call":
                    continue
                for tc in e["message"].get("tool_calls", []) or []:
                    fn = tc.get("function", {})
                    if fn.get("name") not in ("write_file", "edit_file", "read_file"):
                        continue
                    try:
                        path = json.loads(fn.get("arguments") or "{}").get("path")
                    except ValueError:
                        continue
                    if not path:
                        continue
                    info = touched.setdefault(path, {"w": [], "r": []})
                    if fn["name"] == "read_file":
                        info["r"].append(r["seq"])
                    else:
                        info["w"].append(r["seq"])
        lines = []
        for path, info in touched.items():
            parts = []
            if info["w"]:
                wrote = "R" + ",R".join(dict.fromkeys(map(str, info["w"])))
                parts.append(f"写于 {wrote}")
            if info["r"]:
                read = "R" + ",R".join(dict.fromkeys(map(str, info["r"])))
                parts.append(f"读于 {read}")
            lines.append(f"- {path}（{'；'.join(parts)}）")
        return lines

    @staticmethod
    def _head_text(text: str, limit: int) -> str:
        text = " ".join((text or "").split())
        return text if len(text) <= limit else text[:limit] + "…"

    # ---- History Maintenance Pipeline（水位触发的批量整理） -------------------

    def _unorganized_rounds(self) -> list[dict]:
        """已闭合且尚未整理完成的轮次（按时间正序）。

        org_state ∈ ""（从未整理）/ "pending"（排队或上次崩溃遗留）/
        "failed"（上次解析失败）都算未整理；整理中（inflight）的不算，
        防止批量排队期间被下一次触发重复收编。
        """
        return [
            r for r in self.rounds
            if r.get("end_state") == "completed"
            and r.get("org_state") != "done"
            and r["seq"] not in self._org_inflight
        ]

    def _maybe_organize_batch(self) -> None:
        """水位检查：当前上下文窗口体量达阈值且本轮已闭合时，批量入队整理。

        水位口径 = last_context_estimate（最近一次装配的估算，轮闭合时
        即本轮峰值）——不是未整理积压量（2026-09-07 用户拍板）。每次轮
        闭合至多触发一批（上限 _org_batch_max 轮，最老的先整理）；剩余
        未整理轮留给下次闭合继续消化。产物暂存不直写：本轮装配保持
        原样，下一轮开启才生效（_promote_org_results）；过程对用户静默。
        """
        if self.last_context_estimate < self._org_watermark:
            return
        unorganized = self._unorganized_rounds()
        if not unorganized:
            return
        batch = unorganized[: self._org_batch_max]
        for r in batch:
            r["org_state"] = "pending"
            self._org_inflight.add(r["seq"])
        if self.async_organization:
            self._org_queue.put(batch)
            self._ensure_worker()
        else:
            # 同步模式（run 命令/测试）：立即整理，结果随轮次落盘
            try:
                self._organize_rounds(batch)
            finally:
                for r in batch:
                    self._org_inflight.discard(r["seq"])

    def organize_backlog(self) -> None:
        """立即整理全部未整理轮（分批同步）——run 模式进程收尾用。

        chat 模式不调用：水位设计允许小会话全程不整理（成本归零）；
        run 是一次性任务单元，退出前补整理，TaskState 才能跟得上
        下一次自主推进（"根据任务状态决定下一步"依赖这本账）。
        """
        if self.context_mode != MODE_MANAGED or self.task is None:
            return
        while True:
            unorganized = self._unorganized_rounds()
            if not unorganized:
                return
            batch = unorganized[: self._org_batch_max]
            for r in batch:
                r["org_state"] = "pending"
                self._org_inflight.add(r["seq"])
            try:
                ok = self._organize_rounds(batch)
            except Exception:  # noqa: BLE001——收尾整理失败不阻塞任务退出
                for r in batch:
                    r["org_state"] = "failed"
                self._persist_rounds()
                return
            finally:
                for r in batch:
                    self._org_inflight.discard(r["seq"])
            if not ok or len(batch) < self._org_batch_max:
                return

    def _ensure_worker(self) -> None:
        if self._org_thread is not None and self._org_thread.is_alive():
            return
        self._org_thread = threading.Thread(
            target=self._org_worker, name="wovra-organization", daemon=True
        )
        self._org_thread.start()

    def _org_worker(self) -> None:
        while True:
            batch = self._org_queue.get()
            try:
                self._organize_rounds(batch)
            except Exception:  # noqa: BLE001——整理失败不影响主对话
                for r in batch:
                    r["org_state"] = "failed"
                self._persist_rounds()
            finally:
                self._org_queue.task_done()
                for r in batch:
                    self._org_inflight.discard(r["seq"])

    def flush_organization(self, timeout: float = 10.0) -> bool:
        """等待异步整理队列清空（chat 退出限时等待；run 用同步模式无需调用）。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._org_queue.unfinished_tasks == 0:
                return True
            time.sleep(0.1)
        return self._org_queue.unfinished_tasks == 0

    def _organize_rounds(self, rounds: list[dict]) -> bool:
        """批量整理已闭合的 Round 们（维护管线的工作单元，V3 §4 机制）。

        一次调用处理一批（N 轮 N 次调用 → 1 次，摊薄固定开销）：跨轮
        重复交代的决策/背景在 State Patch 里天然去重。可展开性不变——
        事件 ID 与原始历史不受整理影响（§5：合并的是视图，不是历史）。
        输入 = 各轮用户输入 + 事件截断索引 + 最终回答全文（默认不读
        原文；截断行不足以确定关键事实时可用 read_full 按上限展开）。
        输出 = 各轮 Normalized 意图 + 精修事件索引 + 合并 State Patch，
        **全部写入 pending_org 暂存区**：本轮装配必须纹丝不动（连贯性
        + 缓存前缀稳定），下一轮开启时由 _promote_org_results 生效。
        过程对用户静默（无状态播报）。关闭思考（格式化任务）。解析
        失败重试一次，仍失败则整批保持 Runtime 视图（org_state=failed，
        回入水位等下次触发），原始层永远不受影响。返回是否成功。
        """
        sections = []
        for r in rounds:
            user_inputs = [
                e["message"].get("content", "")
                for e in r["events"] if e["type"] == "user"
            ]
            final_event = next(
                (e for e in reversed(r["events"]) if e["type"] == "final_answer"), None
            )
            final_text = (final_event["message"].get("content") or "") if final_event else ""
            part = (
                f"### Round {r['seq']}\n[用户输入]\n" + "\n---\n".join(user_inputs)
                + "\n[事件截断索引]\n" + truncate.render_round_events(r)
            )
            if final_text:
                part += f"\n[最终回答（完整）]\n{final_text[:2000]}"
            sections.append(part)

        prompt = (
            "你是任务整理器。以下是多个已完成 Round 的用户输入、事件截断索引"
            "与最终回答（按时间顺序排列）。\n"
            "你的职责（最后只输出一个 JSON 对象，不要代码块围栏）：\n"
            '1. "rounds"：数组，与输入的 Round 一一对应，每个元素为 '
            '{"seq": 轮次号, "normalized_user_input": "该轮用户意图的澄清表述'
            "——不是压缩，是把用户想要什么说得更清楚\", "
            '"refined_index": [{"id": "该轮的事件ID", "line": "一行摘要"}]}——'
            "索引行比截断行更短更准（保留结论：什么可行、什么实测不行、"
            "卡在哪），id 必须取自对应轮次事件流中已有的事件 ID，"
            "无实质内容的事件（如寒暄）可省略；\n"
            '2. "state_patch"：全部轮次合并后的任务状态增量补丁 '
            '{"completed":[],"decisions":[],"known_issues":[],"open_questions":[],'
            '"escalations":[],"experiments":[],'
            '"current_status":"...","goal":"...","is_done":bool}——'
            "escalations 是决策升级：实测与预期不符、影响方向、需要上级或"
            "人拍板的事项（写明预期、现实、选项），不要擅自改方向；"
            "experiments 是待办实验：机器无法自行验证、需要人当传感器的事项"
            "（写明做什么、看什么、什么算对）。"
            "多轮之间重复交代的决策与背景只记一次，已完成的事项不要重复累积。\n"
            f"若截断索引不足以确定关键事实（如失败的具体原因），"
            f"可用 read_full 工具查看事件原文（最多 {_ORGANIZE_MAX_READS} 次）。\n\n"
            + "\n\n".join(sections)
        )

        messages = [{"role": "user", "content": prompt}]
        content = ""
        reads_left = _ORGANIZE_MAX_READS
        for _ in range(_ORGANIZE_MAX_CALLS):
            content, ordered, _usage = self._stream_call(
                messages,
                tools=self._organize_schemas(),
                purpose="organization",
                extra_body={"thinking": {"type": "disabled"}},
            )
            if not ordered:
                break
            messages.append({
                "role": "assistant",
                "content": content,
                "tool_calls": [
                    {
                        "id": tc["id"],
                        "type": "function",
                        "function": {"name": tc["name"], "arguments": tc["arguments"] or "{}"},
                    }
                    for tc in ordered
                ],
            })
            for tc in ordered:
                if tc["name"] != "read_full" or reads_left <= 0:
                    result = "整理阶段不再展开更多原文。"
                else:
                    reads_left -= 1
                    try:
                        target = json.loads(tc["arguments"]).get("event_id", "")
                    except json.JSONDecodeError:
                        target = ""
                    result = self._read_full_event(target)
                messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result})

        state = self._parse_state_json(content)
        if state is None and content.strip():
            retry_content, _ordered, _usage = self._stream_call(
                messages
                + [
                    {"role": "assistant", "content": content[:2000]},
                    {"role": "user", "content": "你的输出不是合法 JSON。请重新输出，只包含 JSON 对象本身。"},
                ],
                purpose="organization",
                extra_body={"thinking": {"type": "disabled"}},
            )
            state = self._parse_state_json(retry_content)
        if not isinstance(state, dict):
            for r in rounds:
                r["org_state"] = "failed"
            self._persist_rounds()
            return False

        rounds_by_seq = {r["seq"]: r for r in rounds}
        for item in state.get("rounds") or []:
            if not isinstance(item, dict):
                continue
            r = rounds_by_seq.get(item.get("seq"))
            if r is None:
                continue
            # 暂存区：不直写正式字段——本轮对话期间的装配由这些字段
            # 组成，动它们就是"下一步替换"，会破坏连贯性和缓存前缀
            pending = r["pending_org"] = {}
            if item.get("normalized_user_input"):
                pending["normalized"] = str(item["normalized_user_input"])
            valid_ids = {e["id"] for e in r["events"]}
            for line_item in item.get("refined_index") or []:
                if (
                    isinstance(line_item, dict)
                    and line_item.get("id") in valid_ids
                    and line_item.get("line")
                ):
                    pending.setdefault("refined_index", {})[line_item["id"]] = str(
                        line_item["line"]
                    )
        patch = state.get("state_patch")
        if isinstance(patch, dict):
            if state.get("is_done") is not None and "is_done" not in patch:
                patch["is_done"] = bool(state["is_done"])
            # 批次级补丁挂在批内第一轮上：生效时只应用一次
            if rounds:
                rounds[0].setdefault("pending_org", {})["state_patch"] = patch
        for r in rounds:
            r["org_state"] = "done"
        self._persist_rounds()
        return True

    def _promote_org_results(self) -> None:
        """把暂存的整理产物落进正式视图（仅在**新 Round 开启时**调用）。

        本轮对话期间装配必须保持原样（连贯性 + 缓存前缀稳定），所以
        整理线程只把产物写进各轮的 pending_org 暂存区；直到下一轮
        开启，才替换精修索引/Normalized 意图、应用状态补丁并落盘。
        崩溃安全：pending_org 随 rounds 一起持久化，重启后第一次开
        新轮时补生效。
        """
        changed = False
        for r in self.rounds:
            pending = r.pop("pending_org", None)
            if not pending:
                continue
            changed = True
            if pending.get("normalized"):
                r["user_input"]["normalized"] = pending["normalized"]
            if pending.get("refined_index"):
                r.setdefault("refined_index", {}).update(pending["refined_index"])
            patch = pending.get("state_patch")
            if patch and self.task is not None:
                self.task.apply_state_patch(patch)
        if changed:
            self._persist_rounds()

    def _organize_schemas(self) -> list[dict]:
        """Organization 阶段唯一的工具：按事件 ID 读取原文（后门，默认不用）。"""

        def read_full(event_id: str) -> str:
            """按事件 ID（如 R1-E02）读取该事件的完整原文。"""
            return self._read_full_event(event_id)

        return [_schema_of(read_full)]

    def _read_full_event(self, event_id: str) -> str:
        """按事件 ID 返回完整原文，不做内容截断。

        旧会话数据可能带分离的 full 字段（旧版安全截断的产物），
        一并返回保证可读；新数据 message 即全文。
        """
        for r in self.rounds:
            for e in r["events"]:
                if e["id"] == event_id:
                    message = e["message"]
                    parts = [f"[{event_id}] {e['type']}"]
                    if message.get("tool_calls"):
                        parts.append(
                            "调用: "
                            + json.dumps(message["tool_calls"], ensure_ascii=False)
                        )
                    parts.append(message.get("content") or "")
                    if e.get("full"):
                        parts.append("[完整原文]\n" + e["full"])
                    return "\n".join(parts)
        return f"未找到事件: {event_id}"

    @staticmethod
    def _parse_state_json(text: str) -> Optional[dict]:
        """从整理输出里解析 JSON；容忍围栏与前后说明文字。"""
        raw = (text or "").strip()
        if not raw:
            return None
        if raw.startswith("```"):
            raw = raw.strip("`")
        start, end = raw.find("{"), raw.rfind("}")
        if start == -1 or end <= start:
            return None
        try:
            state = json.loads(raw[start:end + 1])
        except json.JSONDecodeError:
            return None
        return state if isinstance(state, dict) else None

    # ---- 组织运行时 V1（docs/organization-runtime-v1.md） -----------------------
    #
    # 分形节点：任何节点对上是子、对下是主。四条不变量在本节落地——
    # 原始需求逐字下传（org_context 只追加不转述）、目标下传事实上传
    # 方法不进传递（子任务状态账本就是报告）、信息不切开职责才切开
    # （所有权边界防写冲突）。细节设计见设计文档 §3。

    def _org_depth(self) -> int:
        """本节点在组织树里的深度（根为 0）。"""
        depth, task = 0, self.task
        while task is not None and task.parent_id and depth < _ORG_MAX_DEPTH + 1:
            try:
                task = Task.load(task.parent_id)
            except Exception:  # noqa: BLE001——父任务丢失按根处理
                break
            depth += 1
        return depth

    def _refresh_board(self) -> None:
        """刷新子任务派发板（轮开始与组织操作后调用，机械渲染零成本）。"""
        self._subtask_board = []
        if self.task is None or not _org_enabled():
            return
        for c in task_module.find_children(self.task.id):
            try:
                child = Task.load(c["id"])
            except Exception:  # noqa: BLE001——损坏的子任务不挡板
                continue
            state = child.get_state()
            running = tools_module.background_find(child.id) is not None
            self._subtask_board.append(
                f"- {child.id[-6:]} 进程={'运行中' if running else '未运行'} "
                f"状态={child.status} 轮次={len(child.rounds)} "
                f"升级={len(state.escalations)} 实验={len(state.experiments)} "
                f"| {c['goal'][:40]}"
            )

    def spawn_subtask(self, goal: str, requirements: str = "", ownership: str = "") -> str:
        """创建子任务并挂到本任务之下，返回子任务 id。

        goal：这个职责块要实现什么；requirements：本块的约束与要求
        （多行）；ownership：本块的文件/模块所有权边界（防止与其他
        子任务写冲突）。全局需求会逐字下传给子任务，不需要复述全局
        背景。创建后用 run_subtask 派发执行。
        """
        if self.task is None:
            return "本会话未绑定任务，无法创建子任务。"
        goal = (goal or "").strip()
        if not goal:
            return "goal 不能为空：子任务必须有自己的职责块目标。"
        # 本节点层数 = _org_depth()+1，子节点层数还要 +1——超层拒绝
        if self._org_depth() + 2 > _ORG_MAX_DEPTH:
            return (
                f"已达组织深度上限（{_ORG_MAX_DEPTH} 层）。请在本层完成工作，"
                "或先 merge_subtask 清算已完成的子任务。"
            )
        child = Task.create(
            goal=goal,
            requirements=[r.strip() for r in (requirements or "").splitlines() if r.strip()],
        )
        child.parent_id = self.task.id
        child.mode = self.context_mode
        child.workspace = self.task.workspace or str(tools_module.PROJECT_ROOT)
        # 意图快照逐字下传（不变量 1）：父目标原文 + 全局需求原文 +
        # 本层追加的拆解与所有权边界。不转述、不改写——追加不是复述
        snapshot = [f"[全局目标]\n{self.task.goal}"]
        if self.task.requirements:
            snapshot.append(
                "[全局需求]\n" + "\n".join(f"- {r}" for r in self.task.requirements)
            )
        state = self.task.get_state()
        if state.current_status:
            snapshot.append("[全局当前状态]\n" + state.current_status)
        if (ownership or "").strip():
            snapshot.append("[本块所有权边界]\n" + ownership.strip())
        child.org_context = "\n\n".join(snapshot)
        child.save()
        self.task.record("subtask", f"创建子任务 {child.id}：{goal}")
        self.task.save()
        self._refresh_board()
        return (
            f"子任务已创建：{child.id}\n"
            f"全局目标与需求已逐字下传。用 run_subtask 派发执行，"
            f"check_subtask 查看现状，merge_subtask 清算合并。"
        )

    def dispatch_subtask(self, task_id: str, instruction: str = "") -> str:
        """派发子任务到后台进程（非阻塞，立即返回）。

        子任务在独立进程里执行一轮（数分钟量级），进度写它自己的日志
        与状态账本。派发后不必等待：可继续派发其他职责块，或结束回合
        向用户汇报——每轮上下文里的[子任务派发板]会显示各块进展。
        instruction 可传用户拍板的决策（如"用户选了方案 A"）。
        """
        if self.task is None:
            return "本会话未绑定任务。"
        try:
            child = Task.load(task_id)
        except Exception as error:  # noqa: BLE001——加载失败原样报告
            return f"子任务加载失败：{error!r}"
        if child.parent_id != self.task.id:
            return f"{task_id} 不是本任务的子任务，拒绝派发。"
        if child.status == "merged":
            return "该子任务已清算，无需再派发。"
        if tools_module.background_find(child.id) is not None:
            return "该子任务已在后台运行中，勿重复派发。（check_subtask 查看账本）"
        if instruction.strip():
            child.pending_instruction = instruction.strip()
            child.save()
        bg_id = tools_module.start_background_argv(
            [sys.executable, "-m", "wovra", "run", child.id],
            label=f"子任务 {child.id}",
        )
        self.task.record("subtask", f"派发子任务 {child.id} → {bg_id}")
        self.task.save()
        self._refresh_board()
        return (
            f"子任务 {child.id} 已派发到后台（{bg_id}），非阻塞执行中。\n"
            f"进展见每轮的[子任务派发板]；账本用 check_subtask 查看；"
            f"实时日志用 \\bg {bg_id}。完成后用 merge_subtask 清算。"
        )

    def check_subtask(self, task_id: str, level: str = "summary") -> str:
        """查看子任务现状（不阻塞）：summary = 状态账本 + 进程状态；
        detail = 账本 + 各轮一行 + 最近一轮事件索引。"""
        if self.task is None:
            return "本会话未绑定任务。"
        try:
            child = Task.load(task_id)
        except Exception as error:  # noqa: BLE001
            return f"子任务加载失败：{error!r}"
        if child.parent_id != self.task.id:
            return f"{task_id} 不是本任务的子任务。"
        state = child.get_state()
        running = tools_module.background_find(child.id) is not None
        head = (
            f"[子任务 {child.id}] 状态：{child.status}"
            f"｜后台进程：{'运行中' if running else '未在运行'}"
        )
        if level == "detail":
            parts = [head]
            if state.render():
                parts.append(state.render())
            for r in child.rounds:
                head_text = r["user_input"].get("normalized") or r["user_input"]["original"]
                mark = "✓" if r.get("end_state") == "completed" else "…"
                parts.append(f"R{r['seq']}{mark} {' '.join(head_text.split())[:80]}")
            if child.rounds:
                parts.append(
                    "[最近一轮事件索引]\n"
                    + truncate.render_round_events(child.rounds[-1])
                )
            return "\n\n".join(parts)
        return head + "\n" + (state.render() or "（状态账本为空）")

    def merge_subtask(self, task_id: str) -> str:
        """清算子任务：把它的状态账本合并进本任务（解散≠删除，
        子任务的历史原样保留，可随时翻阅）。"""
        if self.task is None:
            return "本会话未绑定任务。"
        try:
            child = Task.load(task_id)
        except Exception as error:  # noqa: BLE001
            return f"子任务加载失败：{error!r}"
        if child.parent_id != self.task.id:
            return f"{task_id} 不是本任务的子任务，拒绝清算。"
        if child.status == "merged":
            return "该子任务已清算过。"
        state = child.get_state()
        patch = {
            name: list(getattr(state, name))
            for name in ("completed", "decisions", "known_issues",
                         "open_questions", "escalations", "experiments")
            if getattr(state, name)
        }
        self.task.apply_state_patch(patch)
        child.status = "merged"
        child.record("subtask", f"已被父任务 {self.task.id} 清算合并（状态账本并入，历史保留）")
        child.save()
        counts = "；".join(f"{k} {len(v)} 条" for k, v in patch.items()) or "无条目"
        self.task.record("subtask", f"清算子任务 {child.id}：{counts}")
        self.task.save()
        self._refresh_board()
        return f"子任务 {child.id} 已清算：{counts}。其历史原样保留，可继续查阅。"

    def _subtask_system_prompt(self, child: Task) -> str:
        """子任务 agent 的系统提示词：逐字下传的全局快照 + 职责块纪律。"""
        parts = [
            "你是 Wovra 子任务执行者：组织架构中负责一个职责块的节点。"
            "带全局视野，只做本块。",
        ]
        if child.org_context:
            parts.append(
                child.org_context
                + "\n（以上全局上下文逐字来自父任务，只读参考——"
                "它帮你理解全局，不许拿它当自己职责块之外的工作清单。）"
            )
        parts.append(f"[你的职责块]\n{child.goal}")
        if child.requirements:
            parts.append("[本块要求]\n" + "\n".join(f"- {r}" for r in child.requirements))
        parts.append(f"[工作区]\n{child.workspace or str(tools_module.PROJECT_ROOT)}")
        parts.append(
            "纪律：每一步的产物都要自证可用；实测与预期不符且影响方向时，"
            "不要擅自改方向——作为决策升级写进状态账本（escalations），"
            "由上级决定。需要人验证的事项写进 experiments（写明做什么、"
            "看什么、什么算对）。"
        )
        return "\n\n".join(parts)

    # ---- expand_history（设计文档第 12 节） --------------------------------------

    def expand_history(self, ids: list[str] | str, level: str = "full") -> str:
        """按需展开历史：Truncated → Summary（意图+索引）→ Full 三档读取。

        ids 可为轮（"R3"）或事件（"R3-E02"），容错逗号字符串与大小写；
        一次可传多个，无调用次数上限。展开只是临时把更高分辨率的信息
        读进当前上下文，不修改历史。
        """
        if isinstance(ids, str):
            ids = [s.strip() for s in ids.split(",") if s.strip()]
        level = (level or "full").strip().lower()
        if level not in ("truncated", "summary", "full"):
            return f"未知级别: {level}，可选 truncated / summary / full"
        results = []
        for rid in ids:
            if "-E" in rid:
                results.append(
                    self._read_full_event(rid) if level == "full" else self._event_summary(rid)
                )
            else:
                results.append(self._expand_round(rid, level))
        return "\n\n".join(results) or "未找到任何 ID"

    def _event_summary(self, event_id: str) -> str:
        for r in self.rounds:
            refined = r.get("refined_index") or {}
            for e in r["events"]:
                if e["id"] == event_id:
                    line = refined.get(event_id) or e["truncated"]
                    return f"[{event_id}] {line}"
        return f"未找到事件: {event_id}"

    def _expand_round(self, round_id: str, level: str) -> str:
        try:
            seq = int(round_id.lstrip("Rr"))
        except ValueError:
            return f"轮次 ID 无效: {round_id}"
        for r in self.rounds:
            if r["seq"] != seq:
                continue
            if level in ("summary", "truncated"):
                lines = [f"[R{seq}] 用户：{r['user_input']['original']}"]
                if r["user_input"].get("normalized"):
                    lines.append(f"意图：{r['user_input']['normalized']}")
                lines += self._round_index_lines(r)
                return "\n".join(lines)
            parts = [f"[R{seq}] 用户：{r['user_input']['original']}"]
            for e in r["events"]:
                if e["type"] == "user":
                    continue
                parts.append(
                    f"--- {e['id']} ({e['type']}) ---\n"
                    + (e.get("full") or e["message"].get("content") or "")
                )
            return "\n".join(parts)
        return f"未找到轮次: {round_id}"

    # ---- baseline 阈值压缩（设计文档第 6 节） ------------------------------------

    def _baseline_context_estimate(self) -> int:
        """下一次请求的上下文体量估算（窗口占用口径）。

        摘要 + 未压缩轮次的全部事件（正文与工具调用参数）。
        水位曾误用累计计费口径——每步重复计整段 prompt，成倍虚增
        （实测真实上下文 3 万 tok、账本 58 万，提前触发压缩）。
        """
        parts = []
        if self.task is not None and self.task.baseline_summary:
            parts.append(self.task.baseline_summary)
        for r in self.rounds:
            if r.get("compacted"):
                continue
            for e in r["events"]:
                m = e["message"]
                parts.append(str(m.get("content") or ""))
                for tc in m.get("tool_calls") or []:
                    fn = tc.get("function") or {}
                    parts.append(str(fn.get("arguments") or ""))
        return tokens.estimate("\n".join(parts))

    def _baseline_accounting(self) -> None:
        """baseline：真实上下文体量达 80% × 窗口时触发阈值压缩。

        水位 = _baseline_context_estimate（下一次请求的体量），与
        managed 的窗口保底同口径；触发后压缩较早轮次为摘要，水位
        下次检查时自然反映压缩后的体量。baseline_prompt_used 只做
        计费口径的成本记录，不参与触发。
        """
        self._baseline_prompt_used += self.last_stats["prompt_tokens"]
        if self.task is not None:
            self.task.baseline_prompt_used = self._baseline_prompt_used
        threshold = self.context_limit * _COMPRESS_THRESHOLD
        if self._baseline_context_estimate() < threshold:
            return
        older = [r for r in self.rounds if not r.get("compacted")][:-2]
        if len(older) < 1:
            return  # 保留最近 2 轮原文；没有可压缩的历史就等下一轮
        self._emit_status("历史接近窗口上限，正在压缩较早的对话…")
        transcript = "\n\n".join(
            f"[R{r['seq']}] " + truncate.render_round_events(r) for r in older
        )
        summary, _ordered, _usage = self._stream_call(
            [{
                "role": "user",
                "content": (
                    "以下是长会话较早阶段的对话记录（截断索引形式）。"
                    "请压缩成一段高密度摘要，保留：任务相关结论、重要决策、"
                    "已尝试方案与结果、未解决的问题。省略寒暄与重复输出。"
                    "直接输出摘要正文。\n\n" + transcript
                ),
            }],
            purpose="compaction",
            extra_body={"thinking": {"type": "disabled"}},
        )
        if self.task is not None:
            prior = self.task.baseline_summary
            self.task.baseline_summary = (
                (prior + "\n\n" if prior else "") + summary.strip()
            )
        for r in older:
            r["compacted"] = True


def _sanitize_json_strings(obj):
    """递归清洗结构里的未配对代理项（见 task.sanitize_surrogates）。"""
    if isinstance(obj, str):
        return sanitize_surrogates(obj)
    if isinstance(obj, list):
        return [_sanitize_json_strings(v) for v in obj]
    if isinstance(obj, dict):
        return {k: _sanitize_json_strings(v) for k, v in obj.items()}
    return obj


def _action_word(name: str) -> str:
    """工具名 → 进度提示的动作词（未知工具退回"调用 xxx"）。"""
    return _ACTION_WORDS.get(name, f"调用 {name}")


def _schema_of(fn: Callable) -> dict:
    """根据函数签名自动生成 OpenAI tools 协议要求的 JSON Schema。"""
    properties = {}
    for name, param in inspect.signature(fn).parameters.items():
        annotation = param.annotation
        # Optional[X]（X | None）取 X 的类型，避免退化为 string
        args_ = getattr(annotation, "__args__", None)
        if args_ and type(None) in args_:
            non_none = [a for a in args_ if a is not type(None)]
            if len(non_none) == 1:
                annotation = non_none[0]
        json_type = _JSON_TYPES.get(annotation, "string")
        properties[name] = {"type": json_type}

    doc = inspect.getdoc(fn)
    # 描述取首段（空行分隔、折叠空白）：关键使用约束（如 run_command
    # 的超时与常驻服务警告）往往一行装不下，首段才能完整送达模型
    description = (
        " ".join(doc.split("\n\n")[0].split()) if doc else fn.__name__
    )

    return {
        "type": "function",
        "function": {
            "name": fn.__name__,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": [
                    name
                    for name, p in inspect.signature(fn).parameters.items()
                    if p.default is inspect.Parameter.empty
                ],
            },
        },
    }
