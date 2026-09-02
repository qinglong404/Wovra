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
from pathlib import Path
from typing import Callable, Optional

from .llm import LLM

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
        on_tool_call: Optional[Callable[[str, dict], None]] = None,
        on_tool_result: Optional[Callable[[str, str], None]] = None,
    ) -> None:
        self.llm = llm or LLM()
        self.max_turns = max_turns
        # 观察性钩子：Wovra 关注"可观察"，示例用它把工具调用过程打印出来。
        # 正式的事件/报告系统是路线图后面阶段的内容。
        self.on_tool_call = on_tool_call
        self.on_tool_result = on_tool_result

        self.tools: dict[str, Callable] = {}
        self._schemas: list[dict] = []
        for fn in tools:
            self.register(fn)

        # messages 是当前会话的完整历史。阶段 1 不做持久化和折叠，
        # 直接放在内存里；这个列表未来会演变成"任务状态"的一部分。
        self.messages: list[dict] = (
            [{"role": "system", "content": system_prompt}] if system_prompt else []
        )

    def register(self, fn: Callable) -> None:
        """把一个 Python 函数注册为模型可调用的工具。"""
        if fn.__name__ in self.tools:
            raise ValueError(f"工具重复注册: {fn.__name__}")
        self.tools[fn.__name__] = fn
        self._schemas.append(_schema_of(fn))

    def run(self, user_input: str) -> str:
        """处理一条用户输入，返回模型的最终文本回答。"""
        self.messages.append({"role": "user", "content": user_input})

        for _ in range(self.max_turns):
            response = self.llm.chat(self.messages, tools=self._schemas or None)
            message = response.choices[0].message

            # 模型不再请求工具，说明它认为可以直接回答了，循环结束。
            if not message.tool_calls:
                answer = message.content or ""
                self.messages.append({"role": "assistant", "content": answer})
                return answer

            # 请求了工具：先把这条 assistant 消息（含 tool_calls）放回历史，
            # 协议要求紧随其后的必须是每个工具调用对应的 tool 消息。
            self.messages.append(
                {
                    "role": "assistant",
                    "content": message.content or "",
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments or "{}",
                            },
                        }
                        for tc in message.tool_calls
                    ],
                }
            )
            for tool_call in message.tool_calls:
                self._execute(tool_call)

        # 防御性上限：模型可能陷入"永远在调工具"的死循环，
        # max_turns 保证循环一定终止。报告/干预机制成熟后由人接管。
        raise RuntimeError(
            f"agent 超过最大循环次数（{self.max_turns} 轮）仍未给出最终回答"
        )

    def _execute(self, tool_call) -> None:
        """执行单个工具调用，并把结果作为 tool 消息追加到历史。

        任何失败（参数不是 JSON、工具名不存在、函数抛异常）都
        不抛出，而是把错误文本作为工具结果回传给模型——让模型
        自己看到错误并尝试纠正，循环才不会因为一次失败而中断。
        """
        name = tool_call.function.name

        if self.on_tool_call:
            self.on_tool_call(name, tool_call.function.arguments)

        try:
            arguments = json.loads(tool_call.function.arguments or "{}")
        except json.JSONDecodeError as error:
            result = f"工具参数不是合法 JSON: {error}"
        else:
            fn = self.tools.get(name)
            if fn is None:
                # 模型可能臆造不存在的工具名，如实告知它
                result = f"未知工具: {name}，可用工具: {list(self.tools)}"
            else:
                try:
                    result = fn(**arguments)
                except Exception as error:  # noqa: BLE001——错误要回传给模型而不是中断循环
                    result = f"工具执行出错: {error!r}"

        # 协议要求工具结果是字符串；非字符串（如 dict/list）序列化后回传
        if not isinstance(result, str):
            result = json.dumps(result, ensure_ascii=False, default=str)

        if self.on_tool_result:
            self.on_tool_result(name, result)

        self.messages.append(
            {"role": "tool", "tool_call_id": tool_call.id, "content": result}
        )


# ---- 内置的安全示例工具 -------------------------------------------------
# 放在 agent.py 里作为最简单的参考实现；路径类工具都把访问范围
# 限制在项目根目录内，避免模型读到项目之外的东西。


def _safe_path(relative: str) -> Path:
    """把相对路径解析到项目根目录内，越界直接报错。"""
    root = Path(__file__).resolve().parent.parent.parent
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise ValueError(f"路径越界，只允许访问项目目录内的文件: {relative}")
    return path


def list_files(directory: str = ".") -> list[str]:
    """列出项目内某个目录下的文件和子目录（不含递归）。"""
    path = _safe_path(directory)
    return sorted(p.name + ("/" if p.is_dir() else "") for p in path.iterdir())


def read_file(path: str) -> str:
    """读取项目内一个文本文件的内容（最多前 4000 字符）。"""
    target = _safe_path(path)
    text = target.read_text(encoding="utf-8")
    return text[:4000] + ("\n...(已截断)" if len(text) > 4000 else "")


def get_current_time() -> str:
    """获取当前本地时间（ISO 格式）。"""
    from datetime import datetime

    return datetime.now().isoformat(timespec="seconds")
