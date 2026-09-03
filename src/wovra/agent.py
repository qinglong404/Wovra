"""Agent 运行时 + V1 Context Runtime（Round/Event/组织/装配/展开）。

执行循环与上下文管理的关系（设计稿口径）：

* Round = 用户一条消息从发出到最终回答的完整过程，是工作记忆边界；
* Event = Round 内的一次记录（用户输入/工具调用/工具结果/回答…），
  每个 Event 同时保存 Full（原始协议消息，事实来源）与
  Truncated（Runtime 程序生成的概览，零 LLM 成本，见 truncate.py）；
* 当前 Round 不截断（超大内容走安全阈值）；
* Round 结束后进行一次 Organization：产出 Round Summary、
  Normalized 用户输入和 Task State Patch（一次调用，不多外包）；
* 新 Round 的上下文由 Context Assembly 装配：
  Task State + 选中的历史（Summary/Truncated）+ 当前 Round 全量；
* AI 可用 expand_history 按需把历史从 Truncated 升级到 Full，
  展开只是读取，不修改历史。

两种模式（对照实验）：
* managed  —— 上述机制全部生效（默认）；
* baseline —— 完全正常的现状：全量消息回放，无组织、无状态、
  无安全截断。上下文爆掉本身就是对照组的实验数据。
"""

import inspect
import json
import os
import re
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

# 上下文预算与策略参数（均可被环境变量覆盖）
_DEFAULT_CONTEXT_LIMIT = int(os.environ.get("WOVRA_CONTEXT_LIMIT", "65536"))
_DEFAULT_MAX_RECENT_ROUNDS = int(os.environ.get("WOVRA_MAX_RECENT_ROUNDS", "3"))
_DEFAULT_MAX_HISTORY_TOKENS = int(os.environ.get("WOVRA_MAX_HISTORY_TOKENS", "6000"))

# Organization 两段式的限制：最多 4 次调用、3 次原文展开
_ORGANIZE_MAX_CALLS = 4
_ORGANIZE_MAX_READS = 3

# 相关性判定的最低重合度（字符二元组/单词数），低于它的轮次不入选
_RELEVANCE_MIN_SCORE = 2

# Python 类型注解 → JSON Schema 类型 的对应表（工具 schema 自动生成用）
_JSON_TYPES = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
}


def _relevance(query: str, text: str) -> int:
    """极简相关性：CJK 取字符二元组、ASCII 取单词，数重合集大小。

    V1 规则优先（设计稿第 11 节）——不上 embedding，够用来挑出
    "哪几轮在聊同一件事"即可，选不中的轮次靠 expand_history 兜底。
    """
    def grams(s: str) -> set:
        s = s.lower()
        tokens_out = set(re.findall(r"[a-z0-9_]+", s))
        cjk = re.findall(r"[\u4e00-\u9fff]", s)
        tokens_out.update(f"{a}{b}" for a, b in zip(cjk, cjk[1:]))
        return tokens_out

    return len(grams(query) & grams(text))


