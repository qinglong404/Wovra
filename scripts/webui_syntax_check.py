"""提取 webui/index.html 的内联 <script> 做 node --check（纯本地，零 LLM）。

为什么需要：前端是这个项目唯一"改完看不见"的层——90K 字符的脚本块一旦
语法出错，页面直接空白，而 Python 测试一条都不会红。把它固化成仪器，
每次改前端后跑一次即可。
"""
import pathlib
import re
import subprocess
import sys

html = pathlib.Path("webui/index.html").read_text(encoding="utf-8")
blocks = re.findall(r"<script[^>]*>(.*?)</script>", html, re.S)
if not blocks:
    sys.exit("没有找到 <script> 块")
out = pathlib.Path("output/_webui_check.js")
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text("\n;\n".join(blocks), encoding="utf-8")
print(f"脚本块 {len(blocks)} 个 / {sum(len(b) for b in blocks)} 字符 → {out}")
r = subprocess.run(["node", "--check", str(out)], capture_output=True, text=True)
print("node --check:", "通过" if r.returncode == 0 else "失败")
if r.returncode:
    print(r.stdout[-2000:], r.stderr[-2000:])
    sys.exit(1)
