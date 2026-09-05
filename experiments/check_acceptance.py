"""静态验收检查：对产出的 chat.html 逐条判定验收标准。

结构级检查客观、可复现，但**可博弈**（写出标记不等于实现行为）——
协议要求每轮另做 2 分钟人工冒烟抽查（见 experiments/README.md）。

用法：uv run python experiments/check_acceptance.py <chat.html 路径>
输出：逐条 PASS/FAIL + JSON 汇总行（供 collect.py 读取）。
"""

import json
import re
import sys
from pathlib import Path


def check(html: str) -> dict[str, bool]:
    """对页面文本逐条判定验收标准，返回 {条目: 是否通过}。"""
    lower = html.lower()
    return {
        "C1 语音朗读（speechSynthesis + 停止逻辑）": bool(
            re.search(r"speechSynthesis", html)
            and re.search(r"\.speak\(", html)
            and re.search(r"\.cancel\(", html)
            and ("朗读" in html or "🔊" in html)
        ),
        "C2 导出对话（Blob 下载 Markdown）": bool(
            "Blob" in html
            and "createObjectURL" in html
            and "download" in lower
            and ".md" in lower
        ),
        "C3 重新生成（替换 AI 回复）": bool(
            re.search(r"重新生成|regenerate|🔄", html, re.IGNORECASE)
        ),
        "C4 会话内搜索（关键词高亮）": bool(
            ("搜索" in html or "search" in lower)
            and re.search(r"高亮|highlight|<mark", html, re.IGNORECASE)
        ),
        "C5 无外部依赖（零网络请求）": not re.search(
            r"(?<![\\`])fetch\(|XMLHttpRequest"
            r"|<script[^>]+src=[\"']https?://"
            r"|<link[^>]+href=[\"']https?://",
            html,
            re.IGNORECASE,
        ),
        "C6 结构完整（标签闭合）": bool(
            lower.count("<script") == lower.count("</script")
            and lower.count("<style") == lower.count("</style")
            and "</html>" in lower[-400:]
        ),
    }


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        return
    html = Path(sys.argv[1]).read_text(encoding="utf-8", errors="replace")
    results = check(html)
    passed = sum(results.values())
    for name, ok in results.items():
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    print(f"通过 {passed}/{len(results)}")
    print(json.dumps({"passed": passed, "total": len(results), "results": results},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
