"""抓 GitHub 项目页的 star 数与 license（API 限流时的替代取证）。

用法：uv run --no-sync python scripts/probe_github_stars.py [owner/repo ...]
不带参数则用内置候选清单。输出每行一个仓库，只打结论。2026-09-13 立。
"""

import re
import sys
import urllib.request

CANDIDATES = [
    "unclecode/crawl4ai",
    "adbar/trafilatura",
    "searxng/searxng",
    "microsoft/markitdown",
    "lightpanda-io/browser",
    "browser-use/browser-use",
    "mendableai/firecrawl",
    "scrapy/scrapy",
    "jina-ai/reader",
]


def fetch(repo: str) -> tuple[str, str]:
    """返回 (stars 文本, license 文本)；抓不到给 '-'。"""
    try:
        html = urllib.request.urlopen(
            urllib.request.Request(f"https://github.com/{repo}",
                                   headers={"User-Agent": "Mozilla/5.0"}),
            timeout=20).read().decode("utf-8", "replace")
    except Exception as error:  # noqa: BLE001
        return "-", f"ERR {type(error).__name__}"
    stars = "-"
    m = (re.search(r'aria-label="([\d,]+) users starred', html)
         or re.search(r'id="repo-stars-counter-star"[^>]*title="([\d,]+)"', html))
    if m:
        stars = m.group(1)
    lic = "-"
    m = re.search(r'>([A-Za-z0-9\.\- ]{2,30}) license<', html) or re.search(
        r'license["\s:]+([A-Za-z0-9\.\- ]{2,30})<', html)
    if m:
        lic = m.group(1).strip()
    return stars, lic


def main() -> None:
    repos = sys.argv[1:] or CANDIDATES
    for repo in repos:
        stars, lic = fetch(repo)
        print(f"{repo:34s} stars={stars:>8s}  license={lic}")


if __name__ == "__main__":
    main()
