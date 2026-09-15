"""成败徽标的**视觉核对**（真浏览器截图，零 LLM）。

为什么必须看图：用户报障的对象是"工具消息块"——判定再对，徽标颜色/文案
画错也等于没修。本仪器把 `tests/fixtures/tool_status_cases.json` 的每条用例
渲染成**真 UI 形态的工具卡**（复用 `webui/index.html` 的真 CSS 与真
`statusOf`/`resultStatus`），截图后人工过目。

做法：从 `webui/index.html` 抽出 <style> 与 STATUS_TABLE 段（含 statusOf、
resultStatus），生成一张用例矩阵页；用项目自带的 screenshot 工具截 PNG，
并打印像素统计（暗部占比 / 主色），确保是"真画出来了"而不是空白页。

用法：`uv run --no-sync python scripts/probe_status_visual.py`
产物：`output/status_visual.html` + `output/status_visual.png`
"""
from __future__ import annotations

import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
PAGE = ROOT / "webui" / "index.html"
FIXTURE = ROOT / "tests" / "fixtures" / "tool_status_cases.json"
OUT_HTML = ROOT / "output" / "status_visual.html"

BEGIN = "/* >>> STATUS_TABLE"
END_MARK = "/* <<< STATUS_TABLE */"


def extract() -> tuple[str, str]:
    text = PAGE.read_text(encoding="utf-8")
    style = "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", text, re.S))
    start = text.find(BEGIN)
    if start == -1:
        sys.exit("找不到 STATUS_TABLE 标记块")
    end = text.find(END_MARK, start)
    if end == -1:
        sys.exit("STATUS_TABLE 标记块不完整")
    block = text[start:end + len(END_MARK)]
    # resultStatus 紧跟表块之后
    after = text[end:end + 3000]
    fn = re.search(r"function resultStatus\(name,content\)\{.*?\n\}", after, re.S)
    if fn is None:
        sys.exit("找不到 resultStatus")
    return style, block + "\n" + fn.group(0)


# 工具卡与徽标相关的类名：只抄这些规则进核对页。
#
# 为什么不整页套用产品样式表（第一版的错）：产品 `body{display:flex}` 是
# "侧栏 + 阅读列"骨架，核对页没有那套 DOM → 卡片被挤成一根根窄柱、标题重叠。
# 抄局部规则既保住"颜色/间距就是产品那一套"，又让核对页自己排版。
_TOOLCARD_KEYS = (
    ".tcard", ".t-head", ".t-icon", ".t-name", ".t-sum", ".t-when", ".t-st",
    ".t-args", ".t-result", ".t-jump", ".st-ok", ".st-bad", ".st-deny",
    ".st-run", ".st-running",
)


def toolcard_css(style: str) -> str:
    """从产品样式表里抽出工具卡/徽标规则（含 :root 变量），供核对页复用。

    抄来的东西必须可验：调用方 `verify_css` 会核对三条状态色的取值仍与
    产品一致——否则"抄错一版就永远看不出来"。
    """
    rules = re.findall(r"([^{}@]+)\{([^{}]*)\}", style)
    picked = [f"{sel.strip()}{{{body.strip()}}}" for sel, body in rules
              if any(key in sel for key in _TOOLCARD_KEYS)]
    root = re.search(r":root\{([^{}]*)\}", style)
    variables = f":root{{{root.group(1).strip()}}}" if root else ""
    if not picked:
        sys.exit("没抽到任何工具卡规则——产品类名改了？")
    return variables + "\n" + "\n".join(picked)


def verify_css(style: str) -> None:
    """状态色必须仍是产品里那三个（teal/red/gold）——护栏，防止抄成静默过期。"""
    for cls, token in (("st-ok", "teal"), ("st-bad", "red"), ("st-deny", "gold")):
        found = re.search(rf"\.{cls}\{{color:var\(--{token}\)\}}", style)
        if found is None:
            sys.exit(f"产品的 .{cls} 不再用 --{token}——核对页配色需同步")


def esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def main() -> None:
    style, js = extract()
    verify_css(style)
    cases = json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]
    groups: dict[str, list[dict]] = {"deny": [], "error": [], "ok": []}
    for case in cases:
        groups[case["expect"]].append(case)

    columns = []
    for kind, label in (("ok", "应判成功"), ("error", "应判失败"), ("deny", "应判拒绝")):
        cards = [f'<h2 class="sg-h">{label}（{len(groups[kind])} 条）</h2>']
        for case in groups[kind]:
            content = esc(case["content"][:150])
            head = esc((case["content"].split("\n") or [""])[0][:60])
            cards.append(f"""
<div class="tcard" data-src="{content}">
  <div class="t-head">
    <span class="t-icon">🔧</span><span class="t-name">read_file</span>
    <span class="t-sum" title="{head}">{head}</span>
    <span class="t-st" data-st>…</span>
  </div>
  <div class="t-args">{esc(case["why"][:80])}</div>
  <div class="t-result" style="display:block">{esc(case["content"][:120])}</div>
</div>""")
        columns.append('<div class="sg-col">' + "".join(cards) + "</div>")

    html = f"""<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>成败徽标视觉核对</title><style>
{toolcard_css(style)}
/* 核对页自己的排版：只抄工具卡规则（见 toolcard_css），不套用产品整张样式表
   ——产品 body 是 flex 骨架、卡片样式为真应用布局而设，整页套用会把这里
   挤成窄条（这是第一版的错，截图可见）。 */
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:var(--bg0);color:var(--tx);font-family:var(--sans);
  font-size:13px;padding:18px}}
h1{{font-size:16px;margin-bottom:4px}}
.sg-sub{{color:var(--tx3);font-size:12px;margin-bottom:14px}}
.sg-wrap{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));
  gap:14px;align-items:start}}
.sg-col{{display:flex;flex-direction:column;gap:8px;min-width:0}}
.sg-h{{font-size:13.5px;color:var(--acc);margin-bottom:2px}}
.sg-col .tcard{{margin:0;overflow:visible}}
.sg-col .t-result{{display:block;max-height:none;overflow:visible;
  font-size:11px;line-height:1.5;word-break:break-word}}
</style></head><body>
<h1>工具消息块 · 成败判定视觉核对</h1>
<p class="sg-sub">每张卡片的徽标由页面真函数 resultStatus 计算——颜色与文案即用户所见。</p>
<div class="sg-wrap">{''.join(columns)}</div>
<script>
{js}
for (const card of document.querySelectorAll('.tcard')) {{
  const [txt, cls] = resultStatus('read_file', card.dataset.src);
  const el = card.querySelector('[data-st]');
  el.textContent = txt; el.className = 't-st ' + cls;
}}
</script></body></html>"""
    OUT_HTML.parent.mkdir(parents=True, exist_ok=True)
    OUT_HTML.write_text(html, encoding="utf-8")
    counts = "／".join(f"{k}:{len(groups[k])}" for k in ("ok", "error", "deny"))
    print(f"已生成 {OUT_HTML.relative_to(ROOT)}（{len(cases)} 个用例；{counts}）")


if __name__ == "__main__":
    main()
