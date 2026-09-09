"""输出上限探针：运行时从不传 max_tokens，截断点 = 服务端默认值。

对比两次调用：
1. 不带 max_tokens —— 看服务端默认在哪掐（finish=length 的位置）
2. 带 max_tokens=32000 —— 看端点是否接受参数、能否突破默认

成本：~30K 输出 token 级，几分钱。用法：python scripts/max_output_probe.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wovra.llm import LLM  # noqa: E402


def probe(tag: str, max_tokens: object = None) -> None:
    llm = LLM()
    prompt = (
        "请写一篇长文《从零实现一个网页文本编辑器》，要求内容详尽充实、"
        "连续输出不要中断，尽量写到一万字以上，不要总结不要提前收尾。"
    )
    kwargs = {} if max_tokens is None else {"max_tokens": max_tokens}
    t0 = time.monotonic()
    resp = llm.chat([{"role": "user", "content": prompt}], stream=False, **kwargs)
    elapsed = time.monotonic() - t0
    choice = resp.choices[0]
    usage = resp.usage
    text = getattr(choice, "message", None)
    content = (getattr(text, "content", None) or "") if text else ""
    print(f"[{tag}] finish={choice.finish_reason} 输出字符={len(content):,} "
          f"耗时={elapsed:.1f}s")
    print(f"        usage: completion={usage.completion_tokens:,} "
          f"prompt={usage.prompt_tokens:,} 总={usage.total_tokens:,}")
    if content:
        tail = content.strip().splitlines()[-1] if content.strip() else ""
        print(f"        末尾数字: {tail!r}")


if __name__ == "__main__":
    probe("默认（无 max_tokens）")
    probe("max_tokens=32000")
