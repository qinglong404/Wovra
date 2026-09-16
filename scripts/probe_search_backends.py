"""探检索后端可用性（常驻仪器，2026-09-16，勿删）。

用途：一条命令看清"现在到底哪条检索通道是活的、结果长什么样"。GAIA 评测
（output/gaia/FINDINGS.md）那次把网络抖动写成了被测对象的缺陷，就是因为没人
记录**当时的通道快照**——这个探针补的正是那一格。

用法
    uv run --no-sync python scripts/probe_search_backends.py
    uv run --no-sync python scripts/probe_search_backends.py --query "柏林 马拉松 纪录" --max 3
    uv run --no-sync python scripts/probe_search_backends.py --providers tavily,exa

输出只打结论（每通道一行耗时/条数 + 前几条标题），**不打印任何密钥**。
"""

from __future__ import annotations

import argparse
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from wovra.tools import web as web_module  # noqa: E402

_DEFAULT_QUERY = "Wikipedia Tower Bridge"


def _probe_api(provider: str, query: str, max_results: int) -> None:
    key = web_module._search_key(provider)
    if not key:
        print(f"[{provider}] 未配置密钥（跳过）")
        return
    started = time.time()
    outcome = web_module._api_search(provider, query, max_results)
    elapsed = time.time() - started
    if isinstance(outcome, str):
        print(f"[{provider}] ✗ {elapsed:.2f}s {outcome}")
        return
    print(f"[{provider}] ✓ {elapsed:.2f}s {len(outcome)} 条")
    for title, link, snippet in outcome[:3]:
        print(f"    · {title[:60]}｜{link[:70]}")
        if snippet:
            print(f"      {snippet[:100]}")


def _probe_local(query: str, max_results: int) -> None:
    started = time.time()
    outcome = web_module._search_ddg(query, max_results)
    elapsed = time.time() - started
    if isinstance(outcome, str):
        print(f"[ddg 本地兜底] ✗ {elapsed:.2f}s {outcome}")
        return
    rows, dropped, note = outcome
    print(f"[ddg 本地兜底] ✓ {elapsed:.2f}s {len(rows)} 条（剔广告 {dropped}）"
          + (f"｜{note[:60]}" if note else ""))
    for title, link, _ in rows[:2]:
        print(f"    · {title[:60]}｜{link[:70]}")


def _probe_fetch(url: str) -> None:
    """试一次 Firecrawl 内容提取（它是提取服务，不是搜索服务）。"""
    if not web_module._search_key("firecrawl"):
        print("[firecrawl scrape] 未配置密钥（跳过）")
        return
    started = time.time()
    markdown = web_module._firecrawl_markdown(url)
    elapsed = time.time() - started
    if not markdown:
        print(f"[firecrawl scrape] ✗ {elapsed:.2f}s 没拿到正文（未配置/失败/空内容）")
        return
    print(f"[firecrawl scrape] ✓ {elapsed:.2f}s {len(markdown)} 字符")
    print(f"    {markdown[:160].replace(chr(10), ' / ')}")


def main() -> int:
    parser = argparse.ArgumentParser(description="探检索后端可用性（不打印密钥）")
    parser.add_argument("--query", action="append", default=[],
                        help="查询词，可重复；默认 Wikipedia Tower Bridge")
    parser.add_argument("--providers", default="",
                        help="逗号分隔，默认探测所有已配置密钥的供应商")
    parser.add_argument("--max", type=int, default=3, dest="max_results")
    parser.add_argument("--local", action="store_true", help="额外探本地兜底通道")
    parser.add_argument("--fetch", default="", help="额外试一次 Firecrawl 单页提取")
    args = parser.parse_args()

    queries = args.query or [_DEFAULT_QUERY]
    configured = [name for name in web_module._SEARCH_KEY_VARS
                  if web_module._search_key(name)]
    order = web_module._search_order()
    providers = ([p.strip() for p in args.providers.split(",") if p.strip()]
                 if args.providers else configured)
    print(f"已配密钥: {configured or '（无）'}；本次随机顺序: {order or '（无，走本地兜底）'}")
    for query in queries:
        print(f"=== 查询: {query}")
        for provider in providers:
            _probe_api(provider, query, args.max_results)
        if args.local or not providers:
            _probe_local(query, args.max_results)
    if args.fetch:
        print(f"=== Firecrawl 提取: {args.fetch}")
        _probe_fetch(args.fetch)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
