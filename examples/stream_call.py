"""演示通过 wovra.llm.LLM 的流式调用：分块接收、边收边打印、统计 tokens。

与 examples/basic_call.py 里裸用 OpenAI SDK 不同，本示例走项目自己的
LLM 封装（与 wovra 内部行为一致），可以额外拿到：

    * 分块迭代：stream=True 时 chat() 返回分块生成器，逐段输出
    * LLM.chat 会自动附带 stream_options.include_usage，
      最后一个分块带 usage 统计，可直接核算 tokens 消耗
    * 推理模型的分块里可能混有思考内容（reasoning_content），
      用 reasoning_of 兼容取出；本示例只展示正式回答，思考演示见 thinking_call.py

运行（自动从项目根目录的 .env 读取配置）：
    uv run python examples/stream_call.py
"""

from wovra.llm import LLM, reasoning_of


def stream_chat(llm: LLM, messages: list[dict]) -> None:
    """流式调用：每收到一小段文本就打印，结束后输出 tokens 统计。"""
    stream = llm.chat(messages, stream=True)

    n_pieces = 0
    for chunk in stream:
        # include_usage 开启时，最后一个分块只带 usage、没有 choices
        if chunk.usage is not None:
            u = chunk.usage
            print(
                f"\n[tokens] 输入 {u.prompt_tokens}，"
                f"输出 {u.completion_tokens}，共 {u.total_tokens}"
            )
        for choice in chunk.choices:
            delta = choice.delta
            if reasoning_of(delta):
                # 推理模型的思考片段：跳过，不混入正式回答
                continue
            piece = delta.content or ""
            if piece:
                print(piece, end="", flush=True)
                n_pieces += 1

    if n_pieces == 0:
        print("(未收到任何文本分块)")
    else:
        print(f"\n[{n_pieces} 个文本分块，逐段打印完成]")


def main() -> None:
    llm = LLM()  # 不传参数则自动从 .env 读取配置
    print(f"模型：{llm.model}\n")

    stream_chat(
        llm,
        [
            {"role": "system", "content": "你是一个简洁的助手。"},
            {"role": "user", "content": "用三句话介绍流式输出的好处。"},
        ],
    )


if __name__ == "__main__":
    main()
