"""缓存存活曲线探针：把 WOVRA_CACHE_TTL 从猜想变成数字。

背景（D' 组 20260908-231729-6d526e 实测）：消息流纯追加、装配无罪，
但服务端只认了 53.5% 的命中——TTL 阈值拟合全部失败，说明不是干净的
过期时间。本脚本用受控实验画存活曲线：同一份大前缀，按阶梯间隔重发，
读服务端返回的 cached_tokens。

用途备忘（2026-09-09 补充，套餐制下双 purpose）：
1. TTL 实测：看 cached_tokens 翻转边界是否尖锐稳定（当前仅有 D 组
   TTL≈60s 的拟合证据）。
2. 配额对账：用户套餐不显示任何 token 明细（只有 5h/7d/30d 剩余量），
   账单对账路径作废——跑前后各记一次"剩余量"，配额扣减 vs 上报
   token 总量（miss + cached×折扣）的比值即真实扣减规则；比值恒定
   则上报可信，波动则口径存疑。本脚本是唯一的行为 ground truth 来源。

方法说明：每次探针的 prefill 会重写缓存，所以第 k 次探针测到的是
"距上一次同前缀 prefill 间隔 X 秒后的存活率"——与生产装配模式一致
（每次调用都在重写前缀）。

用法：python scripts/cache_ttl_probe.py [--sizes 10,30]
成本：默认曲线 ~30K tok 前缀 × 7 档 ≈ 21 万输入 token，GLM 半价下
约几毛钱。--sizes 可选小前缀先试跑。

注意：跑之前确认 .env 已配置（Wovra_API_KEY / Wovra_BASE_URL /
Wovra_MODEL）。全程约 20 分钟（阶梯等待是主体）。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wovra.llm import LLM  # noqa: E402

# 阶梯间隔（秒）：覆盖"快步进慢生成"的真实 pacing 区间
DELAYS = [0, 10, 30, 60, 120, 300, 600]


def build_prefix(k_tokens: int) -> str:
    """确定性大前缀：结构化重复文本，tokenizer 友好、逐字节恒定。"""
    block = (
        "模块 {i}：这一节记录缓存存活探针的固定上下文内容，用于让服务端"
        "为这段前缀建立缓存条目。内容必须逐字节恒定，任何变化都会使前缀"
        "失配。第 {i} 段包含一个数列 {seq} 以及一行收尾说明：以上内容仅"
        "用于占据前缀体量，不含任何任务指令。\n"
    )
    parts = []
    i = 0
    total = 0
    while total < k_tokens:
        seq = ",".join(str((i * 37 + j) % 1000) for j in range(40))
        text = block.format(i=i, seq=seq)
        parts.append(text)
        total += len(text) // 3  # 粗略 token 估算
        i += 1
    return "".join(parts)


def probe(llm: LLM, prefix: str) -> tuple[int, int, float]:
    """发一次同前缀请求，返回 (prompt_tok, cached_tok, 命中率)。"""
    messages = [
        {"role": "user", "content": prefix + "\n\n只回答两个字：收到"},
    ]
    resp = llm.chat(messages, stream=False)
    usage = resp.usage
    cached = getattr(getattr(usage, "prompt_tokens_details", None),
                     "cached_tokens", 0) or 0
    return usage.prompt_tokens, cached, cached / max(usage.prompt_tokens, 1)


def main() -> None:
    ap = argparse.ArgumentParser(description="服务端前缀缓存存活曲线探针")
    ap.add_argument("--sizes", default="30", help="前缀体量（K tok，逗号分档）")
    args = ap.parse_args()

    llm = LLM()
    print(f"模型={llm.model} 端点={llm.base_url or '(默认)'}")
    for k_str in args.sizes.split(","):
        k = int(k_str)
        prefix = build_prefix(k * 1000)
        print(f"\n== 前缀 ~{k}K tok，阶梯 {DELAYS} ==（预计 ~{len(DELAYS) * k // 2}K tok 成本档）")
        print(f"{'间隔(s)':>8} {'prompt':>9} {'cached':>9} {'命中率':>7}")
        for delay in DELAYS:
            if delay:
                print(f"  … 等待 {delay}s", flush=True)
                time.sleep(delay)
            p, c, rate = probe(llm, prefix)
            print(f"{delay:>8} {p:>9} {c:>9} {rate:>6.1%}", flush=True)
    print("\n解读：命中率从 1.0 开始衰减的档位即服务端缓存存活边界；"
          "衰减是阶梯状（整段存活）还是连续的（逐条驱逐）决定 pacer 策略。")


if __name__ == "__main__":
    main()
