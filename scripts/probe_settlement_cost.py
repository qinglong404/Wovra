"""结算调用的缓存账探针：把"每轮结算该喂什么"从算式变成读数。

背景（2026-09-17 讨论）：V4 里"每轮写一段总结"有两种喂法——

* **甲**：喂全量装配快照 + 尾部追加指令。它是工作请求的**字节延长**，
  理论上整段命中，只付新增字节；
* **乙**：只喂"已有段落 + 本轮原文"。这是**另起一条流**，与工作轨迹
  没有共享前缀，本轮原文按未命中价重付。

两者的差价不在"会不会破坏前缀"（都不破坏——都不动装配字节），而在
**命中那部分要不要按命中价付费**。而"命中价 ÷ 未命中价"（记作 h）
是这个仓库唯一没有记过的量，于是"哪个便宜"一直只能猜。

本脚本用受控前缀把两件事测出来，不依赖任何真实会话（真实会话中途改
系统提示词/工具数组会把前缀打断，读数不可用于方案对比）：

    1 冷发甲流前缀    → 全 miss（首现，写入缓存条目）
    2 甲流纯追加      → 期望 cached≈前缀、miss≈新增（甲便宜的前提）
    3 甲流再追加      → 同上，验证跨次复用的稳定性
    4 冷发乙流前缀    → 全 miss（另一条流，与甲流无共享前缀）
    5 乙流原样重发    → 期望 cached≈全量（乙的归档段第二次起才免费）

同时 dump 一次原始响应体：本端点（commandcode 网关）的 usage 带
`cache_write_tokens` / `cache_creation_input_tokens`（Anthropic 式缓存
字段），需要确认它报的是什么，以及有没有直接的金额字段。

用法：

    uv run --no-sync python scripts/probe_settlement_cost.py            # 24K 前缀
    uv run --no-sync python scripts/probe_settlement_cost.py --k 50     # 50K 前缀
    uv run --no-sync python scripts/probe_settlement_cost.py --dump     # 附原始响应体

成本：默认约 2×k K 输入 token（多数为命中读），可忽略；耗时约 1 分钟。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))          # 复用同目录仪器
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cache_ttl_probe import build_prefix  # noqa: E402
from wovra.llm import LLM  # noqa: E402

TAIL = "\n\n只回答两个字：收到"
NEXT = "再回答两个字：好的"
NEXT2 = "再回答两个字：可以"


def _usage_of(resp) -> dict:
    u = resp.usage
    detail = getattr(u, "prompt_tokens_details", None)
    return {
        "prompt": int(u.prompt_tokens or 0),
        "cached": int(getattr(detail, "cached_tokens", 0) or 0),
        "write": getattr(detail, "cache_write_tokens", None),
        "ccreate": getattr(u, "cache_creation_input_tokens", None),
        "completion": int(u.completion_tokens or 0),
        "reasoning": int(
            (getattr(u, "completion_tokens_details", None) or {})
            and getattr(u.completion_tokens_details, "reasoning_tokens", 0) or 0
        ),
    }


def call(llm: LLM, label: str, messages: list[dict], dump: bool = False) -> dict:
    resp = llm.chat(messages)
    row = _usage_of(resp)
    row["label"] = label
    row["miss"] = max(0, row["prompt"] - row["cached"])
    row["rate"] = row["cached"] / max(row["prompt"], 1)
    print(
        f"{label:<16} prompt {row['prompt']:>8,}　cached {row['cached']:>8,}"
        f"　miss {row['miss']:>8,}　命中 {row['rate']:>6.1%}"
        f"　写 {row['write']}　completion {row['completion']:,}",
        flush=True,
    )
    if dump:
        print("[原始 usage] " + json.dumps(resp.model_dump().get("usage"), ensure_ascii=False))
        meta = resp.model_dump().get("metadata")
        if meta:
            print("[metadata] " + json.dumps(meta, ensure_ascii=False)[:600])
    return row


def cut_probe(llm: LLM, seg_k: int = 3) -> None:
    """九段受控上下文，测三种"截断"形态的命中（用户口径：全量 vs 砍开头）。

    把上下文想成 1 2 3 … 9 九段：
      * 全量      1..9 + 指令 —— 新指令只能接在**尾部**，故整段是已发字节的延长；
      * 砍尾部    1..7 + 指令 —— 仍然是某个已发序列的**前缀**；
      * 砍开头    5..9 + 指令 —— 开头就与已发序列分叉（这就是"截断输入"）。
    缓存只能从第 0 个 token 起逐字节匹配，故前两者命中、后者 0——
    本实验就是把这条机制变成读数（末行重发砍开头的那份，证明 0 不是配置坏了）。
    """
    segs = [
        build_prefix(seg_k * 1000).replace("模块", f"第{i}段-")
        for i in range(1, 10)
    ]

    def msg(parts: list[str], tail: str) -> list[dict]:
        return [{"role": "user", "content": "\n".join(parts) + "\n" + tail}]

    print(f"\n== 截断形态实验（每段 ~{seg_k}K tok，共 9 段）==")
    call(llm, "冷发 1..9（全量）", msg(segs, TAIL), True)
    call(llm, "砍尾部 1..7", msg(segs[:7], TAIL))
    call(llm, "砍开头 5..9", msg(segs[4:], TAIL))
    call(llm, "砍开头 5..9 重发", msg(segs[4:], TAIL))


def main() -> None:
    ap = argparse.ArgumentParser(description="结算调用缓存账探针")
    ap.add_argument("--k", type=int, default=24, help="每条流的受控前缀体量（K tok）")
    ap.add_argument("--dump", action="store_true", help="附原始 usage / metadata")
    ap.add_argument("--cut", action="store_true", help="只跑截断形态实验")
    args = ap.parse_args()

    llm = LLM()
    print(f"模型={llm.model}　端点={llm.base_url or '(默认)'}　前缀 ~{args.k}K tok\n")
    if args.cut:
        cut_probe(llm)
        return

    p = build_prefix(args.k * 1000)          # 甲流（= 工作轨迹的模拟体）
    q = build_prefix(args.k * 1000 // 2)     # 乙流（另一条流，体量刻意不同以便辨认）
    q = q.replace("模块", "另流")             # 逐字节与 p 不同：确保不是同一条前缀

    m1 = [{"role": "user", "content": p + TAIL}]
    m2 = m1 + [{"role": "assistant", "content": "收到"},
               {"role": "user", "content": NEXT}]
    m3 = m2 + [{"role": "assistant", "content": "好的"},
               {"role": "user", "content": NEXT2}]
    n1 = [{"role": "user", "content": q + TAIL}]

    rows = [
        call(llm, "1 冷发甲流", m1, args.dump),
        call(llm, "2 甲流纯追加", m2),
        call(llm, "3 甲流再追加", m3),
        call(llm, "4 冷发乙流", n1),
        call(llm, "5 乙流原样重发", n1),
    ]

    jia = [r for r in rows if r["label"].startswith(("2", "3"))]
    yi = [r for r in rows if r["label"].startswith("5")]
    print("\n── 结论口径（等效未命中 tok = miss + h × cached）──")
    if jia:
        print("甲（纯追加）：" + "；".join(
            f"miss {r['miss']:,} + h×{r['cached']:,}" for r in jia
        ) + "　→ 命中的那部分就是甲的全部代价")
    if yi:
        r = yi[0]
        print(f"乙（原样重发）：miss {r['miss']:,} + h×{r['cached']:,}"
              "　→ 首次发新流时 miss≈全量，归档段要第二次起才免费")
    print("判据：甲 < 乙 ⟺ h × C < R（C=上下文体量，R=本轮原文体量）"
          "；h 由本表的 hit/miss 结构 + 端点定价表确定。")


if __name__ == "__main__":
    main()
