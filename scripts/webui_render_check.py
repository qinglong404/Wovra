"""在 Node 里**真跑**一次 webui 的渲染函数，核对产出的 HTML（纯本地，零 LLM）。

为什么需要：`webui_syntax_check.py` 只回答"语法对不对"——它拦不住"函数跑起来
产出 `undefined`/`NaN`/空块"这类**语义**错误。2026-09-13 连着几批前端回归
（思考块被强制折叠、临时块重复、状态栏数字口径错）都是这一类：语法全过，
页面不对。Python 测试照不到前端（用户定的纪律：前端改动必须过 node --check
**并实际渲染核对**）。

做法：抽出内联 <script>，在一个极小的 DOM 桩里执行，然后按几种真实场景调用
渲染函数、把产出的 HTML 抓出来查：
  * `undefined` / `NaN` / `null` 字面量  —— 模板里变量没接上
  * 空 cells（状态栏一个 agent 都没画出来）
  * 状态栏必须同时给出「重组后投影」与「实测」两类数（2026-09-13 用户口径）

输出只打结论（≤20 行），不打 HTML 原文。
"""
import pathlib
import re
import subprocess
import sys

PREFIX = r"""
// ---- 极小 DOM 桩：只实现渲染函数真正会碰的那几样 ----
const els = new Map();
function mkEl(id){
  const e = {
    id, innerHTML:'', textContent:'', value:'', className:'', dataset:{},
    style:new Proxy({},{get:()=>'' ,set:()=>true}),
    classList:{ _s:new Set(), add(...c){c.forEach(x=>this._s.add(x))},
                remove(...c){c.forEach(x=>this._s.delete(x))},
                toggle(c,v){v===undefined?(this._s.has(c)?this._s.delete(c):this._s.add(c)):(v?this._s.add(c):this._s.delete(c))},
                contains(c){return this._s.has(c)} },
    children:[], parentElement:null, parentNode:null,
    appendChild(){}, removeChild(){}, remove(){}, insertBefore(){},
    querySelector(){return mkEl('q')}, querySelectorAll(){return []},
    addEventListener(){}, setAttribute(){}, getAttribute(){return null},
    scrollIntoView(){}, focus(){}, click(){},
  };
  return e;
}
function byId(id){ if(!els.has(id)) els.set(id, mkEl(id)); return els.get(id); }
global.window = global;
global.addEventListener = ()=>{};
global.removeEventListener = ()=>{};
global.matchMedia = ()=>({ matches:false, addEventListener(){} });
global.getComputedStyle = ()=>mkEl('cs');
global.document = {
  getElementById:byId,
  querySelector:(s)=>byId('sel:'+s),
  querySelectorAll:()=>[],
  createElement:(t)=>mkEl('new:'+t),
  addEventListener(){}, body:mkEl('body'),
  documentElement:mkEl('html'), hidden:false,
};
global.location = { hash:'', search:'', pathname:'/' };
global.localStorage = { getItem:()=>null, setItem(){}, removeItem(){} };
global.navigator = { clipboard:null };
global.setInterval = ()=>0;
global.clearInterval = ()=>{};
global.setTimeout = ()=>0;
global.clearTimeout = ()=>{};
global.fetch = ()=>Promise.reject(new Error('no net in check'));
global.requestAnimationFrame = ()=>0;
global.CSS = { escape:(s)=>String(s) };
global.marked = null; global.DOMPurify = null; global.hljs = null;
global.EventSource = function(){ return { close(){}, addEventListener(){} } };

// ---- 执行 webui 脚本 ----
// 拼成**同一个文件**再跑（不另起 eval）：渲染函数读的模块级绑定（META/TREE…）
// 与下面的场景赋值必须在同一作用域里，分开 eval 会各拿一份、场景改了个寂寞。
"""

