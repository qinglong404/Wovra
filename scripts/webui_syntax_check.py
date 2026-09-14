"""提取 webui/index.html 的内联 <script> 逐块编译检查（纯本地，零 LLM）。

为什么需要：前端是这个项目唯一"改完看不见"的层——10 万字符的脚本块一旦
语法出错，页面直接卡在骨架屏，而 Python 测试一条都不会红。

**为什么不用 `node --check`（2026-09-14 实测教训）**：`node --check` 按
CommonJS 解析（整份文件被当作**函数体**），于是**顶层 `return` 合法**；
而浏览器里 <script> 是经典脚本，顶层 return 是 `Illegal return statement`
——整块报废。真实事故：`flushLiveStep` 的 `}` 被插到 `return had;` 之前
（同一批"补丁插错位置"），`node --check` 通过、页面骨架屏转圈，用户报
"会话记录一直加载不出来"。`vm.Script` 与浏览器同为经典脚本语义，能抓它。

实现要点：
* **逐块**编译（与浏览器一致——每个 <script> 是独立编译单元），
  拼接检查会掩盖"某一块自己没闭合"或跨块重复声明；
* 行号**回映到 index.html**：报告"第几行"要是文件里的行号，
  不然还得手工换算偏移（这次排查就手工换过）。
"""
import pathlib
import re
import subprocess
import sys

DRIVER = r"""
const vm = require('vm'), fs = require('fs');
const file = process.argv[1];
const src = fs.readFileSync(file, 'utf8');
try {
  new vm.Script(src, { filename: file });
} catch (e) {
  const m = /:(\d+)/.exec((e.stack || '').split('\n')[0] || '');
  console.log('ERR ' + (m ? m[1] : '?') + ' ' + e.message);
  process.exit(1);
}
// 第二关：**顶层声明完整性**（2026-09-14）。同一批"补丁插错位置"还会留下
// 语法合法的静默错位：`}` 与函数声明互换 → 整个函数被吞进上一个函数里。
// 页面表现为某页签整块空白（实测：renderCtxbar/renderHeader/renderTimeline
// 被吞，页面一直"加载中"）。做法：在 vm 上下文里**真跑一遍**（声明在解析时
// 实例化，运行期报错不影响），再逐条核对列 0 的 function 声明是否真在顶层。
const ctx = vm.createContext({ console, setTimeout, clearTimeout, setInterval, clearInterval });
try { vm.runInContext(src, ctx); } catch (e) { /* 运行期错误（无 DOM）不影响声明 */ }
const re = /^(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(/;
const nested = [];
src.split('\n').forEach((line, i) => {
  const m = re.exec(line);
  if (m && typeof ctx[m[1]] !== 'function') nested.push((i + 1) + ':' + m[1]);
});
if (nested.length) { console.log('NESTED ' + nested.join(',')); process.exit(2); }
console.log('OK');
process.exit(0);
"""

html = pathlib.Path("webui/index.html").read_text(encoding="utf-8")
blocks = [
    (m.start(1), m.group(1))
    for m in re.finditer(r"<script[^>]*>(.*?)</script>", html, re.S)
]
blocks = [(pos, body) for pos, body in blocks if body.strip()]
if not blocks:
    sys.exit("没有找到内联 <script> 块")

out_dir = pathlib.Path("output")
out_dir.mkdir(parents=True, exist_ok=True)
print(f"脚本块 {len(blocks)} 个 / {sum(len(b) for _, b in blocks)} 字符")
failed = False
for i, (pos, body) in enumerate(blocks):
    line0 = html.count("\n", 0, pos) + 1  # 块首在 index.html 里的行号
    out = out_dir / f"_webui_check_{i}.js"
    out.write_text(body, encoding="utf-8")
    r = subprocess.run(
        ["node", "-e", DRIVER, str(out)], capture_output=True, text=True
    )
    if r.returncode == 0:
        print(f"块{i}（index.html:{line0} 起）: 通过")
        continue
    failed = True
    detail = (r.stdout + r.stderr).strip()
    m = re.match(r"ERR (\d+) (.*)", detail)
    if m and m.group(1) != "?":
        print(f"块{i}: 失败 → index.html 第 {line0 + int(m.group(1)) - 1} 行：{m.group(2)}")
    elif detail.startswith("NESTED "):
        items = []
        for item in detail[len("NESTED "):].split(","):
            ln, _, name = item.partition(":")
            items.append(f"{name}（index.html 第 {line0 + int(ln) - 1} 行）")
        print(f"块{i}: 失败 → 列 0 的函数声明没落在顶层（被吞进别的函数/块）："
              + "、".join(items))
    else:
        print(f"块{i}: 失败 {detail[:500]}")
if failed:
    sys.exit(1)