class Agent:
    """一个能调用工具、具备上下文生命周期管理的智能体。

    用法（见 examples/ 与 cli.py）：

        agent = Agent(system_prompt="...", tools=[my_tool], task=task)
        answer = agent.run("帮我做某件事")
    """

    def __init__(
        self,
        llm: Optional[LLM] = None,
        system_prompt: str = "",
        tools: tuple = (),
        max_turns: int = 10,
        task: Optional[Task] = None,
        context_mode: str = MODE_MANAGED,
        context_limit: Optional[int] = None,
        max_recent_rounds: Optional[int] = None,
        max_history_tokens: Optional[int] = None,
        on_tool_call: Optional[Callable[[str, str], None]] = None,
        on_tool_result: Optional[Callable[[str, str], None]] = None,
        on_status: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.llm = llm or LLM()
        self.max_turns = max_turns
        self.task = task
        self.context_mode = context_mode
        self.context_limit = context_limit or _DEFAULT_CONTEXT_LIMIT
        self.max_recent_rounds = max_recent_rounds or _DEFAULT_MAX_RECENT_ROUNDS
        self.max_history_tokens = max_history_tokens or _DEFAULT_MAX_HISTORY_TOKENS
        # 观察性钩子：Wovra 关注"可观察"，CLI 用它把工具调用过程打印出来
        self.on_tool_call = on_tool_call
        self.on_tool_result = on_tool_result
        # 后台动作（如 Round 整理）耗时较长，状态回调让界面"不卡"
        self.on_status = on_status

        # 注册审计挂钩：变更类工具（写/改/执行命令）通过它把
        # 参数之外的事实（如被覆盖文件的旧内容）记入任务历史
        tools_module.set_audit_recorder(
            lambda detail: task.record("file_change", detail) if task else None
        )

        self.tools: dict[str, Callable] = {}
        self._schemas: list[dict] = []
        for fn in tools:
            self.register(fn)

        # 对话轮次：本 Agent 实例经历过的 run() 次数（跨步累计）
        self.turn_count = 0
        # 成本核算：在 __init__ 即初始化，保证任何调用路径都有账本
        self.last_stats = {
            "seconds": 0.0, "turn": 0, "llm_calls": 0, "tool_calls": 0,
            "prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0,
            "total_tokens": 0, "cached_tokens": 0, "cache_miss_tokens": 0,
            "mode": self.context_mode,
            "prompt_breakdown": dict.fromkeys(tokens.CATEGORIES, 0),
        }
        # 历史轮次从任务持久层恢复（managed/baseline 共用原始层）
        self.rounds: list[dict] = [dict(r) for r in (task.rounds if task else [])]
        self.current_round: Optional[dict] = None
        # 当前 Round 的协议消息列表（工具循环直接追加），
        # 它与 current_round["events"] 同源（见 _record_event）
        self.messages: list[dict] = []
        self.compressed_prefix: str = ""  # 保留字段：baseline 未来如需压缩再启用
        # managed 模式注册历史展开工具（AI 主动找回细节的唯一入口）
        if self.context_mode == MODE_MANAGED:
            self.register(self.expand_history)
        # system 提示词：不再注入目标/摘要等报告内容（报告只给人看）
        self.system_prompt = system_prompt

    def register(self, fn: Callable) -> None:
        """把一个 Python 函数注册为模型可调用的工具。"""
        if fn.__name__ in self.tools:
            raise ValueError(f"工具重复注册: {fn.__name__}")
        self.tools[fn.__name__] = fn
        self._schemas.append(_schema_of(fn))

    # ---- Round / Event 流水线 -------------------------------------------------

    def _record_event(self, type: str, message: dict, tool_name: str = "") -> dict:  # noqa: A002
        """把一条协议消息登记为 Event（生成 ID 与 Truncated）。

        managed 模式下超大工具结果触发安全阈值：协议消息里只留
        安全范围 + 事件 ID；baseline 保持原文（对照实验要的就是
        "正常现状"的行为）。
        """
        if self.current_round is None:
            # 直接调 _execute 的测试路径：没有 Round 也必须可用
            self.messages.append(message)
            return {"id": "", "message": message}
        seq = len(self.current_round["events"]) + 1
        event_id = f"R{self.current_round['seq']}-E{seq:02d}"
        event = truncate.make_event(event_id, type, message, tool_name=tool_name)
        if self.context_mode != MODE_MANAGED and "full" in event:
            # baseline：撤销安全截断，恢复原文
            event["message"] = {**message}
        self.current_round["events"].append(event)
        self.messages.append(event["message"])
        return event

    # ---- 主循环 ---------------------------------------------------------------

    def run(
        self,
        user_input: str,
        on_thinking: Optional[Callable[[str], None]] = None,
        on_answer_delta: Optional[Callable[[str], None]] = None,
    ) -> str:
        """处理一条用户输入（开启并完成一个 Round），返回最终回答。"""
        self.turn_count += 1
        self.last_stats = {
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
            "prompt_breakdown": dict.fromkeys(tokens.CATEGORIES, 0),
        }

        seq = len(self.rounds) + 1
        self.current_round = {
            "seq": seq,
            "user_input": {"original": user_input, "normalized": ""},
            "events": [],
            "summary": "",
            "end_state": "",
        }
        self.rounds.append(self.current_round)
        self.messages = []  # 当前 Round 的协议消息，随事件一起重建
        self._record_event("user", {"role": "user", "content": user_input})
        if self.task is not None:
            self.task.record("user_input", user_input)
            self.task.save()  # 先落盘再干活：进程崩溃也不丢这条输入

        for _ in range(self.max_turns):
            messages = self._assemble_messages()
            content, ordered, _usage = self._stream_call(
                messages,
                tools=self._schemas or None,
                on_thinking=on_thinking,
                on_answer_delta=on_answer_delta,
            )

            if ordered:
                # 请求了工具：先把这条 assistant 消息（含 tool_calls）放回历史，
                # 协议要求紧随其后的必须是每个工具调用对应的 tool 消息。
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

            # 模型不再请求工具，说明它认为可以直接回答了，Round 结束。
            answer = content
            self._record_event("final_answer", {"role": "assistant", "content": answer})
            if self.task is not None:
                self.task.record("final_answer", answer)
            self.finalize_round("completed")
            if self.task is not None:
                s = self.last_stats
                self.task.record(
                    "usage",
                    f"[{s['mode']}] {s['seconds']:.1f}s, "
                    f"prompt={s['prompt_tokens']}, completion={s['completion_tokens']}, "
                    f"total={s['total_tokens']}（其中思考 {s['reasoning_tokens']}）",
                )
                self.task.save()
            return answer

        # 防御性上限：模型可能陷入"永远在调工具"的死循环，
        # max_turns 保证循环一定终止。报告/干预机制成熟后由人接管。
        self.finalize_round("failed")
        raise RuntimeError(
            f"agent 超过最大循环次数（{self.max_turns} 轮）仍未给出最终回答"
        )

    def finalize_round(self, end_state: str) -> None:
        """Round 收尾（正常完成/中断/失败共用），保证只整理一次。"""
        if self.current_round is None:
            return
        if self.context_mode == MODE_MANAGED and self.task is not None:
            self._organize_round(end_state)
        self.current_round["end_state"] = end_state
        if self.task is not None:
            self.task.rounds = self.rounds
            self.task.save()
        self.current_round = None  # 置空即是"已收尾"标记，防止重复整理

    def _execute(self, call_id: str, name: str, arguments: str) -> None:
        """执行单个工具调用，并把结果作为 tool 消息追加到历史。

        任何失败（参数不是 JSON、工具名不存在、函数抛异常）都
        不抛出，而是把错误文本作为工具结果回传给模型——让模型
        自己看到错误并尝试纠正，循环才不会因为一次失败而中断。
        """
        if self.on_tool_call:
            self.on_tool_call(name, arguments)

        try:
            parsed = json.loads(arguments or "{}")
        except json.JSONDecodeError as error:
            result = f"工具参数不是合法 JSON: {error}"
        else:
            fn = self.tools.get(name)
            if fn is None:
                # 模型可能臆造不存在的工具名，如实告知它
                result = f"未知工具: {name}，可用工具: {list(self.tools)}"
            else:
                try:
                    result = fn(**parsed)
                except Exception as error:  # noqa: BLE001——错误要回传给模型而不是中断循环
                    result = f"工具执行出错: {error!r}"

        # 协议要求工具结果是字符串；非字符串（如 dict/list）序列化后回传
        if not isinstance(result, str):
            result = json.dumps(result, ensure_ascii=False, default=str)

        if self.on_tool_result:
            self.on_tool_result(name, result)

        # 工具结果作为 Event 进入当前 Round（managed 模式下超大结果
        # 会在这里触发安全阈值，回传给模型的是"安全范围 + 事件 ID"）。
        # 协议消息的入列由 _record_event 负责（两种路径都已覆盖）
        event = self._record_event(
            "tool_result", {"role": "tool", "tool_call_id": call_id, "content": result},
            tool_name=name,
        )
        result_for_context = event["message"]["content"] if event.get("id") else result

        # 每次工具调用与结果都进入任务历史（人类时间线，程序维护）
        if self.task is not None:
            self.task.record("tool_call", f"{name}({arguments})")
            self.task.record("tool_result", f"{name} -> {result_for_context[:500]}")
            self.task.save()

    # ---- 流式调用（所有 LLM 交互的唯一通道） -------------------------------------

    def _stream_call(
        self,
        messages: list[dict],
        tools: Optional[list[dict]] = None,
        on_thinking: Optional[Callable[[str], None]] = None,
        on_answer_delta: Optional[Callable[[str], None]] = None,
    ) -> tuple[str, list[dict], Any]:
        """发一次流式补全，聚合分片，返回 (内容, 工具调用列表, usage)。

        所有 LLM 调用（干活/组织/展开后的补读）都经过这里，
        计时与用量累加因此天然覆盖每一次调用。
        """
        self.last_stats["llm_calls"] += 1
        start = time.monotonic()
        stream = self.llm.chat(messages, tools=tools, stream=True)
        content_parts: list[str] = []
        # 流式协议下，工具调用是分片到达的：同一调用的参数 JSON
        # 会被拆成多个 delta 追加式下发，必须按 index 聚合
        tool_calls_acc: dict[int, dict] = {}
        usage = None
        for chunk in stream:
            # usage 只在最后一个分块上，且该分块 choices 为空
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

        self.last_stats["seconds"] += time.monotonic() - start
        if usage is not None:
            self._accumulate_usage(usage)
        ordered = [tool_calls_acc[i] for i in sorted(tool_calls_acc)]
        return "".join(content_parts), ordered, usage

    def _accumulate_usage(self, usage) -> None:
        """把一次调用的 usage 累加进 last_stats。

        所有 LLM 调用（干活/组织）都必须经过 _stream_call → 这里，
        否则就会漏计——此前摘要调用漏掉缓存与思考 token，
        导致 命中+未命中 对不上输入总量，就是这么来的。
        """
        self.last_stats["prompt_tokens"] += usage.prompt_tokens or 0
        self.last_stats["completion_tokens"] += usage.completion_tokens or 0
        self.last_stats["total_tokens"] += usage.total_tokens or 0

        details = getattr(usage, "completion_tokens_details", None)
        reasoning_tokens = getattr(details, "reasoning_tokens", None)
        if reasoning_tokens:
            # 推理模型的思考 token 也计费，单列出来成本核算才准确
            self.last_stats["reasoning_tokens"] += reasoning_tokens

        # 缓存命中：prompt_tokens_details.cached_tokens（OpenAI 协议扩展）。
        # 方舟等端点会漏报部分调用的明细——漏报的按 0 命中计入未命中，
        # 保证 命中+未命中 ≡ 输入总量
        prompt_details = getattr(usage, "prompt_tokens_details", None)
        cached = getattr(prompt_details, "cached_tokens", None) or 0
        self.last_stats["cached_tokens"] += cached
        self.last_stats["cache_miss_tokens"] += max(
            0, (usage.prompt_tokens or 0) - cached
        )

    # ---- Context Assembly（设计稿第 10/11 节） -----------------------------------

    def _assemble_messages(self) -> list[dict]:
        """把 Task State + 选中的历史 + 当前 Round 组装成协议消息。

        baseline：system + 全部历史的原文回放 + 当前 Round——
        与"正常现状"完全等价，无任何加工。
        managed：system（含 Task State）+ 选中轮次的浓缩视图
        （用户原文 + 整理摘要 + 事件截断索引）+ 当前 Round 全量。
        """
        system_parts = [self.system_prompt] if self.system_prompt else []
        msgs: list[dict] = []
        past = [r for r in self.rounds if r is not self.current_round]

        if self.context_mode == MODE_BASELINE:
            msgs.append(self._system_message(system_parts))
            for r in past:
                msgs.extend(e["message"] for e in r["events"])
            msgs.extend(self.messages)
            return msgs

        # managed：Task State 进 system（AI 可见的工作状态，
        # 与"给人看的报告"严格分离）
        if self.task is not None:
            state_text = self.task.get_state().render(
                budget=self.max_history_tokens // 3
            )
            if state_text:
                system_parts.append(state_text)

        # 选择历史：最近 K 轮恒选；更早轮次按相关性挑选（预算内）
        recent = past[-self.max_recent_rounds:]
        recent_ids = {id(r) for r in recent}
        query = self.current_round["user_input"]["original"] if self.current_round else ""
        candidates = sorted(
            (
                (self._relevance(query, self._round_text(r)), r)
                for r in past
                if id(r) not in recent_ids
            ),
            key=lambda pair: -pair[0],
        )
        selected_ids = {id(r) for r in recent}
        used = 0
        for score, r in candidates:
            if score < _RELEVANCE_MIN_SCORE:
                break
            cost = tokens.estimate(self._render_round_view(r))
            if used + cost > self.max_history_tokens:
                continue
            selected_ids.add(id(r))
            used += cost

        # 未选中轮次的单行索引（模型由此知道还有什么可展开）
        index_lines = []
        for r in past:
            if id(r) in selected_ids:
                continue
            head = r["user_input"].get("normalized") or r["user_input"]["original"]
            summary_head = _head_text(r.get("summary", ""), 60)
            index_lines.append(f"[R{r['seq']}] 用户：{_head_text(head, 80)}｜{summary_head}")
        if index_lines:
            system_parts.append("[历史索引]（未被选中的轮次，可用 expand_history 展开）\n" + "\n".join(index_lines))
        msgs.append(self._system_message(system_parts))

        for r in past:
            if id(r) not in selected_ids:
                continue
            if r.get("summary") or r["user_input"].get("normalized"):
                # 浓缩视图：用户原文全量 + 整理摘要 + 事件截断索引
                msgs.append({"role": "user", "content": r["user_input"]["original"]})
                body = [f"[第{r['seq']}轮整理] 意图：{r['user_input'].get('normalized', '')}"]
                if r.get("summary"):
                    body.append(f"[摘要] {r['summary']}")
                body.append("[事件索引]")
                body += [truncate.event_index_line(e) for e in r["events"]]
                msgs.append({"role": "assistant", "content": "\n".join(body)})
            else:
                # 未整理的轮次（如中断且整理失败）：回放原文兜底
                msgs.extend(e["message"] for e in r["events"])

        msgs.extend(self.messages)
        return msgs

    def _system_message(self, parts: list[str]) -> dict:
        content = "\n\n".join(p for p in parts if p)
        return {"role": "system", "content": content}

    @staticmethod
    def _round_text(r: dict) -> str:
        """用于相关性计算的轮次文本（意图 + 摘要 + 用户原文）。"""
        return " ".join(
            filter(None, [
                r["user_input"].get("normalized", ""),
                r["user_input"]["original"],
                r.get("summary", ""),
            ])
        )

    @staticmethod
    def _head_text(text: str, limit: int) -> str:
        text = " ".join((text or "").split())
        return text if len(text) <= limit else text[:limit] + "…"

    # ---- Round Organization（设计稿第 6/7/8 节） ---------------------------------

    def _organize_round(self, end_state: str) -> None:
        """Round 结束后的整理：Summary + Normalized 输入 + State Patch。

        一次调用同时产出三件事（不再单独调用状态刷新）。
        默认只看 Truncated 事件流；信息不足时模型可用 read_full
        按需展开原文（上限 _ORGANIZE_MAX_READS 次），
        使整理成本不随 Round 内工具数量线性失控。
        解析失败则保留原状态——宁可这轮不整理，也不用坏数据覆盖。
        """
        if self.on_status:
            self.on_status("正在整理本轮对话…")
        round_data = self.current_round
        seq = round_data["seq"]
        transcript = truncate.render_round_events(round_data)
        final_answer = next(
            (e for e in reversed(round_data["events"]) if e["type"] == "final_answer"), None
        )

        if end_state == "interrupted":
            task_hint = "本轮被用户中断：说明正在做什么、做到哪里、为什么中断、未完成什么。"
        elif end_state == "failed":
            task_hint = "本轮执行失败：重点整理失败原因、已尝试方案、当前状态、后续建议。"
        else:
            task_hint = "本轮正常完成。"

        prompt = (
            f"你是任务整理器。以下是第 {seq} 轮对话的 Truncated 事件流"
            f"（{end_state}）。{task_hint}\n"
            "你的职责（最后只输出一个 JSON 对象，不要代码块围栏）：\n"
            '1. "normalized_user_input"：澄清用户这条消息的实际意图——'
            "不是压缩，是把用户想要什么说得更清楚，可以比原文长；\n"
            '2. "round_summary"：本轮过程的高浓度摘要——按步骤组织，'
            "每步保留结论级信息（什么可行、什么实测不行、卡在哪），"
            "相似步骤可合并；删除冗余输出细节，但不要过度压缩；\n"
            '3. "state_patch"：任务状态增量补丁 '
            '{"completed":[],"decisions":[],"known_issues":[],"open_questions":[],'
            '"current_status":"...","goal":"...","is_done":bool}——'
            "只包含需要新增或修改的条目，没有变化的字段给空值。\n"
            "如果 Truncated 信息不足以整理（如失败原因不明），"
            f"先用 read_full 工具查看相关事件原文（最多 {_ORGANIZE_MAX_READS} 次）。\n\n"
            f"[Truncated 事件流]\n{transcript}\n"
        )
        if final_answer:
            prompt += f"\n[最终回答]\n{final_answer['truncated']}\n"

        reads_left = _ORGANIZE_MAX_READS
        messages = [{"role": "user", "content": prompt}]
        content = ""
        for _ in range(_ORGANIZE_MAX_CALLS):
            content, ordered, _usage = self._stream_call(messages, tools=self._organize_schemas())
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
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result,
                })

        state = self._parse_state_json(content)
        if state is None and content.strip():
            # 一次重试：明确要求只输出 JSON 本身（模型偶发夹带说明文字/围栏）
            retry_content, _ordered, _usage = self._stream_call(
                messages
                + [
                    {"role": "assistant", "content": content[:2000]},
                    {"role": "user", "content": "你的输出不是合法 JSON。请重新输出，只包含 JSON 对象本身。"},
                ]
            )
            state = self._parse_state_json(retry_content)
        if not isinstance(state, dict):
            return  # 保留原状态，本轮不更新
        if state.get("normalized_user_input"):
            round_data["user_input"]["normalized"] = state["normalized_user_input"]
        if state.get("round_summary"):
            round_data["summary"] = state["round_summary"]
        patch = state.get("state_patch")
        if isinstance(patch, dict):
            if state.get("is_done") is not None and "is_done" not in patch:
                patch["is_done"] = bool(state["is_done"])
            self.task.apply_state_patch(patch)

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

    def _organize_schemas(self) -> list[dict]:
        """Organization 阶段唯一的工具：按事件 ID 读取原文。"""

        def read_full(event_id: str) -> str:
            """按事件 ID（如 R1-E02）读取该事件的完整原文。"""
            for r in self.rounds:
                for e in r["events"]:
                    if e["id"] == event_id:
                        message = e["message"]
                        parts = [f"[{e['id']}] {e['type']}"]
                        if message.get("tool_calls"):
                            parts.append(
                                "调用: " + json.dumps(message["tool_calls"], ensure_ascii=False)[:2000]
                            )
                        body = message.get("content") or ""
                        parts.append(body[:4000])
                        if e.get("full"):
                            parts.append("[完整原文]\n" + e["full"][:4000])
                        return "\n".join(parts)
            return f"未找到事件: {event_id}"

        return [_schema_of(read_full)]

    def _read_full_event(self, event_id: str) -> str:
        """主上下文里按 ID 读取事件原文（供 expand_history 复用）。"""
        for r in self.rounds:
            for e in r["events"]:
                if e["id"] == event_id:
                    message = e["message"]
                    parts = [f"[{event_id}] {e['type']}"]
                    if message.get("tool_calls"):
                        parts.append("调用: " + json.dumps(message["tool_calls"], ensure_ascii=False)[:2000])
                    parts.append((message.get("content") or "")[:4000])
                    if e.get("full"):
                        parts.append("[完整原文]\n" + e["full"][:4000])
                    return "\n".join(parts)
        return f"未找到事件: {event_id}"

    # ---- 历史展开（设计稿第 12 节） ----------------------------------------------

    def expand_history(self, ids: list[str] | str, level: str = "full") -> str:
        """按需展开历史：Truncated → Summary → Full 三档读取。

        ids 可为轮（"R3"）或事件（"R3-E02"），一次可传多个；
        模型偶尔会把列表写成逗号分隔字符串、把 level 写成大写，
        这里统一容错。展开只是临时把更高分辨率的信息读进当前
        上下文，不修改历史。当前 Round 的内容本就是全量。
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
                    self._read_full_event(rid)
                    if level == "full"
                    else self._event_summary(rid)
                )
            else:
                results.append(self._expand_round(rid, level))
        return "\n\n".join(results) or "未找到任何 ID"

    def _event_summary(self, event_id: str) -> str:
        for r in self.rounds:
            for e in r["events"]:
                if e["id"] == event_id:
                    return f"[{e['id']}] {e['truncated']}"
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
                head = r["user_input"].get("normalized") or r["user_input"]["original"]
                lines = [f"[R{seq}] 用户：{head}"]
                if r.get("summary"):
                    lines.append(f"[摘要] {r['summary']}")
                lines += [truncate.event_index_line(e) for e in r["events"]]
                return "\n".join(lines)
            # full：整轮原文（每条事件限 2000 字，防止一次性爆上下文）
            parts = [f"[R{seq}] 用户：{r['user_input']['original']}"]
            for e in r["events"]:
                if e["type"] == "user":
                    continue
                parts.append(f"--- {e['id']} ({e['type']}) ---\n" + (e.get("full") or e["message"].get("content") or "")[:2000])
            return "\n".join(parts)
        return f"未找到轮次: {round_id}"

    # ---- 用量与成本核算 ---------------------------------------------------------

    def _accumulate_usage(self, usage) -> None:
        """把一次调用的 usage 累加进 last_stats。

        所有 LLM 调用（干活/组织）都经过 _stream_call → 这里，
        否则就会漏计——此前摘要调用漏掉缓存与思考 token，
        导致 命中+未命中 对不上输入总量，就是这么来的。
        """
        self.last_stats["prompt_tokens"] += usage.prompt_tokens or 0
        self.last_stats["completion_tokens"] += usage.completion_tokens or 0
        self.last_stats["total_tokens"] += usage.total_tokens or 0

        details = getattr(usage, "completion_tokens_details", None)
        reasoning_tokens = getattr(details, "reasoning_tokens", None)
        if reasoning_tokens:
            # 推理模型的思考 token 也计费，单列出来成本核算才准确
            self.last_stats["reasoning_tokens"] += reasoning_tokens

        # 缓存命中：prompt_tokens_details.cached_tokens（OpenAI 协议扩展）。
        # 方舟等端点会漏报部分调用的明细——漏报的按 0 命中计入未命中，
        # 保证 命中+未命中 ≡ 输入总量
        prompt_details = getattr(usage, "prompt_tokens_details", None)
        cached = getattr(prompt_details, "cached_tokens", None) or 0
        self.last_stats["cached_tokens"] += cached
        self.last_stats["cache_miss_tokens"] += max(
            0, (usage.prompt_tokens or 0) - cached
        )


def _schema_of(fn: Callable) -> dict:
    """根据函数签名自动生成 OpenAI tools 协议要求的 JSON Schema。

    约定：
      * 函数 docstring 的第一行作为工具描述（模型靠它决定何时用这个工具）
      * 参数的 JSON 类型由类型注解推断，未注解的参数按 string 处理
      * 需要更精细的参数说明时，可以绕过本函数直接手写 schema
    """
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
