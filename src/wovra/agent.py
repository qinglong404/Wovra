"""最小 Agent 运行时：LLM 与工具之间的执行循环。

对应 README 路线图的阶段 1 —— 跑通这个环：

    用户输入 → LLM → (需要工具?) → 执行工具 → 结果回传 → LLM → ... → 最终回答

设计取向：
    * 阶段 1 只关心"循环能转起来"，所以刻意保持小；
      任务状态持久化、上下文折叠是阶段 2/3 的事，这里不做。
    * 回传给服务端的 assistant 消息被清洗成纯 dict，
      只保留协议要求的字段——reasoning_content 等扩展字段
      不应回传（多数兼容服务端不接受它们出现在历史里）。
"""

import inspect
import json
import time
from typing import Callable, Optional

from . import tokens
from .llm import LLM, reasoning_of
from .task import Task
from .tools import (
    AUDITED_TOOLS,
    edit_file,
    get_current_time,
    list_files,
    read_file,
    run_command,
    write_file,
)

# Python 类型注解 → JSON Schema 类型 的对应表。
# 工具的参数 schema 就是由它自动生成的，因此工具函数的参数
# 应当使用这几种内置类型做注解。
_JSON_TYPES = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
}


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
        # 没写注解时 annotation 是 inspect.Parameter.empty，按 string 兜底
        json_type = _JSON_TYPES.get(annotation, "string")
        properties[name] = {"type": json_type}

    # docstring 第一行 = 工具描述；没有 docstring 就用函数名凑合
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
                    # 没有默认值的参数视为必填，与 Python 语义一致
                    if p.default is inspect.Parameter.empty
                ],
            },
        },
    }


