"""顶部上下文条（#ctxbar）的**视觉核对**（真浏览器截图，零 LLM）。

为什么必须看图：顶部条是"这一份上下文占窗口多少"的唯一常驻读数，配色/对齐/数字
画错就算逻辑全对也等于没做。本仪器用**产品真 CSS + 真 renderCtxbar** 生成两态页
（V4 共享态、单 agent 实测态），截图人工过目。

做法：抽出 `webui/index.html` 的 <style> 与内联脚本 → 在 `webui_render_check`
的迷你 DOM 里真跑 `renderCtxbar` / `applyPlanCtx`，把产出的 HTML 收回来 →
拼一张静态页（只抄上下文条相关的样式规则）→ 由调用方截图。

用法：`uv run --no-sync python scripts/probe_ctxbar_visual.py`
产物：`output/ctxbar_visual.html`（随后用 screenshot 工具截 PNG 并看图）
"""
from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from webui_render_check import PREFIX, _static_ids  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]
PAGE = ROOT / "webui" / "index.html"
OUT_HTML = ROOT / "output" / "ctxbar_visual.html"
OUT_JS = ROOT / "output" / "_ctxbar_visual.js"

# 上下文条用到的样式规则（抄局部，不套整张表：产品 body 是 flex 骨架，
# 整页套用会把这条挤成窄柱——`probe_status_visual.py` 里同样的理由）。
_KEYS = ("#ctxbar", ".ctx-cell", ".ctx-dot", ".ctx-bar", ".ctxrow", ".mono", ".dot", ".nm")

SCENARIO = r"""
const out = {};
// ① V4 共享态：注册表里有这一份的**现读观测**（每步都在刷）
META = { id:'vis', registry:[
  {id:'Main', name:'主agent', status:'active', ctx_cur:131072, ctx_peak:235398, window:1000000, rounds:3, steps:21, seqs:[1,2,3]},
  {id:'A', name:'附件处理', status:'active', ctx_cur:0, ctx_peak:0, window:0, rounds:0, steps:0},
  {id:'B', name:'前端界面', status:'dormant', ctx_cur:0, ctx_peak:0, window:0, rounds:0, steps:0}],
  round_list:[] };
VIEWSIZES = { sig:'v', window:1000000, basis:'tiktoken:cl100k_base', shared:true,
  agents:[{id:'Main', name:'主agent', is_main:true, count:0, total_chars:0, tokens:131072}] };
renderCtxbar();
out.shared_before = byId('ctxbar').innerHTML;
// ② 每步一更新：/plan 报来新观测 → 条上的数字必须当场跟着变
const moved = applyPlanCtx({ ctx:[{id:'Main', name:'主agent',
  ctx_cur:262144, ctx_peak:262144, window:1000000}] });
out.shared_moved = byId('ctxbar').innerHTML;
out.shared_changed = !!moved;
// ③ 单 agent 会话（没有投影，退回实测观测）
META = { id:'vis2', registry:[
  {id:'Main', name:'主agent', status:'active', ctx_cur:41800, ctx_peak:52000, window:1000000, rounds:3, steps:12}],
  round_list:[] };
VIEWSIZES = null;
renderCtxbar();
out.solo = byId('ctxbar').innerHTML;
console.log('__OUT__' + JSON.stringify(out));
"""


def _style_for_ctxbar(style: str) -> str:
    rules = re.findall(r"([^{}@]+)\{([^{}]*)\}", style)
    picked = [f"{sel.strip()}{{{body.strip()}}}" for sel, body in rules
              if any(k in sel for k in _KEYS)]
    root = re.search(r":root\{([^{}]*)\}", style)
    variables = f":root{{{root.group(1).strip()}}}" if root else ""
    merged = re.search(r":root\{--side-w[^{}]*\}", style)
    if merged:
        variables += "\n" + merged.group(0)
    if not picked:
        sys.exit("没抽到上下文条规则——产品类名改了？")
    return variables + "\n" + "\n".join(picked)


def main() -> int:
    html = PAGE.read_text(encoding="utf-8")
    blocks = re.findall(r"<script[^>]*>(.*?)</script>", html, re.S)
    if not blocks:
        sys.exit("没有找到 <script> 块")
    prefix = PREFIX.replace("__STATIC_IDS__",
                            json.dumps(_static_ids(html), ensure_ascii=False))
    OUT_JS.parent.mkdir(parents=True, exist_ok=True)
    OUT_JS.write_text(prefix + "\n;\n".join(blocks) + SCENARIO, encoding="utf-8")
    proc = subprocess.run(["node", str(OUT_JS)], capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    if proc.returncode:
        sys.exit((proc.stderr or "")[-1200:])
    line = next((l for l in (proc.stdout or "").splitlines()
                 if l.startswith("__OUT__")), None)
    if line is None:
        sys.exit("场景没有回传结果")
    got = json.loads(line[len("__OUT__"):])
    if not got.get("shared_changed"):
        sys.exit("每步更新未生效：applyPlanCtx 没有识别出新观测")

    style = "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", html, re.S))
    css = _style_for_ctxbar(style)
    page = f"""<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>顶部上下文条 · 视觉核对</title><style>
{css}
/* 核对页自己的排版（只保证这条能被看清楚） */
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:var(--bg0);color:var(--tx);font-family:var(--sans);font-size:13px;
  padding:20px;display:block}}
h2{{font-size:13px;color:var(--acc);margin:18px 0 6px;font-weight:600}}
.note{{color:var(--tx3);font-size:11.5px;margin-bottom:4px}}
#ctxbar{{display:flex;border:1px solid var(--line);border-radius:8px}}
</style></head><body>
<h2>① V4 共享态（跑轮中，131072 tok / 1M）</h2>
<p class="note">这一份体量来自注册表里每步刷新的实测观测；多个 agent 同用一份。</p>
<div id="ctxbar" class="show">{got['shared_before']}</div>
<h2>② 同一条，服务端报来新观测（262144 tok）——按步更新后当场变</h2>
<p class="note">前端每 tick 合并 /plan 的现读值并重画，不必等轮闭合。</p>
<div id="ctxbar" class="show">{got['shared_moved']}</div>
<h2>③ 单 agent 会话（退回实测观测）</h2>
<div id="ctxbar" class="show">{got['solo']}</div>
</body></html>"""
    OUT_HTML.write_text(page, encoding="utf-8")
    print(f"已生成 {OUT_HTML.relative_to(ROOT)}（两态 + 单 agent 态）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
