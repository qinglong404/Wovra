"""流式调用的独立示例：逐块接收、边收边打印，并拼出完整回复。

和 basic_call.py 里的 chat_stream 相比，这里把流式当成主角，
额外演示三个实战要点：

    * delta.content 是"增量"而不是全文，需要自己拼接
    * 最后一个 chunk 可能不带 choices（只带 token 用量），要判空跳过
    * chunk.choices[0].finish_reason 在最后一块变为 "stop"

思考型模型（GLM 等）会先流出一段 reasoning_content 再流出正文，
本示例统一用 wovra.llm.reasoning_of 兼容取出，不影响正文打印。

运行（自动从项目根目录的 .env 读取配置）：
    uv run python examples/streaming_call.py
"""

import os

from dotenv import load_dotenv
from openai import OpenAI

from wovra.llm import reasoning_of

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


def chat_stream(client: OpenAI, user_message: str) -> str:
    """流式调用：边收边打印增量，返回拼接后的完整回复。"""
    stream = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": "你是一个简洁的助手。"},
            {"role": "user", "content": user_message},
        ],
        stream=True,
        # 让服务端在最后附带 token 用量；个别服务不支持时可删掉这一行
        stream_options={"include_usage": True},
    )

    pieces: list[str] = []
    finish_reason = None
    usage = None
    in_thinking = False

    for chunk in stream:
        # 结尾的 usage-only chunk 没有 choices，直接取用量后跳过
        if not chunk.choices:
            usage = getattr(chunk, "usage", None)
            continue

        delta = chunk.choices[0].delta
        reasoning = reasoning_of(delta)
        if reasoning:
            # 进入思考段时打一次标记；思考结束后换行再输出正文
            if not in_thinking:
                in_thinking = True
                print("[思考] ", end="", flush=True)
            print(reasoning, end="", flush=True)
            continue
        elif in_thinking:
            in_thinking = False
            print("\n")

        piece = delta.content or ""
        pieces.append(piece)
        print(piece, end="", flush=True)

        if chunk.choices[0].finish_reason:
            finish_reason = chunk.choices[0].finish_reason

    print()
    if finish_reason:
        print(f"（结束原因：{finish_reason}）")
    if usage:
        print(f"（token 用量：输入 {usage.prompt_tokens}，输出 {usage.completion_tokens}）")
    return "".join(pieces)


def main() -> None:
    client = make_client()

    print(f"模型：{MODEL}")
    print("=== 流式调用（逐块输出） ===")
    reply = chat_stream(client, "用一句话介绍一下流式输出的好处。")

    print(f"\n完整回复共 {len(reply)} 个字符，与逐块打印的内容一致。")


if __name__ == "__main__":
    main()
