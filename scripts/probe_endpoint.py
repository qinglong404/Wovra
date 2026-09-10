"""新端点探针：最小请求，打印逐 chunk 到达时间（区分缓冲/挂流）。

用法：.venv/bin/python scripts/probe_endpoint.py
"""
import sys
import time

from wovra.llm import LLM, reasoning_of

llm = LLM()
print(f"model={llm.model}", flush=True)
print(f"base_url={llm.base_url}", flush=True)
print("发送最小请求（一句话问候，max_tokens 由 LLM 默认=393216）…", flush=True)

t0 = time.monotonic()
stream = llm.chat(
    [{"role": "user", "content": "用一句话打个招呼。"}],
    stream=True,
)
last = t0
first = None
n_thinking = n_content = 0
usage = None
finish = None
for chunk in stream:
    now = time.monotonic()
    gap = now - last
    last = now
    if first is None:
        first = now
        print(f"[{now - t0:6.1f}s] 首个分块到达（TTFT {now - t0:.1f}s）", flush=True)
    if gap > 5:
        print(f"[{now - t0:6.1f}s] 静默 {gap:.1f}s 后继续", flush=True)
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
    if reasoning_of(delta):
        n_thinking += 1
    if delta.content:
        n_content += 1
        if n_content <= 3:
            print(f"  content: {delta.content!r}", flush=True)

dur = time.monotonic() - t0
print(f"\n完成: dur={dur:.1f}s finish={finish}", flush=True)
print(f"thinking 分块 {n_thinking} / content 分块 {n_content}", flush=True)
if usage:
    print(f"usage: {usage}", flush=True)
    extra = getattr(usage, "model_extra", None) or {}
    if extra:
        print(f"extra: {extra}", flush=True)
sys.exit(0)