class Agent:
    """一个能调用工具的最小智能体。

    用法（见 examples/tool_call.py）：

        agent = Agent(system_prompt="...", tools=[my_tool, another_tool])
        answer = agent.run("帮我做某件事")

    run() 内部会循环"请求模型 → 执行工具 → 回传结果"，直到模型
    给出不含 tool_calls 的最终回答，或达到 max_turns 安全上限。
    """

    def __init__(
        self,
        llm: Optional[LLM] = None,
        system_prompt: str = "",
        tools: tuple = (),
        max_turns: int = 10,
        task: Optional[Task] = None,
        on_tool_call: Optional[Callable[[str, dict], None]] = None,
        on_tool_result: Optional[Callable[[str, str], None]] = None,
    ) -> None:
        self.llm = llm or LLM()
        self.max_turns = max_turns
        # 绑定任务后，agent 的每次输入、工具调用、最终回答都会
        # 记录到 task 并落盘（task=None 时退化为纯内存对话，阶段 1 行为不变）
        self.task = task
        # 观察性钩子：Wovra 关注"可观察"，示例用它把工具调用过程打印出来。
        # 正式的事件/报告系统是路线图后面阶段的内容。
        self.on_tool_call = on_tool_call
        self.on_tool_result = on_tool_result

        self.tools: dict[str, Callable] = {}
        self._schemas: list[dict] = []
        for fn in tools:
            self.register(fn)

        # 为 token 分类核算保存两个边界：注入前后的 system 消息
        # 由这两部分拼成（见下），只有这里知道各自的长度
        self.system_prompt = system_prompt
        self.task_context = task.context() if task is not None else ""
        # 对话轮次：本 Agent 实例经历过的 run() 次数（跨步累计）
        self.turn_count = 0

        # system 消息 = 调用方给的提示词 + 任务上下文。
        # 任务上下文来自磁盘上的持久状态，因此"新进程 + 新 Agent"也能
        # 无缝接续之前的工作——这就是停止/恢复能力的实现点。
        system_content = system_prompt
        if task is not None:
            system_content = (system_prompt + "\n\n" + task.context()).strip()
            task.record("task_context_loaded", f"system prompt 注入任务 {task.id}")

        # messages 是本次进程内的对话历史（协议层面的消息流）。
        # 注意区分两层状态：messages 是"对话"，task 是"工作"——
        # 解释性往来可以留在 messages 里，只有改变工作本身的事实
        # 才会进入 task（对应 README 的"工作与解释是两回事"）。
        self.messages: list[dict] = (
            [{"role": "system", "content": system_content}] if system_content else []
        )

    def register(self, fn: Callable) -> None:
        """把一个 Python 函数注册为模型可调用的工具。"""
        if fn.__name__ in self.tools:
            raise ValueError(f"工具重复注册: {fn.__name__}")
        self.tools[fn.__name__] = fn
        self._schemas.append(_schema_of(fn))

    def run(
        self,
        user_input: str,
        on_thinking: Optional[Callable[[str], None]] = None,
        on_answer_delta: Optional[Callable[[str], None]] = None,
    ) -> str:
        """处理一条用户输入，返回模型的最终文本回答。

        统一走流式：无论是否传回调，循环机制完全相同——
        on_thinking / on_answer_delta 只是把增量转发给调用方做展示
        （CLI 用它实现打字机效果），不传就是静默累积。
        """
        self.messages.append({"role": "user", "content": user_input})
        if self.task is not None:
            self.task.record("user_input", user_input)
            self.task.save()  # 先落盘再干活：进程崩溃也不丢这条输入

        # 本轮 run 的累计开销（可能经历多次 LLM 调用，跨轮累加），
        # 供调用方做成本核算；也随任务事件落盘。
        # prompt_breakdown 是提示词的分类估算（见 tokens.py），
        # 展示时会按真实总量校准，这里累加的是原始估算值。
        # 轮次 = 本会话第几次 run；步数 = 本次 run 内的 LLM 调用数。
        # 缓存口径：服务端没返回 prompt_tokens_details 的调用按 0 命中
        # 计入未命中——宁可保守，也保证 命中+未命中 恒等于输入总量
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
            "prompt_breakdown": dict.fromkeys(tokens.CATEGORIES, 0),
        }

        for _ in range(self.max_turns):
            reasoning_started = False  # 供回调做"只打印一次横幅"的判断
            content_parts: list[str] = []
            # 流式协议下，工具调用是分片到达的：同一调用的参数 JSON
            # 会被拆成多个 delta 追加式下发，必须按 index 聚合
            tool_calls_acc: dict[int, dict] = {}
            usage = None

            start = time.monotonic()
            # 快照此刻的消息列表：用量返回后按它做分类估算，
            # 避免["回答轮"追加的新消息]混进本次调用的提示词里
            prompt_snapshot = list(self.messages)
            self.last_stats["llm_calls"] += 1
            stream = self.llm.chat(self.messages, tools=self._schemas or None, stream=True)
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
                if thinking:
                    if not reasoning_started:
                        reasoning_started = True
                    if on_thinking:
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
                # 分类占比按"本次调用发出前的消息快照"估算后累加
                estimated = tokens.breakdown(
                    self.system_prompt, self.task_context, self._schemas, prompt_snapshot
                )
                for category, value in estimated.items():
                    self.last_stats["prompt_breakdown"][category] += value

            if tool_calls_acc:
                # 请求了工具：先把这条 assistant 消息（含 tool_calls）放回历史，
                # 协议要求紧随其后的必须是每个工具调用对应的 tool 消息。
                ordered = [tool_calls_acc[i] for i in sorted(tool_calls_acc)]
                self.messages.append(
                    {
                        "role": "assistant",
                        "content": "".join(content_parts),
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
                    }
                )
                self.last_stats["tool_calls"] += len(ordered)
                for tc in ordered:
                    self._execute(call_id=tc["id"], name=tc["name"], arguments=tc["arguments"])
                continue

            # 模型不再请求工具，说明它认为可以直接回答了，循环结束。
            answer = "".join(content_parts)
            self.messages.append({"role": "assistant", "content": answer})
            if self.task is not None:
                self.task.record("final_answer", answer)
                # 先刷新状态再记录 usage：状态评估的调用也是真实开销，
                # 应当包含在本次 run 的成本核算里
                self._refresh_state(answer)
                self.task.record(
                    "usage",
                    f"{self.last_stats['seconds']:.1f}s, "
                    f"prompt={self.last_stats['prompt_tokens']}, "
                    f"completion={self.last_stats['completion_tokens']}, "
                    f"total={self.last_stats['total_tokens']}",
                )
                self.task.save()
            return answer

        # 防御性上限：模型可能陷入"永远在调工具"的死循环，
        # max_turns 保证循环一定终止。报告/干预机制成熟后由人接管。
        raise RuntimeError(
            f"agent 超过最大循环次数（{self.max_turns} 轮）仍未给出最终回答"
        )

    def _refresh_state(self, latest_answer: str) -> None:
        """每轮结束后让模型重新评估任务状态。

        与"目标前置"的旧设计相反：目标不建任务时定死，而是随对话
        逐步成形、演化，甚至被推翻。模型每轮重新输出它对
        目标 / 状态（未完成或已完成）/ 进展的最新理解，写回任务。

        输出约定为 JSON；解析失败则整体保留原状态——宁可这轮不更新，
        也不用坏数据覆盖。这次调用同样流式并计入成本核算。
        """
        if self.task is None:
            return
        prompt = (
            "请根据任务上下文和最近的对话，重新评估这个任务，"
            "只输出一个 JSON 对象（不要代码块围栏、不要解释）：\n"
            '{"goal": "当前对任务目标的理解；若对话尚未形成明确目标则为空字符串",\n'
            ' "status": "in_progress 或 done（仅当任务目标已达成才填 done）",\n'
            ' "summary": "当前进展摘要，Markdown 列表，不超过 6 行；'
            '闲聊或与任务无关的内容不必写进进展"}\n\n'
            f"{self.task.context()}\n\n最近一次回答：{latest_answer[:2000]}"
        )
        try:
            summary_parts: list[str] = []
            # 状态评估同样走流式（为了拿 usage 计入成本核算）；
            # 不需要工具，显式传 None 防止把工具 schema 带进这次
            # 与工作无关的调用
            start = time.monotonic()
            state_messages = [{"role": "user", "content": prompt}]
            usage = None
            for chunk in self.llm.chat(state_messages, tools=None, stream=True):
                if getattr(chunk, "usage", None):
                    usage = chunk.usage
                if getattr(chunk, "choices", None):
                    piece = chunk.choices[0].delta.content
                    if piece:
                        summary_parts.append(piece)
            self.last_stats["seconds"] += time.monotonic() - start
            if usage is not None:
                self._accumulate_usage(usage)
                estimated = tokens.breakdown("", "", [], state_messages)
                for category, value in estimated.items():
                    self.last_stats["prompt_breakdown"][category] += value
            raw = "".join(summary_parts).strip()
            # 容错：剥掉模型偶尔坚持要加的 ```json 围栏
            if raw.startswith("```"):
                raw = raw.strip("`")
                raw = raw[raw.find("{"):] if "{" in raw else ""
            state = json.loads(raw) if raw else {}
        except Exception:  # noqa: BLE001——状态评估失败不应影响主流程
            return  # 保留原状态，本轮不更新
        self.task.apply_state(
            goal=state.get("goal"),
            status=state.get("status"),
            summary=state.get("summary"),
        )

    def _accumulate_usage(self, usage) -> None:
        """把一次调用的 usage 累加进 last_stats。

        所有 LLM 调用（工具轮/回答轮/摘要生成）都必须经过这里，
        否则就会漏计——此前摘要调用漏掉了缓存与思考 token，
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

    def _execute(self, call_id: str, name: str, arguments: str) -> None:
        """执行单个工具调用，并把结果作为 tool 消息追加到历史。

        参数是拆开的协议字段而不是整个 tool_call 对象：调用方
        （流式聚合或测试）手里就是这三个值，没必要再造一层包装。

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

        # 每次工具调用与结果都进入任务历史——
        # 这就是"Execution history"的最小形态，crash 后据此追溯
        if self.task is not None:
            self.task.record("tool_call", f"{name}({arguments})")
            self.task.record("tool_result", f"{name} -> {result[:500]}")
            # 变更类工具（写文件/改文件/执行命令）额外记一条完整审计：
            # arguments 含全部新内容，且不截断——"agent 到底改了什么"
            # 必须能从 task.json 完整还原，这是"审计换权限"的兑现
            if name in AUDITED_TOOLS:
                self.task.record("file_change", f"{name} {arguments}")
            self.task.save()

        self.messages.append(
            {"role": "tool", "tool_call_id": call_id, "content": result}
        )
