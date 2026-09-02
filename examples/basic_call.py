"""通过 OpenAI 协议调用大模型 API 的最小示例。

兼容任何 OpenAI 协议的服务（OpenAI、DeepSeek、Moonshot、Qwen、vLLM、Ollama 等），
只需修改 BASE_URL 和 MODEL。

配置来源（项目根目录的 .env 文件或环境变量）：
    Wovra_API_KEY   -- API 密钥
    Wovra_BASE_URL  -- API 地址（OpenAI 协议兼容服务）
    Wovra_MODEL     -- 模型名（默认 gpt-4o-mini）

运行（自动从项目根目录的 .env 读取配置）：
    uv run python examples/basic_call.py
"""

import os

from dotenv import load_dotenv
from openai import OpenAI

# 从项目根目录（examples/ 的上一级）加载 .env；已存在的环境变量不会被覆盖
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))

BASE_URL = os.environ.get("Wovra_BASE_URL")  # 例如 https://api.deepseek.com/v1
API_KEY = os.environ.get("Wovra_API_KEY", "")
MODEL = os.environ.get("Wovra_MODEL", "gpt-4o-mini")


def make_client() -> OpenAI:
    if not API_KEY or API_KEY.startswith("sk-xxxx"):
        raise SystemExit(
            "请先在项目根目录的 .env 中填写 Wovra_API_KEY：\n"
            "  Wovra_API_KEY=sk-...\n"
            "  Wovra_BASE_URL=https://api.deepseek.com/v1  # 可选\n"
            "  Wovra_MODEL=deepseek-chat                    # 可选"
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
