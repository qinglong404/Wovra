"""Probe：DeepSeek V4 Flash 输出预算到底卡在哪。

对比两个请求（同一模型）：
  1. 不传 max_tokens（当前运行时的做法）
  2. 传 max_tokens=65536
要求输出一长串数字。观察 finish_reason / completion / 可见 content 长度 /
reasoning 长度，判断截断是"端点默认 max_tokens 小"还是"thinking 独立配额"。
"""

import sys
import time

from wovra.llm import LLM, reasoning_of
from wovra.task import sanitize_surrogates

PROMPT = "连续输出数字，从 1 开始每行一个：1\n2\n3\n……一直数到 30000。不要解释，不要省略，不要中途停止，尽可能长地输出。"


def run(tag: str, max_tokens: int | None) -> None:
    llm = LLM()
    payload = {"model": llm.model, "messages": [
        {"role": "user", "content": PROMPT},
    ], "stream": True}
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    content: list[str] = []
    reasoning_tokens = 0
    content_tokens = 0
    usage = None
    finish = None
    start = time.monotonic()
    stream = llm._client.chat.completions.create(**payload)
    for chunk in stream:
        if getattr(chunk, "usage", None):
            usage = chunk.usage
        if not getattr(chunk, "choices", None):
            continue
        choice = chunk.choices[0]
        if getattr(choice, "finish_reason", None):
            finish = choice.finish_reason
        delta = choice.delta
        if delta is None:
            continue
        thinking = reasoning_of(delta)
        if thinking:
            reasoning_tokens += len(thinking) // 2  # 粗略字符→token
        if delta.content:
            content.append(sanitize_surrogates(delta.content))
            content_tokens += len(delta.content) // 2
    dur = time.monotonic() - start
    printed = "".join(content)
    print(f"\n===== {tag}（max_tokens={max_tokens}）=====")
    print(f"finish={finish}  dur={dur:.1f}s")
    print(f"可见 content: {len(printed)} 字符（前 60：{printed[:60]!r}）")
    if usage is not None:
        print(f"usage: {usage}")
        extra = getattr(usage, "model_extra", None) or {}
        if extra:
            print(f"extra: {extra}")
    print(f"粗算（字符/2）: thinking≈{reasoning_tokens} content≈{content_tokens}")


if __name__ == "__main__":
    run("无 max_tokens", None)
    run("max_tokens=65536", 65536)
    sys.exit(0)
