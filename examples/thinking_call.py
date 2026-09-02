"""演示如何获取并输出模型的"思考内容"（reasoning content）。

GLM 等推理模型在回答前会先生成一段思考过程。走 OpenAI 协议时，
思考内容不在 `message.content` 里，而是放在额外的 `reasoning_content`
字段中；流式调用时则出现在 `delta.reasoning_content` 里。

要点：
    * 非流式：message.reasoning_content 是思考，message.content 是最终回答
    * 流式：先收到一串带 reasoning_content 的 delta，再收到带 content 的 delta
    * `thinking` 参数可以控制是否开启思考（extra_body 会透传给服务端）

运行：
    uv run python examples/thinking_call.py
"""

import os

from dotenv import load_dotenv
from openai import OpenAI
from wovra.llm import reasoning_of as get_reasoning

# 从项目根目录（examples/ 的上一级）加载 .env；已存在的环境变量不会被覆盖
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))

BASE_URL = os.environ.get("Wovra_BASE_URL")
API_KEY = os.environ.get("Wovra_API_KEY", "")
MODEL = os.environ.get("Wovra_MODEL", "gpt-4o-mini")


def make_client() -> OpenAI:
    if not API_KEY:
        raise SystemExit("请先在项目根目录的 .env 中填写 Wovra_API_KEY")
    return OpenAI(api_key=API_KEY, base_url=BASE_URL)


def chat_with_thinking(client: OpenAI, user_message: str) -> None:
    """非流式调用：一次性拿到完整的思考过程和最终回答。"""
    response = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": user_message}],
        # 透传 OpenAI 协议之外的自定义参数，控制思考开关：
        extra_body={"thinking": {"type": "enabled"}},  # 关闭用 {"type": "disabled"}
    )
    message = response.choices[0].message

    thinking = get_reasoning(message)
    if thinking:
        print("--- 思考过程 ---")
        print(thinking)
        print("--- 最终回答 ---")
    print(message.content or "")


def chat_with_thinking_stream(client: OpenAI, user_message: str) -> None:
    """流式调用：思考内容先流出来，随后才是正式回答。"""
    stream = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": user_message}],
        stream=True,
        extra_body={"thinking": {"type": "enabled"}},
    )
    in_thinking = False
    for chunk in stream:
        delta = chunk.choices[0].delta
        reasoning = get_reasoning(delta)
        if reasoning:
            if not in_thinking:
                print("--- 思考过程 ---")
                in_thinking = True
            print(reasoning, end="", flush=True)
        elif delta.content:
            if in_thinking:
                print("\n--- 最终回答 ---")
                in_thinking = False
            print(delta.content, end="", flush=True)
    print()


def main() -> None:
    client = make_client()

    print(f"模型：{MODEL}")
    print("=== 非流式（含思考） ===")
    chat_with_thinking(client, "9.11 和 9.8 哪个大？")

    print("\n=== 流式（含思考） ===")
    chat_with_thinking_stream(client, "一个袋子里有 3 个红球，取出 1 个后放入 2 个蓝球，再随机取出 1 个，取到蓝球的概率是多少？")


if __name__ == "__main__":
    main()