SCENARIOS = r"""
// ================= 渲染核对场景 =================
function cellCount(html){ return (html.match(/class="ctx-cell"/g) || []).length; }
// 只留可见正文：把 title="..."（悬停才看的）剥掉。用户口径（2026-09-13）：
// **页面上不留机制说明式的描述文字**——"不要留这段话，不要留这种描述的，
// 当产品标准写"。故正文里出现这些词就是回归。
const PROSE = ['机械投影','登记即可算','上次装配','零 LLM','一字不改',
               'tiktoken','字符启发式','会被下一轮刷新','相加 ≠','各视图各自'];
function bodyOf(html){ return html.replace(/title="[^"]*"/g, 'title=""'); }
function proseIn(html){
  const body = bodyOf(html);
  return PROSE.filter(k => body.includes(k));
}
function scan(label, html){
  const bad = [];
  for (const k of ['undefined', 'NaN', '>[object Object]<']) {
    if (html.includes(k)) bad.push(k + '×' + html.split(k).length);
  }
  return bad;
}
const problems = [];
function check(label, html, expectCells){
  const cells = cellCount(html);
  console.log(`  ${label}：cells=${cells} 长度=${html.length}`);
  if (expectCells !== undefined && cells !== expectCells) {
    problems.push(`${label}: 期望 ${expectCells} 个 agent 格，实际 ${cells}`);
  }
  for (const b of scan(label, html)) problems.push(`${label}: 出现 ${b}`);
  const prose = proseIn(html);
  if (prose.length) problems.push(`${label}: 正文里出现机制说明词 ${prose.join('、')}`);
}

console.log('场景 A｜已分裂 + 4 个域从未运行（投影可得，实测为 0）');
META = { id:'s1', registry:[
  {id:'Main', name:'主agent', status:'active', ctx_cur:235398, ctx_peak:235398, window:1000000, rounds:0, steps:208},
  {id:'A', name:'域甲', status:'dormant', ctx_cur:0, ctx_peak:0, window:0, rounds:0, steps:0},
  {id:'B', name:'域乙', status:'dormant', ctx_cur:0, ctx_peak:0, window:0, rounds:0, steps:0},
  {id:'C', name:'域丙', status:'dormant', ctx_cur:0, ctx_peak:0, window:0, rounds:0, steps:0},
  {id:'D', name:'域丁', status:'dormant', ctx_cur:0, ctx_peak:0, window:0, rounds:0, steps:0},
], round_list:[] };
const pvAgents = [
  {id:'Main', name:'主agent', is_main:true, count:4, total_chars:5523, tokens:4621, alt_tokens:13371},
  {id:'A', name:'域甲', count:4, total_chars:5648, tokens:4764},
  {id:'B', name:'域乙', count:4, total_chars:5678, tokens:4734},
  {id:'C', name:'域丙', count:4, total_chars:5650, tokens:4738},
  {id:'D', name:'域丁', count:4, total_chars:5685, tokens:4792},
];
VIEWSIZES = { sig:'x', agents:pvAgents, window:1000000, basis:'tiktoken:cl100k_base' };
renderCtxbar();
let html = byId('ctxbar').innerHTML;
check('A 有投影', html, 5);
if (!html.includes('≈')) problems.push('A: 没有出现 ≈ 前缀（投影没被当成主数）');
if (!html.includes('实测')) problems.push('A: 有实测时没并列显示"实测"');
// 尺子只在悬停里交代（正文不留说明文字）
if (proseIn(html).length === 0 && !/title="[^"]*tiktoken/.test(html)) {
  problems.push('A: 连悬停里都没交代估算口径');
}
if (process.env.WEBUI_RENDER_DUMP) console.log('\n[场景 A 的 HTML]\n' + html + '\n');

console.log('场景 B｜产物尚未生效（域还没进注册表 → 也要列出来）');
META = { id:'s1', registry:[{id:'Main', name:'主agent', status:'active',
  ctx_cur:0, ctx_peak:0, window:0, rounds:0, steps:0}],
  round_list:[{seq:1, split:{pending:true, domains:[{name:'新域'}]}}] };
VIEWSIZES = { sig:'y', window:1000000, basis:'heuristic', agents:[
  {id:'Main', name:'主agent', is_main:true, count:2, total_chars:2000, tokens:1500},
  {id:'A', name:'新域', count:1, total_chars:900, tokens:700}] };
renderCtxbar();
html = byId('ctxbar').innerHTML;
check('B 含未登记域', html, 2);

console.log('场景 C｜单 agent 会话（没有投影，退回实测）');
META = { id:'s2', registry:[{id:'Main', name:'主agent', status:'active',
  ctx_cur:41800, ctx_peak:52000, window:1000000, rounds:3, steps:12}], round_list:[] };
VIEWSIZES = null;
renderCtxbar();
html = byId('ctxbar').innerHTML;
check('C 实测回退', html, 1);
if (!html.includes('41.8K')) problems.push('C: 实测值没显示出来');

console.log('场景 E｜投影还没回来（多 agent，不许显示"无数据"）');
META = { id:'s3', registry:[
  {id:'Main', name:'主agent', status:'active', ctx_cur:0, ctx_peak:0, window:0},
  {id:'A', name:'域甲', status:'dormant', ctx_cur:0, ctx_peak:0, window:0}], round_list:[] };
VIEWSIZES = null;      // 拉取在途
renderCtxbar();
html = byId('ctxbar').innerHTML;
check('E 在途', html, 2);
if (!html.includes('投影计算中')) problems.push('E: 在途时没显示"投影计算中"');
if (html.includes('无数据')) problems.push('E: 在途时显示了"无数据"（会被读成还是没有）');

console.log('场景 D｜project 面板的注册表卡片');
META = { id:'s1', registry:[
  {id:'Main', name:'主agent', status:'active', ctx_cur:235398, ctx_peak:235398, window:1000000, rounds:0, steps:208, files:[], history_files:[]},
  {id:'A', name:'域甲', status:'dormant', ctx_cur:0, ctx_peak:0, window:0, rounds:0, steps:0, files:['src/a.py'], history_files:[], file_notes:{}},
], round_list:[], chat:[], registry_defects:['F2 违反：Main.files 与 A.files 共认 1 个文件（a.py）——一个文件只能一个域'] };
VIEWSIZES = { sig:'z', agents:pvAgents, window:1000000, basis:'tiktoken:cl100k_base' };
TREE = { files:[], root:'/x', truncated:false, cur:'s1' };
TREESEL = new Set();
try {
  renderProject(byId('content'));
  html = byId('content').innerHTML;
  check('D 项目页', html);
  if (!html.includes('注册表归属冲突')) problems.push('D: 归属冲突警示卡没出现');
} catch (e) {
  problems.push('D: renderProject 抛错 —— ' + e.message);
}

// 护栏自检：永不报警的检查等于没有检查。钉住"正文里有→报、只在 title 里→不报"。
if (proseIn('<div>机械投影</div>').length !== 1) {
  problems.push('护栏自检: 正文里的机制说明词没被抓到');
}
if (proseIn('<div title="机械投影">ok</div>').length !== 0) {
  problems.push('护栏自检: 悬停里的词被误报');
}

console.log('');
if (problems.length) {
  console.log('渲染核对：失败 ' + problems.length + ' 项');
  problems.slice(0, 12).forEach(p => console.log('  ✗ ' + p));
  process.exit(1);
}
console.log('渲染核对：通过（5 个场景，无 undefined/NaN，口径完整）');
"""


def main() -> int:
    html = pathlib.Path("webui/index.html").read_text(encoding="utf-8")
    blocks = re.findall(r"<script[^>]*>(.*?)</script>", html, re.S)
    if not blocks:
        sys.exit("没有找到 <script> 块")
    out = pathlib.Path("output")
    out.mkdir(parents=True, exist_ok=True)
    (out / "_webui_render.js").write_text("\n;\n".join(blocks), encoding="utf-8")
    # 一个文件：DOM 桩 → webui 脚本 → 场景（同一作用域，故共享 META/VIEWSIZES…）
    (out / "_webui_render_check.js").write_text(
        PREFIX + "\n;\n".join(blocks) + SCENARIOS, encoding="utf-8")
    r = subprocess.run(
        ["node", str(out / "_webui_render_check.js")],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    print((r.stdout or "").rstrip())
    if r.returncode:
        print((r.stderr or "")[-1500:])
    return r.returncode


if __name__ == "__main__":
    sys.exit(main())
