"""查 PyPI 包的版本 / Python 要求 / 依赖数量（可集成性评估）。

用法：uv run --no-sync python scripts/probe_pypi_meta.py [包名 ...]
不带参数用内置候选清单。输出 ≤20 行结论。2026-09-13 立（网络检索工具调研）。
"""

import json
import sys
import urllib.request

PACKAGES = [
    "crawl4ai", "trafilatura", "ddgs", "html2text", "readability-lxml",
    "markitdown", "playwright", "selectolax", "lxml", "httpx", "pysearx",
]


def meta(name: str) -> str:
    try:
        info = json.load(urllib.request.urlopen(
            urllib.request.Request(f"https://pypi.org/pypi/{name}/json",
                                   headers={"User-Agent": "Wovra"}),
            timeout=15))["info"]
    except Exception as error:  # noqa: BLE001
        return f"{name:20s} ERR {type(error).__name__}"
    deps = info.get("requires_dist") or []
    return (f"{name:20s} v{info['version']:12s} python={str(info.get('requires_python')):14s}"
            f" deps={len(deps):3d}  lic={(info.get('license_expression') or info.get('license') or '-')[:28]}")


def main() -> None:
    for name in (sys.argv[1:] or PACKAGES):
        print(meta(name))


if __name__ == "__main__":
    main()
