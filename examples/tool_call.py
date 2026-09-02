"""演示最小 Agent 运行时：模型如何多轮调用工具直到完成任务。

运行：
    uv run python examples/tool_call.py

这次提问模型必须依赖工具才能回答（它自己不知道当前时间，
也不知道项目里有什么文件），可以观察到完整的
"LLM → tool call → 工具执行 → 结果回传 → LLM" 循环。
"""

from wovra.agent import Agent, get_current_time, list_files, read_file
from wovra.llm import LLM


def on_tool_call(name: str, arguments: str) -> None:
    """工具即将执行时被调用：展示模型"想做"什么。"""
    print(f"[调用工具] {name}({arguments})")


def on_tool_result(name: str, result: str) -> None:
    """工具执行完毕时被调用：展示回传给模型的结果（截断显示）。"""
    preview = result if len(result) <= 200 else result[:200] + " ...(截断)"
    print(f"[工具结果] {name} -> {preview}")


def main() -> None:
    agent = Agent(
        # 不传 llm 则自动从 .env 读取配置
        system_prompt="你是一个简洁的助手，需要时使用提供的工具获取真实信息。",
        tools=[get_current_time, list_files, read_file],
        on_tool_call=on_tool_call,
        on_tool_result=on_tool_result,
    )

    print(f"模型：{agent.llm.model}\n")

    # 这个问题需要：查时间 → 看目录 → 读文件，至少三轮工具调用
    answer = agent.run(
        "现在几点了？然后看看项目根目录下有哪些文件，"
        "挑出记录 Python 版本的那个文件，告诉我它的内容。"
    )

    print(f"\n=== 最终回答 ===\n{answer}")


if __name__ == "__main__":
    main()
