"""通过 OpenAI 协议调用大模型 API 的最小示例。

兼容任何 OpenAI 协议的服务（OpenAI、DeepSeek、Moonshot、Qwen、vLLM、Ollama 等），
只需修改 BASE_URL 和 MODEL。

配置来源（环境变量）：
    OPENAI_API_KEY  -- API 密钥
    OPENAI_BASE_URL -- API 地址（默认 OpenAI 官方）
    WOVRA_MODEL     -- 模型名（默认 gpt-4o-mini）

运行：
    uv run python examples/basic_call.py
"""

import os

from openai import OpenAI

BASE_URL = os.environ.get("OPENAI_BASE_URL")  # 例如 https://api.deepseek.com/v1
API_KEY = os.environ.get("OPENAI_API_KEY", "")
MODEL = os.environ.get("WOVRA_MODEL", "gpt-4o-mini")


def make_client() -> OpenAI:
    if not API_KEY:
        raise SystemExit(
            "请先设置环境变量 OPENAI_API_KEY，例如：\n"
            "  export OPENAI_API_KEY=sk-...\n"
            "  export OPENAI_BASE_URL=https://api.deepseek.com/v1  # 可选\n"
            "  export WOVRA_MODEL=deepseek-chat                    # 可选"
        )
    return OpenAI(api_key=API_KEY, base_url=BASE_URL)


def chat(client: OpenAI, user_message: str) -> str:
    """一次普通的对话补全，返回完整回复文本。"""
    response = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": "你是一个简洁的助手。"},
            {"role": "user", "content": user_message},
        ],
    )
    return response.choices[0].message.content or ""


def chat_stream(client: OpenAI, user_message: str) -> None:
    """流式输出：每收到一小段文本就打印。"""
    stream = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": "你是一个简洁的助手。"},
            {"role": "user", "content": user_message},
        ],
        stream=True,
    )
    for chunk in stream:
        piece = chunk.choices[0].delta.content or ""
        print(piece, end="", flush=True)
    print()


def main() -> None:
    client = make_client()

    print(f"模型：{MODEL}")
    print("=== 普通调用 ===")
    print(chat(client, "用一句话介绍你自己。"))

    print("\n=== 流式调用 ===")
    chat_stream(client, "从 1 数到 5，用顿号分隔。")


if __name__ == "__main__":
    main()
