"""Agent 运行时 + V2 Context Runtime。

设计文档：docs/context-runtime-v2.md（定稿）。核心内容：

* Round/Event：Round 只在 AI 产出最终回答时闭合（开放轮会合并
  中断/无回复期间的多条用户输入，跨会话持久化）；Event 同时保存
  Full（原始协议消息）与 Truncated（Runtime 截断，零 LLM 成本）
* Organization：轮闭合后进入后台 FIFO 队列异步执行（不阻塞对话），
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
import threading
import time
from typing import Any, Callable, Optional

from . import tokens
from . import tools as tools_module
from . import truncate
from .llm import LLM, reasoning_of
from .task import Task
from .tools import (
    edit_file,
    get_current_time,
    list_files,
    read_file,
    run_command,
    search_files,
    write_file,
)

MODE_MANAGED = "managed"
MODE_BASELINE = "baseline"

# 上下文预算：全部按窗口百分比设计（亿级使用规模），绝对值可覆盖
_DEFAULT_CONTEXT_LIMIT = int(os.environ.get("WOVRA_CONTEXT_LIMIT", "1000000"))
_DEFAULT_HISTORY_BUDGET_RATIO = float(os.environ.get("WOVRA_HISTORY_BUDGET_RATIO", "0.3"))
_DEFAULT_MAX_RECENT_ROUNDS = int(os.environ.get("WOVRA_MAX_RECENT_ROUNDS", "3"))
_COMPRESS_THRESHOLD = float(os.environ.get("WOVRA_COMPRESS_THRESHOLD", "0.8"))

# 开放 Round 的规模软限制：事件数超限后，加载时只保留最近若干条全量
_OPEN_ROUND_EVENT_LIMIT = int(os.environ.get("WOVRA_OPEN_ROUND_EVENT_LIMIT", "50"))
_OPEN_ROUND_KEEP_FULL = 30

_ORGANIZE_MAX_CALLS = 4
_ORGANIZE_MAX_READS = 3
_CACHE_RATE = 30  # 缓存价 = 未命中的 1/30

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
        max_turns: int = 10,
        task: Optional[Task] = None,
        context_mode: str = MODE_MANAGED,
        context_limit: Optional[int] = None,
        history_budget: Optional[int] = None,
        max_recent_rounds: Optional[int] = None,
        async_organization: bool = False,
        on_tool_call: Optional[Callable[[str, str], None]] = None,
        on_tool_result: Optional[Callable[[str, str], None]] = None,
        on_status: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.llm = llm or LLM()
        self.max_turns = max_turns
        self.task = task
        self.context_mode = context_mode
        self.context_limit = context_limit or _DEFAULT_CONTEXT_LIMIT
        ratio = float(os.environ.get("WOVRA_HISTORY_BUDGET_RATIO", _DEFAULT_HISTORY_BUDGET_RATIO))
        self.history_budget = history_budget or int(self.context_limit * ratio)
        self.max_recent_rounds = max_recent_rounds or _DEFAULT_MAX_RECENT_ROUNDS
        # 整理是否异步执行：chat 模式开（不阻塞对话），run/测试用同步（确定性）
        self.async_organization = async_organization
        self.on_tool_call = on_tool_call
        self.on_tool_result = on_tool_result
        self.on_status = on_status

        tools_module.set_audit_recorder(
            lambda detail: task.record("file_change", detail) if task else None
        )

        self.tools: dict[str, Callable] = {}
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

        # baseline 记账：累计输入 token（触发阈值压缩）
        self._baseline_prompt_used = task.baseline_prompt_used if task else 0

        self.last_stats = self._fresh_stats()

        if self.context_mode == MODE_MANAGED:
            self.register(self.expand_history)
            # 惰性补跑：上次会话退出时未完成的整理（pending），本次加载即补上
            if self.task is not None:
                for r in self.rounds:
                    if r.get("org_state") == "pending" and r.get("end_state") == "completed":
                        self._enqueue_organization(r)

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

    def _open_or_reuse_round(self, user_input: str) -> None:
        """开启新 Round，或续上未闭合的开放 Round（V2 闭合规则）。

        上一轮若因中断/异常/无回复而未闭合（end_state=open），
        本轮输入并入同一个 Round——直到 AI 产出最终回答才算完整一轮。
        """
        last = self.rounds[-1] if self.rounds else None
        if last is not None and last.get("end_state") in ("", "open"):
            self.current_round = last
            # 协议消息从事件的 Full 中重建（它们就是事实来源）
            self.messages = [e["message"] for e in last["events"]]
            return
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

    def _record_event(self, type: str, message: dict, tool_name: str = "") -> dict:  # noqa: A002
        """把一条协议消息登记为 Event（生成 ID 与 Truncated）。"""
        if self.current_round is None:
            self.messages.append(message)
            return {"id": "", "message": message}
        seq = len(self.current_round["events"]) + 1
        event_id = f"R{self.current_round['seq']}-E{seq:02d}"
        event = truncate.make_event(event_id, type, message, tool_name=tool_name)
        if self.context_mode != MODE_MANAGED and "full" in event:
            # baseline：不做安全截断，保持"正常现状"的行为
            event["message"] = {**message}
        self.current_round["events"].append(event)
        self.messages.append(event["message"])
        return event

    def _persist_rounds(self) -> None:
        if self.task is not None:
            with self._save_lock:
                self.task.rounds = self.rounds
                self.task.baseline_prompt_used = self._baseline_prompt_used
                self.task.save()

    def close_round(self) -> None:
        """闭合当前 Round（仅最终回答路径调用），managed 模式进入整理队列。"""
        if self.current_round is None:
            return
        self.current_round["end_state"] = "completed"
        self.current_round = None
        self._persist_rounds()
        if self.context_mode == MODE_MANAGED and self.task is not None:
            self._enqueue_organization(self.rounds[-1])

    def finalize_round(self, end_state: str = "open") -> None:
        """CLI 异常/中断路径：Round 保持开放（不闭合、不整理），仅持久化。"""
        if self.current_round is None:
            return
        self.current_round["end_state"] = "open"
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
        self.turn_count += 1
        self.last_stats = self._fresh_stats()

        self._open_or_reuse_round(user_input)
        self._record_event("user", {"role": "user", "content": user_input})
        if self.task is not None:
            self.task.record("user_input", user_input)
            self._persist_rounds()

        for _ in range(self.max_turns):
            messages = self._assemble_messages()
            content, ordered, _usage = self._stream_call(
                messages,
                tools=self._schemas or None,
                purpose="working",
                on_thinking=on_thinking,
                on_answer_delta=on_answer_delta,
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
                for tc in ordered:
                    self._execute(call_id=tc["id"], name=tc["name"], arguments=tc["arguments"])
                continue

            # 模型不再请求工具 → 产出最终回答 → Round 闭合。
            answer = content
            self._record_event("final_answer", {"role": "assistant", "content": answer})
            if self.task is not None:
                self.task.record("final_answer", answer)
            self.close_round()
            if self.task is not None:
                if self.context_mode == MODE_BASELINE:
                    self._baseline_accounting()
                self.task.record(
                    "usage",
                    f"[{self.context_mode}] working={self.last_stats['purpose']['working']['total']} "
                    f"org={self.last_stats['purpose']['organization']['total']} "
                    f"compaction={self.last_stats['purpose']['compaction']['total']} "
                    f"prompt={self.last_stats['prompt_tokens']} "
                    f"completion={self.last_stats['completion_tokens']} "
                    f"total={self.last_stats['total_tokens']}（思考 {self.last_stats['reasoning_tokens']}）",
                )
                self._persist_rounds()
            return answer

        # 步数超限：Round 保持开放（失败尝试并入本轮，不产生整理成本），
        # 由调用方决定后续（重试/人工介入）。
        self._persist_rounds()
        raise RuntimeError(
            f"agent 超过最大循环次数（{self.max_turns} 轮）仍未给出最终回答"
        )

    def _execute(self, call_id: str, name: str, arguments: str) -> None:
        """执行单个工具调用，并把结果作为 tool 消息追加到当前 Round。"""
        if self.on_tool_call:
            self.on_tool_call(name, arguments)

        try:
            parsed = json.loads(arguments or "{}")
        except json.JSONDecodeError as error:
            result = f"工具参数不是合法 JSON: {error}"
        else:
            fn = self.tools.get(name)
            if fn is None:
                result = f"未知工具: {name}，可用工具: {list(self.tools)}"
            else:
                try:
                    result = fn(**parsed)
                except Exception as error:  # noqa: BLE001——错误回传给模型而不是中断循环
                    result = f"工具执行出错: {error!r}"

        if not isinstance(result, str):
            result = json.dumps(result, ensure_ascii=False, default=str)

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

    # ---- 流式调用（所有 LLM 交互的唯一通道，按用途分账） ------------------------

    def _stream_call(
        self,
        messages: list[dict],
        tools: Optional[list[dict]] = None,
        purpose: str = "working",
        extra_body: Optional[dict] = None,
        on_thinking: Optional[Callable[[str], None]] = None,
        on_answer_delta: Optional[Callable[[str], None]] = None,
    ) -> tuple[str, list[dict], Any]:
        """发一次流式补全，聚合分片，返回 (内容, 工具调用列表, usage)。

        purpose ∈ working / organization / compaction：成本按用途分账，
        实验才能回答"管理机制自身贵不贵"。
        """
        self.last_stats["llm_calls"] += 1
        start = time.monotonic()
        try:
            stream = self.llm.chat(messages, tools=tools, stream=True, extra_body=extra_body)
        except Exception:
            if not extra_body:
                raise
            # 部分端点不支持 extra_body 里的参数（如关闭思考的 thinking 开关），
            # 降级为不带该参数重发——宁可让整理调用多思考，也不能直接失败
            if self.on_status:
                self.on_status("当前模型不支持该调用参数，已降级重试…")
            stream = self.llm.chat(messages, tools=tools, stream=True)
        content_parts: list[str] = []
        tool_calls_acc: dict[int, dict] = {}
        usage = None
        for chunk in stream:
            if getattr(chunk, "usage", None):
                usage = chunk.usage
            if not getattr(chunk, "choices", None):
                continue
            delta = chunk.choices[0].delta
            if delta is None:
                continue

            thinking = reasoning_of(delta)
            if thinking and on_thinking:
                on_thinking(thinking)

            if delta.content:
                content_parts.append(delta.content)
                if on_answer_delta:
                    on_answer_delta(delta.content)

            for fragment in delta.tool_calls or []:
                index = fragment.index or 0
                acc = tool_calls_acc.setdefault(
                    index, {"id": "", "name": "", "arguments": ""}
                )
                if fragment.id:
                    acc["id"] = fragment.id
                if fragment.function and fragment.function.name:
                    acc["name"] = fragment.function.name
                if fragment.function and fragment.function.arguments:
                    acc["arguments"] += fragment.function.arguments

        elapsed = time.monotonic() - start
        self.last_stats["seconds"] += elapsed
        bucket = self.last_stats["purpose"].setdefault(
            purpose, {"prompt": 0, "completion": 0, "total": 0, "seconds": 0.0}
        )
        if usage is not None:
            self._accumulate_usage(usage, purpose)
        bucket["seconds"] += elapsed
        ordered = [tool_calls_acc[i] for i in sorted(tool_calls_acc)]
        return "".join(content_parts), ordered, usage

    def _accumulate_usage(self, usage, purpose: str) -> None:
        """把一次调用的 usage 累加进总账与用途分账。"""
        self.last_stats["prompt_tokens"] += usage.prompt_tokens or 0
        self.last_stats["completion_tokens"] += usage.completion_tokens or 0
        self.last_stats["total_tokens"] += usage.total_tokens or 0
        bucket = self.last_stats["purpose"].setdefault(
            purpose, {"prompt": 0, "completion": 0, "total": 0, "seconds": 0.0}
        )
        bucket["prompt"] += usage.prompt_tokens or 0
        bucket["completion"] += usage.completion_tokens or 0
        bucket["total"] += usage.total_tokens or 0

        details = getattr(usage, "completion_tokens_details", None)
        reasoning_tokens = getattr(details, "reasoning_tokens", None)
        if reasoning_tokens:
            self.last_stats["reasoning_tokens"] += reasoning_tokens

        prompt_details = getattr(usage, "prompt_tokens_details", None)
        cached = getattr(prompt_details, "cached_tokens", None) or 0
        self.last_stats["cached_tokens"] += cached
        self.last_stats["cache_miss_tokens"] += max(
            0, (usage.prompt_tokens or 0) - cached
        )

    # ---- Context Assembly（设计文档第 4 节） ------------------------------------

    def _assemble_messages(self) -> list[dict]:
        """按变化频率排序装配上下文（缓存友好布局）。

        [1] system 人设（静态）
        [2] 历史轮次视图（少变：闭合时成形，之后不可变）
        [3] Task State + 降档轮次的一行索引（每轮变——放尾部）
        [4] 当前 Round 事件（追加式；超长开放轮做软限制）
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
        if block:
            view_msgs.append({"role": "user", "content": "\n\n".join(block)})

        msgs.extend(view_msgs)
        msgs.extend(self._current_round_messages())
        return msgs

    def _current_round_messages(self) -> list[dict]:
        """当前 Round 的消息；开放轮超长时做软限制（最近若干条全量）。"""
        if self.current_round is None:
            return list(self.messages)
        events = self.current_round["events"]
        if len(events) <= _OPEN_ROUND_EVENT_LIMIT:
            return list(self.messages)
        keep = _OPEN_ROUND_KEEP_FULL
        older = events[:-keep]
        lines = [truncate.event_index_line(e) for e in older]
        block = {"role": "user", "content": (
            f"[本轮早期事件（共 {len(older)} 条，已折叠；"
            f"可用 expand_history 展开）]\n" + "\n".join(lines)
        )}
        return [block] + list(self.messages[-keep:])

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

    @staticmethod
    def _head_text(text: str, limit: int) -> str:
        text = " ".join((text or "").split())
        return text if len(text) <= limit else text[:limit] + "…"

    # ---- History Maintenance Pipeline（异步整理队列） ----------------------------

    def _enqueue_organization(self, round_data: dict) -> None:
        round_data["org_state"] = "pending"
        if self.async_organization:
            self._org_queue.put(round_data)
            self._ensure_worker()
        else:
            # 同步模式（run 命令/测试）：立即整理，进程退出前结果必须落盘
            self._organize_round(round_data)

    def _ensure_worker(self) -> None:
        if self._org_thread is not None and self._org_thread.is_alive():
            return
        self._org_thread = threading.Thread(
            target=self._org_worker, name="wovra-organization", daemon=True
        )
        self._org_thread.start()

    def _org_worker(self) -> None:
        while True:
            round_data = self._org_queue.get()
            try:
                self._organize_round(round_data)
            except Exception:  # noqa: BLE001——整理失败不影响主对话
                round_data["org_state"] = "failed"
                self._persist_rounds()
            finally:
                self._org_queue.task_done()

    def flush_organization(self, timeout: float = 10.0) -> bool:
        """等待异步整理队列清空（chat 退出限时等待；run 用同步模式无需调用）。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._org_queue.unfinished_tasks == 0:
                return True
            time.sleep(0.1)
        return self._org_queue.unfinished_tasks == 0

    def _organize_round(self, round_data: dict) -> None:
        """整理一个闭合的 Round（维护管线的工作单元）。

        输入 = 用户输入们 + 事件截断索引 + 最终回答全文（默认不读事件
        原文；Truncated 不足以确定关键事实时可用 read_full 按上限展开）。
        输出 = Normalized 意图 + 精修事件索引 + State Patch。
        关闭思考（格式化任务）。解析失败重试一次，仍失败则保持
        Runtime 视图，原始层永远不受影响。
        """
        if self.on_status:
            self.on_status("正在整理本轮对话…")
        user_inputs = [
            e["message"].get("content", "")
            for e in round_data["events"] if e["type"] == "user"
        ]
        index = truncate.render_round_events(round_data)
        final_event = next(
            (e for e in reversed(round_data["events"]) if e["type"] == "final_answer"), None
        )
        final_text = (final_event["message"].get("content") or "") if final_event else ""

        prompt = (
            "你是任务整理器。以下是一个已完成 Round 的用户输入、"
            "事件截断索引与最终回答。\n"
            "你的职责（最后只输出一个 JSON 对象，不要代码块围栏）：\n"
            '1. "normalized_user_input"：合并并澄清这些用户输入的实际意图'
            "——不是压缩，是把用户想要什么说得更清楚，可以比原文长；\n"
            '2. "refined_index"：事件精修索引，数组元素为 {"id": "事件ID", '
            '"line": "一行摘要"}——比截断行更短更准（保留结论：什么可行、'
            "什么实测不行、卡在哪），id 必须取自事件流中已有的事件 ID，"
            "无实质内容的事件（如寒暄）可省略；\n"
            '3. "state_patch"：任务状态增量补丁 {"completed":[],'
            '"decisions":[],"known_issues":[],"open_questions":[],'
            '"current_status":"...","goal":"...","is_done":bool}——'
            "只包含需要新增或修改的条目。\n"
            f"若截断索引不足以确定关键事实（如失败的具体原因），"
            f"可用 read_full 工具查看事件原文（最多 {_ORGANIZE_MAX_READS} 次）。\n\n"
            "[用户输入（多条时按顺序合并理解）]\n"
            + "\n---\n".join(user_inputs)
            + "\n\n[事件截断索引]\n" + index + "\n"
        )
        if final_text:
            prompt += f"\n[最终回答（完整）]\n{final_text[:2000]}\n"

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
            round_data["org_state"] = "failed"
            self._persist_rounds()
            return
        if state.get("normalized_user_input"):
            round_data["user_input"]["normalized"] = state["normalized_user_input"]
        refined = state.get("refined_index")
        if isinstance(refined, list):
            valid_ids = {e["id"] for e in round_data["events"]}
            for item in refined:
                if isinstance(item, dict) and item.get("id") in valid_ids and item.get("line"):
                    round_data["refined_index"][item["id"]] = str(item["line"])
        patch = state.get("state_patch")
        if isinstance(patch, dict):
            if state.get("is_done") is not None and "is_done" not in patch:
                patch["is_done"] = bool(state["is_done"])
            self.task.apply_state_patch(patch)
        round_data["org_state"] = "done"
        if self.on_status:
            self.on_status("整理完成")
        self._persist_rounds()

    def _organize_schemas(self) -> list[dict]:
        """Organization 阶段唯一的工具：按事件 ID 读取原文（后门，默认不用）。"""

        def read_full(event_id: str) -> str:
            """按事件 ID（如 R1-E02）读取该事件的完整原文。"""
            return self._read_full_event(event_id)

        return [_schema_of(read_full)]

    def _read_full_event(self, event_id: str) -> str:
        for r in self.rounds:
            for e in r["events"]:
                if e["id"] == event_id:
                    message = e["message"]
                    parts = [f"[{event_id}] {e['type']}"]
                    if message.get("tool_calls"):
                        parts.append(
                            "调用: " + json.dumps(message["tool_calls"], ensure_ascii=False)[:2000]
                        )
                    parts.append((message.get("content") or "")[:4000])
                    if e.get("full"):
                        parts.append("[完整原文]\n" + e["full"][:4000])
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
                    + (e.get("full") or e["message"].get("content") or "")[:2000]
                )
            return "\n".join(parts)
        return f"未找到轮次: {round_id}"

    # ---- baseline 阈值压缩（设计文档第 6 节） ------------------------------------

    def _baseline_accounting(self) -> None:
        """baseline：累计输入达到 80% × 窗口时触发常规阈值压缩。"""
        self._baseline_prompt_used += self.last_stats["prompt_tokens"]
        if self.task is not None:
            self.task.baseline_prompt_used = self._baseline_prompt_used
        threshold = self.context_limit * _COMPRESS_THRESHOLD
        if self._baseline_prompt_used < threshold:
            return
        older = [r for r in self.rounds if not r.get("compacted")][:-2]
        if len(older) < 1:
            return  # 保留最近 2 轮原文；没有可压缩的历史就等下一轮
        if self.on_status:
            self.on_status("历史接近窗口上限，正在压缩较早的对话…")
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
        # 水位重置：压缩后基座 ≈ 摘要 + 保留轮次的体积
        kept = [r for r in self.rounds if not r.get("compacted")]
        self._baseline_prompt_used = tokens.estimate(
            (self.task.baseline_summary if self.task else "")
            + "\n".join(
                e["message"].get("content", "")
                for r in kept for e in r["events"]
            )
        )
        if self.task is not None:
            self.task.baseline_prompt_used = self._baseline_prompt_used


def _schema_of(fn: Callable) -> dict:
    """根据函数签名自动生成 OpenAI tools 协议要求的 JSON Schema。"""
    properties = {}
    for name, param in inspect.signature(fn).parameters.items():
        annotation = param.annotation
        json_type = _JSON_TYPES.get(annotation, "string")
        properties[name] = {"type": json_type}

    doc = inspect.getdoc(fn)
    description = doc.splitlines()[0] if doc else fn.__name__

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
