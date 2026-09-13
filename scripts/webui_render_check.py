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
import json
import pathlib
import re
import subprocess
import sys

PREFIX = r"""
// ---- 迷你 DOM：够跑直播路径（用户 2026-09-13 报的四个渲染 bug 都在这条路上）----
// 支持：#id / .cls / tag / [attr="v"] / 空格后代 / :not(.x) / 逗号列表。
// innerHTML 的 setter 做一件够用的事：把 HTML 里出现的 id 与 class 注册成子元素
// （直播骨架是扁平的，所以这样 `box.querySelector('.live-think-body')` 找得到）。
const ALL = [];
let SEQID = 0;
function clsOf(e){
  return String(e.className||'').split(/\s+/).filter(Boolean);
}
function setCls(e,v){ e.className = v; }
function mkClassList(e){
  return {
    add(...c){ const s=new Set(clsOf(e)); c.forEach(x=>s.add(x)); setCls(e,[...s].join(' ')); },
    remove(...c){ const s=new Set(clsOf(e)); c.forEach(x=>s.delete(x)); setCls(e,[...s].join(' ')); },
    contains(c){ return clsOf(e).includes(c); },
    toggle(c,v){ const has=clsOf(e).includes(c);
      const want=(v===undefined)?!has:!!v; want?this.add(c):this.remove(c); return want; },
  };
}
/* innerHTML → 子节点树（**带栈的真解析，还原嵌套**）。

   2026-09-13 修：原实现用一条全局正则把所有开标签**扁平**挂到根下——后代选择器
   （`card.querySelector('.t-result')`）永远找不到东西。这个假象已经骗过我**四次**
   （场景 P 报"空档"、场景 L 的 textContent、工具结果回填…），每次都是我去改断言
   绕开它，而不是修仪器。现在按栈还原层级：产品代码怎么查，仪器就怎么答。
   只登记带 id/class 的元素（与本仪器一直以来的口径一致：影子节点不参与匹配），
   但**层级按真实嵌套还原**——只有这样后代选择器才成立。 */
const VOID_TAGS = new Set(['br','hr','img','input','meta','link','source']);
function parseInto(root, html){
  root.children = [];
  const stack = [root];
  const re = /<\/?(\w+)((?:"[^"]*"|'[^']*'|[^>])*)>/g;
  let m;
  while((m = re.exec(String(html||'')))){
    const raw = m[0], tag = String(m[1]).toLowerCase();
    if(raw.startsWith('</')){                       // 闭标签：退到匹配的那一层
      for(let i=stack.length-1;i>0;i--){
        if(stack[i].tagName===tag.toUpperCase()){ stack.length=i; break; }
      }
      continue;
    }
    const attrs = m[2] || '';
    const idm = /\bid="([^"]*)"/.exec(attrs);
    const cm = /\bclass="([^"]*)"/.exec(attrs);
    const selfClose = /\/\s*$/.test(attrs) || VOID_TAGS.has(tag);
    if(idm || cm){                                  // 带身份的元素：登记并挂到当前层
      const c = mkEl(tag);
      if(idm) c.id = idm[1];
      if(cm) c.className = cm[1];
      const dm = /[^\s=]+="[^"]*"/g; let a;
      // data-* 落进 dataset（camelCase），供 `[data-call="x"]` 这类选择器用
      while((a = dm.exec(attrs))){
        const eq=a[0].indexOf('='); const k=a[0].slice(0,eq); const v=a[0].slice(eq+1);
        if(!k.startsWith('data-'))continue;
        const cam=k.slice(5).replace(/-([a-z])/g,(_s,x)=>x.toUpperCase());
        c.dataset[cam]=v.slice(1,-1);
      }
      stack[stack.length-1].appendChild(c);
      if(!selfClose) stack.push(c);
    }else if(!selfClose){
      // 没身份的元素也要占一层，否则它的子元素会挂错层（层级照样要真）
      const ghost = mkEl(tag);
      stack[stack.length-1].appendChild(ghost);
      ghost._ghost = true;
      stack.push(ghost);
    }
  }
}
function mkEl(tag){
  const e = {
    tagName:String(tag||'div').toUpperCase(), id:'', className:'',
    textContent:'', value:'', dataset:{}, children:[],
    parentNode:null, parentElement:null, isConnected:true,
    style:new Proxy({},{get:()=>'' ,set:()=>true}),
    _html:'',
    appendChild(c){ c.parentNode=this; c.parentElement=this; c.isConnected=true;
                    this.children.push(c); return c; },
    removeChild(c){ const i=this.children.indexOf(c); if(i>=0)this.children.splice(i,1);
                    c.parentNode=null; c.isConnected=false; },
    remove(){ if(this.parentNode)this.parentNode.removeChild(this); },
    insertBefore(c,_ref){ return this.appendChild(c); },
    closest(){ return null; },
    addEventListener(){}, removeEventListener(){}, setAttribute(){}, getAttribute(){return null},
    scrollIntoView(){}, focus(){}, click(){}, replaceChildren(){ this.children=[]; },
    querySelector(sel){ const got=matchAll(this,sel); return got.length?got[0]:null; },
    querySelectorAll(sel){ return matchAll(this,sel); },
  };
  e.classList = mkClassList(e);
  Object.defineProperty(e,'innerHTML',{
    get(){ return e._html; },
    set(v){ e._html=String(v); parseInto(e, e._html); },
  });
  ALL.push(e);
  return e;
}
function walk(root, out){
  for(const c of root.children||[]){ out.push(c); walk(c, out); }
  return out;
}
function matchOne(el, sel){
  sel = String(sel).trim();
  const nots = [];
  sel = sel.replace(/:not\(([^)]*)\)/g, (_s,x)=>{ nots.push(x.trim()); return ''; });
  for(const n of nots){
    if(n.startsWith('.') && clsOf(el).includes(n.slice(1))) return false;
  }
  const parts = sel.match(/^([a-zA-Z]*)((?:[.#][\w-]+|\[[^\]]+\])*)$/);
  if(!parts) return false;
  if(parts[1] && el.tagName !== parts[1].toUpperCase()) return false;
  const rest = parts[2] || '';
  const tokens = rest.match(/[.#][\w-]+|\[[^\]]+\]/g) || [];
  for(const t of tokens){
    if(t.startsWith('#')){ if(el.id !== t.slice(1)) return false; }
    else if(t.startsWith('.')){ if(!clsOf(el).includes(t.slice(1))) return false; }
    else{
      const a = /^\[([\w-]+)="?([^"\]]*)"?\]$/.exec(t);
      if(!a) return false;
      // `data-seq` → `dataset.seq`（真 DOM 就是这么映射的，桩要跟上，
      // 否则 `.crow[data-seq="7"]` 永远匹配不到——踩过）
      const key = a[1].startsWith('data-')
        ? a[1].slice(5).replace(/-(\w)/g, (_m,c)=>c.toUpperCase())
        : a[1];
      const v = (key==='id') ? el.id
        : (el.dataset[key]!==undefined ? el.dataset[key] : el.getAttribute(a[1]));
      if(String(v===undefined||v===null?'':v) !== a[2]) return false;
    }
  }
  return true;
}
function matchAll(root, sel){
  const out = [];
  for(const one of String(sel).split(',')){
    const chain = one.trim().split(/\s+/).filter(Boolean);
    if(!chain.length) continue;
    let pool = [root];
    for(const step of chain){
      const next = [];
      for(const node of pool){
        const cands = (node===root) ? walk(root,[]) : walk(node,[]);
        for(const c of cands) if(matchOne(c, step)) next.push(c);
      }
      pool = next;
    }
    for(const p of pool) if(!out.includes(p)) out.push(p);
  }
  return out;
}
function resetDom(){
  ALL.length = 0;
  const body = mkEl('body');
  const content = mkEl('div');
  content.id = 'content';
  body.appendChild(content);
  // 页面上**静态就有的** id（从 index.html 的标签里抽出来的）先造出来：
  // 顶层连线代码 `document.getElementById('bar')` 之类要能跑，而
  // getElementById 对**动态**节点仍然老实返回 null（直播路径靠这个判"没有"）。
  for(const id of __STATIC_IDS__){ const e=mkEl('div'); e.id=id; body.appendChild(e); }
  return {body, content};
}
function byId(id){
  for(const e of ALL) if(e.id === id) return e;
  return null;                                // 与真 DOM 一致：没有就是 null
}
let DOM = resetDom();
global.window = global;
global.addEventListener = ()=>{};
global.removeEventListener = ()=>{};
global.matchMedia = ()=>({ matches:false, addEventListener(){} });
global.getComputedStyle = ()=>mkEl('div');
global.document = {
  getElementById:byId,
  // 纯 `#id` 选择器找不到时**就地造一个**（页面顶层的连线代码 `$('#search')`
  // 要能跑）；复合选择器找不到就老实返回 null——直播路径靠这个 null 判断
  // "本轮正式块还没渲染出来"，不能骗它。
  querySelector:(s)=>{
    const g=matchAll(DOM.body,s);
    if(g.length)return g[0];
    const t=String(s).trim();
    if(/^#[\w-]+$/.test(t)){
      const e=byId(t.slice(1));
      if(e)return e;
      const n=mkEl('div'); n.id=t.slice(1); return n;   // 顶层连线用（`$('#search')`）
    }
    return null;
  },
  querySelectorAll:(s)=>matchAll(DOM.body,s),
  createElement:(t)=>mkEl(t),
  addEventListener(){}, get body(){ return DOM.body; },
  documentElement:mkEl('html'), hidden:false,
};
global.location = { hash:'', search:'', pathname:'/' };
global.localStorage = { getItem:()=>null, setItem(){}, removeItem(){} };
global.navigator = { clipboard:null };
// 定时器桩：**返回非 0 id 并记账**——`watchMaintenance()` 靠 `if(MAINT_WATCH)`
// 判"已经在看了"，桩返回 0 会让它每次都重新武装，掩盖幂等性（踩过）。
let _timerSeq = 0;
const _timers = [];
global.setInterval = (fn, ms)=>{ const id=++_timerSeq; _timers.push({id,fn,ms}); return id; };
global.clearInterval = (id)=>{ const i=_timers.findIndex(t=>t.id===id); if(i>=0)_timers.splice(i,1); };
global.setTimeout = ()=>0;
global.clearTimeout = ()=>{};
global.fetch = ()=>Promise.reject(new Error('no net in check'));
global.requestAnimationFrame = ()=>0;
global.CSS = { escape:(s)=>String(s) };
// marked/purify 是 vendor 里的真库（浏览器里加载）；这里给**等价的最小替身**，
// 目的是证明"正文走的是 md() 这条渲染路"，而不是 textContent 直出。
global.marked = { parse:(s)=>String(s||'').replace(/\*\*(.+?)\*\*/g,'<strong>$1</strong>') };
global.DOMPurify = { sanitize:(s)=>String(s||'') };
global.hljs = null;
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

// ===========================================================================
// 场景 F/G：直播区（2026-09-13 大改）。用户报的四个 bug 都在这条路上：
//   ① 思考跑到消息块外面 ② 出现完消失一下才出现
//   ③ 思考不展开、要像工具一样一行折叠 ④ 正文要实时渲染 MD
// ===========================================================================
// ".livebox 挂在 #content 直下" = 没有消息块包着它（用户报的"跑到块外面"）。
// 注意用**直接子节点**判断：querySelector 会连后代一起命中，那样连正确挂载
// 都会被误判（我自己先踩了一次）。
function liveboxDirectUnderContent(){
  return [...DOM.content.children].some(c=>c.classList&&c.classList.contains('livebox'));
}
function mountRound(content, seq, isProv, agent){
  // 与真 `drawConv` 同款：`.crow.agent[data-seq]` 里放 `.gbox[data-agent]`——
  // 一轮里**每个 agent 各一个块**，直播区靠这个身份找对自己的那一块（§87）
  const row = mkEl('div');
  row.className = 'crow agent' + (isProv ? ' live-prov' : '');
  row.dataset.seq = String(seq);
  const av = mkEl('div'); av.className = 'avatar'; row.appendChild(av);
  const col = mkEl('div'); col.className = 'ccol'; row.appendChild(col);
  const g = mkEl('div'); g.className = 'gbox';
  g.dataset.agent = String(agent || 'Main');
  col.appendChild(g);
  content.appendChild(row);
  return {row, gbox:g};
}
// 当前 agent 的**流式事件**（新结构：内容来自服务端推的事件，不再是合成的步骤条目）
function stepsOf(aid){
  const a = aid || resolveAgentId(LIVE.agent) || 'Main';
  return LIVE.events[a] || [];
}
// 造一个与 `/rounds/{seq}` **同形状**的事件（服务端 `Agent._live_event` 的产物）
function ev(o){
  return Object.assign({id:'R7-E01', type:'tool_call', agent:'Main',
                        time:'2026-09-13T17:00:00', thinking:'', status:'',
                        role:'assistant', content:'', tool_calls:null,
                        tool_call_id:null}, o||{});
}
function resetLive(){
  DOM = resetDom();
  META = { id:'s1', status:'in_progress', round_list:[{seq:7, active_view:'A'}],
           registry:[{id:'Main', name:'主agent'},{id:'A', name:'域甲'}] };
  REG = { Main:{id:'Main', name:'主agent'}, A:{id:'A', name:'域甲'} };
  TAB = 'conv'; CUR = 's1';
  LIVE.job = 'j1'; LIVE.cur = 's1'; LIVE.seq = 7; LIVE.think=''; LIVE.ans='';
  LIVE.events = {}; LIVE.calls = {}; LIVE.doneSig = {}; LIVE.boxes = {}; LIVE.provs = {};
  LIVE.keep = null; LIVE.agent = '';
  LIVE.closing = false; LIVE.startedAt = Date.now(); LIVE.status='';
}

console.log('场景 F｜直播区：思考一行折叠 + 正文实时 MD + 一步结束不清空 + 挂在块内');
resetLive();
const F = mountRound(DOM.content, 7, false);      // 正式块已在
applyChunk({k:'think', s:'先想第一段。'});
renderLive();
applyChunk({k:'status', s:'等待模型响应…'});       // 一步收口
applyChunk({k:'think', s:'再想第二段。'});
applyChunk({k:'ans', s:'**加粗**的正文'});
renderLive();
{
  const box = F.gbox.querySelector('.livebox');
  if (process.env.WEBUI_RENDER_DUMP) console.log('   [dbg]', JSON.stringify({
    box:!!box, boxId:box&&box.id, kids:box&&box.children.length,
    html:box&&box._html.slice(0,60), TAB:TAB, CUR:CUR, liveCur:LIVE.cur,
    evLen:stepsOf().length, think:LIVE.think, ans:LIVE.ans }));
  if (!box) problems.push('F: 直播节点没挂在正式消息块里');
  if (liveboxDirectUnderContent()) problems.push('F: 直播节点挂到了 #content 直下（跑到块外面）');
  const tb = box && box.querySelector('.live-think');
  if (!tb) problems.push('F: 没有思考块');
  else if (tb.classList.contains('open')) problems.push('F: 思考块默认是展开的（应像工具卡一样一行折叠）');
  const head = box.querySelector('.live-think-head');
  if (head && !/点击展开/.test(head.textContent)) problems.push('F: 思考块头部没有"点击展开"提示');
  const body = box.querySelector('.live-think-body');
  if (body && body.textContent !== '再想第二段。') problems.push('F: 在飞思考文本不对');
  // ② 一步结束后内容不许消失：现在由**服务端推的事件**接住（不再是前端合成条目）
  applyChunk({k:'event', e:ev({id:'R7-E01', type:'tool_call', agent:'Main',
    thinking:'先想第一段。',
    tool_calls:[{id:'c9', function:{name:'read_file', arguments:'{"path":"a.py"}'}}]})});
  renderLive();
  const doneEl = box.querySelector('.live-done');
  const doneH = String((doneEl && doneEl.innerHTML) || '');
  if (!/先想第一段。/.test(doneH))
    problems.push('F: 一步结束后思考不见了（事件没接住内容 → "消失一下再出现"）');
  if (doneEl && doneEl.querySelectorAll('.tcard').length !== 1)
    problems.push('F: 事件里的工具卡没画出来');
  // ④ 正文必须走 md()，不是 textContent
  const ab = box.querySelector('.live-ans');
  if (!ab) problems.push('F: 没有正文节点');
  else if (!/<strong>加粗<\/strong>/.test(ab.innerHTML))
    problems.push('F: 正文没有实时渲染 MD（应出 <strong>）');
  console.log(`  F 事件条目=${stepsOf().length} 工具卡=${doneEl ? doneEl.querySelectorAll('.tcard').length : '?'} 思考折叠=${tb?!tb.classList.contains('open'):'?'} 正文MD=${!!(ab&&/<strong>/.test(ab.innerHTML))}`);
}

console.log('场景 G｜直播区：轮号未知时也要挂在**消息块**里（临时块），不再落到 #content');
resetLive();
LIVE.seq = 0;                                     // 刚发出去、分片还没回来
applyChunk({k:'think', s:'还没拿到轮号时的思考。'});
renderLive();
{
  const prov = DOM.content.querySelector('.crow.live-prov');
  if (!prov) problems.push('G: 没有造出临时消息块');
  if (!prov || !prov.querySelector('.gbox .livebox'))
    problems.push('G: 临时块里没有直播节点（会跑到块外面）');
  if (liveboxDirectUnderContent())
    problems.push('G: 直播节点挂到了 #content 直下（正是用户报的"思考在消息块外面"）');
  console.log(`   G 临时块=${!!prov} 挂在块内=${!!(prov&&prov.querySelector('.gbox .livebox'))}`);
}

// 造一份"本轮已落账"的会话数据：用户消息 + 一步工具调用（带思考）+ 最终回答。
// 场景 H/I/J 都要用它，否则 drawConv 会因为 CONV.seqs 为空**提前返回**——
// 那样测的就是空气（H 第一版正是这么空跑通过的，掩盖了 closing 之后直播区
// 仍被挂回的真问题）。
function realConv(){
  return { session:'s1', seqs:[7], cache:{7:{events:[
    {id:'R7-E01', type:'user', role:'user', content:'干活', time:''},
    {id:'R7-E02', type:'tool_call', role:'assistant', content:'',
     thinking:'这一轮的思考', time:'',
     tool_calls:[{id:'c1', function:{name:'read_file', arguments:'{}'}}]},
    {id:'R7-E04', type:'tool_result', role:'tool', content:'文件内容',
     tool_call_id:'c1', time:''},
    {id:'R7-E05', type:'final_answer', role:'assistant', content:'这一轮的正文',
     time:''}], blocks:[]}} };
}

console.log('场景 I｜直播区：跑轮中途整段重绘（切页/刷新）不许把直播区冲掉');
resetLive();
mountRound(DOM.content, 7, false);
CONV = realConv();                              // 跑到一半时事件区已有部分内容
META.round_list = [{seq:7, active_view:'A', events:2, steps_used:1}];
META.status = 'in_progress';
applyChunk({k:'think', s:'跑到一半的思考'});
applyChunk({k:'ans', s:'**跑到一半**的正文'});
renderLive();
{
  const before = DOM.content.querySelector('.livebox');
  drawConv(DOM.content, false);                 // 整段重绘（renderTab/轮询都会走）
  const after = DOM.content.querySelector('.livebox');
  if (!after) problems.push('I: 整段重绘把直播区冲掉了（会闪一下、甚至消失）');
  else if (before && after !== before) problems.push('I: 重绘换了新节点（展开状态/增量会丢）');
  const ab = after && after.querySelector('.live-ans');
  if (!ab || !/strong/.test(ab.innerHTML))
    problems.push('I: 重绘后正文的 MD 渲染丢了');
  if (liveboxDirectUnderContent()) problems.push('I: 重绘后直播节点跑到 #content 直下');
  console.log(`   I 重绘后节点在场=${!!after} 同一节点=${!!(before&&after===before)}`);
}

console.log('场景 H｜交接（closing）时不再回挂，避免一帧两份正文');
resetLive();
mountRound(DOM.content, 7, false);
CONV = realConv();                              // **必须有真数据**（否则空跑）
META.round_list = [{seq:7, active_view:'A', events:4, steps_used:2}];
applyChunk({k:'ans', s:'正式版正文'});
renderLive();
LIVE.closing = true;
drawConv(DOM.content, false);
{
  if (DOC_HAS_LIVEBOX()) problems.push('H: closing 之后直播节点又被挂回去了（正文会重复一帧）');
  const rows = DOM.content.querySelectorAll('.crow.agent');
  if (rows.length !== 1) problems.push(`H: 收尾后应有 1 个消息块，实际 ${rows.length}`);
  console.log(`   H closing 后直播节点在场=${DOC_HAS_LIVEBOX()} 消息块=${rows.length}`);
}

console.log('场景 J｜收尾这一次重绘自己就该把直播区收干净（旧的 sendTurn 轮询路径）');
resetLive();
LIVE.seq = 0;                                   // 刚发出去：正式块还没渲染
applyChunk({k:'think', s:'这一轮的思考'});
renderLive();                                   // → 临时消息块（块内）
LIVE.seq = 7;                                   // 服务端报回轮号
mountRound(DOM.content, 7, false);              // 正式块出现了
applyChunk({k:'ans', s:'这一轮的正文'});
renderLive();
LIVE.closing = true;                            // 收尾
CONV = realConv();
META.status = 'finished';
drawConv(DOM.content, true);
// **故意不调 liveDetach**：复现旧 sendTurn 轮询路径（它只 refresh、不撤直播区）。
// 这一批的根因就在这——收尾重绘之后直播区还被挂回去，于是同一轮出现两个块。
{
  const rows = DOM.content.querySelectorAll('.crow.agent');
  const provs = DOM.content.querySelectorAll('.crow.live-prov');
  const boxes = DOM.content.querySelectorAll('.livebox');
  if (provs.length) problems.push('J: 收尾后还留着临时消息块（那就是"第二个块"）');
  if (boxes.length) problems.push('J: 收尾重绘后直播节点又被挂回去了');
  if (rows.length !== 1) problems.push(`J: 收尾后本轮应有 1 个消息块，实际 ${rows.length}`);
  console.log(`   J 消息块=${rows.length} 临时块=${provs.length} 直播节点=${boxes.length}`);
}

console.log('场景 K｜开新一轮先清残留 + sseFinish 幂等');
resetLive();
LIVE.seq = 0;
applyChunk({k:'think', s:'上一轮漏撤的思考'});
renderLive();                                   // 造出"残留的临时块 + 直播节点"
{
  const beforeP = DOM.content.querySelectorAll('.crow.live-prov').length;
  liveAttach('jx');                             // 不 await：清扫发生在 await 之前
  const p = DOM.content.querySelectorAll('.crow.live-prov').length;
  const b = DOM.content.querySelectorAll('.livebox').length;
  if (beforeP !== 1) problems.push('K: 前置条件没造出来（应有 1 个残留临时块）');
  if (p || b) problems.push('K: 开新一轮没有清掉上一轮的残留直播 DOM');
  const job1 = LIVE.job;
  sseFinish();                                  // 幂等：连调两次不许出错
  sseFinish();
  if (job1 !== 'jx') problems.push('K: LIVE.job 不对');
  console.log(`   K 残留临时块 ${beforeP}→${p} 直播节点=${b}`);
}
console.log('场景 L｜活轮不许套"无落点（机制生效前）"，轮号一到就要并回正式块');
resetLive();
LIVE.seq = 0;                                   // 轮号还没到手（POST 刚返回）
META.round_list = [{seq:7, active_view:'A', events:0, steps_used:1}];
applyChunk({k:'think', s:'这一轮的思考'});
renderLive();
{
  const prov = DOM.content.querySelector('.crow.live-prov');
  if (!prov) problems.push('L: 没有造出临时块');
  // 查 `innerHTML` 串而不是子节点的 textContent：桩的 innerHTML 解析不搬文本，
  // 用 textContent 断言会**空跑**（写这条时踩到——它让旧的"无落点"行为也能过）。
  if (prov && /无落点/.test(prov.innerHTML))
    problems.push('L: 活轮的临时块套了历史标签"无落点（机制生效前）"');
  // 轮号到了 + 正式块渲染出来 → 直播内容必须并回正式块，临时块撤掉
  mountRound(DOM.content, 7, false);
  syncLiveSeq(7);
  const prov2 = DOM.content.querySelector('.crow.live-prov');
  const rows = DOM.content.querySelectorAll('.crow.agent');
  if (prov2) problems.push('L: 轮号到了还留着临时块（就是"思考被复制一份到下面"）');
  if (!DOM.content.querySelector('.crow.agent[data-seq="7"] .livebox'))
    problems.push('L: 轮号到了却没并回正式消息块');
  if (rows.length !== 1) problems.push(`L: 应只剩 1 个消息块，实际 ${rows.length}`);
  console.log(`   L 临时块残留=${!!prov2} 消息块=${rows.length}`);
}

console.log('场景 M｜回答产出后进"整理"：状态要更新、内容不许消失');
resetLive();
mountRound(DOM.content, 7, false);
CONV = realConv();
META.round_list = [{seq:7, active_view:'A', events:2, steps_used:1}];
META.status = 'in_progress';
applyChunk({k:'ans', s:'**最终**回答'});
renderLive();
// 服务端在回答落成**事件**之后推的状态（2026-09-13 新增整理段；此前这 100+ 秒
// 一个分片都不推，界面停在"回答中…"不动 —— 用户报"卡到回答中了"）
applyChunk({k:'event', e:ev({id:'R7-E01', type:'final_answer', agent:'Main',
  role:'assistant', content:'**最终**回答'})});
applyChunk({k:'status', s:'回答已产出，正在整理上下文…'});
renderLive();
{
  const rl = document.getElementById('runline');
  const has = rl && /整理/.test(rl.innerHTML);
  if (!has) problems.push('M: 运行线没显示"正在整理"（用户会以为卡死）');
  const box = DOM.content.querySelector('.livebox');
  const done = box && box.querySelector('.live-done');
  if (!done || !/strong/.test(done.innerHTML))
    problems.push('M: 进整理后回答消失了（应留在完成区，等正式渲染接管）');
  console.log(`   M 运行线含整理=${!!has} 回答留在完成区=${!!(done&&/strong/.test(done.innerHTML))}`);
}

console.log('场景 N｜后台维护观察器：有 pending 轮才武装、且只武装一次');
{
  const before = _timers.length;
  resetLive();
  META.round_list = [{seq:7, active_view:'A', org_state:'done'}];
  MAINT_WATCH = 0;
  watchMaintenance();
  if (_timers.length !== before) problems.push('N: 没有 pending 轮却武装了观察器');
  META.round_list = [{seq:7, active_view:'A', org_state:'done'},
                     {seq:8, active_view:'A', org_state:'pending'}];
  watchMaintenance();
  const armed = _timers.length - before;
  if (armed !== 1) problems.push(`N: 有 pending 轮应武装 1 个观察器，实际 ${armed}`);
  watchMaintenance();   // 幂等
  if (_timers.length - before !== 1) problems.push('N: 重复调用又武装了一个（不幂等）');
  _timers.slice(before).forEach(t=>clearInterval(t.id));
  MAINT_WATCH = 0;
  console.log(`   N 无 pending 不武装=true 有 pending 武装数=${armed} 幂等=${_timers.length===before}`);
}

console.log('场景 O｜一轮里两名 agent：各自的思考必须各进各的消息块');
resetLive();
// 与真 drawConv 一样：主 agent 一块、接手方 A 一块，**同一个 data-seq**
const O1 = mountRound(DOM.content, 7, false, 'Main');
const O2 = mountRound(DOM.content, 7, false, 'A');
META.round_list = [{seq:7, active_view:'A', events:2, steps_used:2}];
META.status = 'in_progress';
// 主 agent 先干一步（思考 + 工具调用），事件落地后交给 A
applyChunk({k:'think', s:'主 agent 的思考', ag:'Main'});
renderLive();
applyChunk({k:'status', s:'等待模型响应…', ag:'Main'});
applyChunk({k:'event', e:ev({id:'R7-E01', type:'tool_call', agent:'Main',
  thinking:'主 agent 的思考',
  tool_calls:[{id:'o1', function:{name:'route_to', arguments:'{"agent":"A"}'}}]})});
renderLive();
applyChunk({k:'think', s:'A 的思考', ag:'A'});            // 换手：轮到 A
applyChunk({k:'event', e:ev({id:'R7-E02', type:'tool_call', agent:'A',
  thinking:'A 的思考',
  tool_calls:[{id:'o2', function:{name:'read_file', arguments:'{}'}}]})});
renderLive();
{
  const boxMain = O1.gbox.querySelector('.livebox');
  const boxA = O2.gbox.querySelector('.livebox');
  if (!boxMain) problems.push('O: 主 agent 的块里没有它的直播节点');
  if (!boxA) problems.push('O: A 的块里没有它的直播节点（思考会被挂到主 agent 那块）');
  const mainDone = boxMain && boxMain.querySelector('.live-done');
  const aDone = boxA && boxA.querySelector('.live-done');
  if (!mainDone || !/主 agent 的思考/.test(mainDone.innerHTML))
    problems.push('O: 主 agent 那一步的思考没留在主 agent 的块里');
  if (!aDone || !/A 的思考/.test(aDone.innerHTML))
    problems.push('O: A 的思考没进 A 的块（用户报的 bug 就是这个）');
  if (boxA && /主 agent 的思考/.test(boxA.innerHTML))
    problems.push('O: 主 agent 的思考串到了 A 的块里');
  if (boxMain && /A 的思考/.test(boxMain.innerHTML))
    problems.push('O: A 的思考串到了主 agent 的块里（正是用户报的现象）');
  // 各自的工具卡也要在各自的块里
  const mCards = mainDone ? mainDone.querySelectorAll('.tcard').length : 0;
  const aCards = aDone ? aDone.querySelectorAll('.tcard').length : 0;
  if (mCards !== 1 || aCards !== 1)
    problems.push(`O: 工具卡没各归各块（主 ${mCards} / A ${aCards}）`);
  console.log(`   O 主块有内容=${!!mainDone} A块有内容=${!!aDone} 互不串=${!(boxA&&/主 agent 的思考/.test(boxA.innerHTML))} 卡=${mCards}/${aCards}`);
}

console.log('场景 P｜多步多 agent：干活那一方的那行**不许出现"什么都没有"的空档**');
resetLive();
const P1 = mountRound(DOM.content, 7, false, 'Main');
const P2 = mountRound(DOM.content, 7, false, 'B');
META.round_list = [{seq:7, active_view:'B', events:4, steps_used:3}];
META.status = 'in_progress';
{
  // 复现一次真实跑轮：主 agent 想一步 → 转交 → B 想两步（中间夹工具执行）
  const chunks = [
    {k:'think', s:'主 agent 先看这活归谁', ag:'Main'},
    {k:'status', s:'正在转交…', ag:'Main'},          // 收口 → Main 的完成区
    {k:'think', s:'B 第一步的思考', ag:'B'},
    {k:'status', s:'正在执行 read_file…', ag:'B'},   // 收口 → B 的完成区
    {k:'think', s:'B 第二步的思考', ag:'B'},
    {k:'ans', s:'**最终**答复', ag:'B'},
  ];
  // 注意：迷你 DOM 把 innerHTML 解析成**扁平**子节点（不还原嵌套），所以查询一律
  // **从 box 根出发**（产品代码也是这么查的）。从 `.live-think` 里再查它的子节点
  // 会永远找不到——这一条踩过一次，让场景 P 报了假空档。
  const visible = aid => {
    const row = (aid === 'Main' ? P1 : P2).gbox.querySelector('.livebox');
    if (!row) return false;
    const th = row.querySelector('.live-think');
    const head = row.querySelector('.live-think-head');
    const dn = row.querySelector('.live-done');
    const an = row.querySelector('.live-ans');
    const thinkOn = !!(th && th.style.display !== 'none' && head && head.textContent);
    const doneOn = !!(dn && String(dn.innerHTML).trim().length > 0);
    const ansOn = !!(an && an.style.display !== 'none'
      && String(an.innerHTML).trim().length > 0);
    return !!(thinkOn || doneOn || ansOn);
  };
  const trace = [];
  for (const c of chunks) {
    applyChunk(c);
    renderLive();
    const row = (c.ag === 'Main' ? P1 : P2).gbox.querySelector('.livebox');
    if (process.env.WEBUI_RENDER_DUMP) {
      const th = row && row.querySelector('.live-think');
      const hd = th && th.querySelector('.live-think-head');
      console.log(`   [dbg ${c.k}/${c.ag}] row=${!!row} box=${!!LIVE.boxes[c.ag]}`
        + ` think=${JSON.stringify(LIVE.think).slice(0, 20)}`
        + ` thinkDisp=${th ? JSON.stringify(th.style.display) : 'n/a'}`
        + ` head=${hd ? JSON.stringify(hd.textContent).slice(0, 30) : 'n/a'}`
        + ` html=${row ? JSON.stringify(String(row.innerHTML).slice(0, 40)) : 'n/a'}`);
    }
    trace.push(`${c.k}/${c.ag}:Main=${visible('Main') ? '有' : '空'}`
      + `,B=${visible('B') ? '有' : '空'}`);
  }
  // 前两条是"主 agent 在转交"，B 还没轮到，允许空；从 B 开始干活起不许空
  const afterB = trace.slice(2);
  if (afterB.some(t => t.endsWith('B=空'))) {
    problems.push('P: B 干活期间那一行出现过"什么都没有"的空档（=用户看到的一闪一闪）');
  }
  console.log('   P ' + trace.join(' | '));
}

console.log('场景 Q｜事件区已画过一部分时：直播内容**不许被去重吃掉**（一闪一闪的真因）');
resetLive();
const Q1 = mountRound(DOM.content, 7, false, 'Main');
const Q2 = mountRound(DOM.content, 7, false, 'B');
META.round_list = [{seq:7, active_view:'B', events:4, steps_used:2}];
META.status = 'in_progress';
{
  // 模拟"切页/刷新后事件区已经画过一步"：往 B 的块里塞一个**正式**思考块
  // （它是 B 那一步的官方渲染，内容是"转交那一步"的——**和我这边的第 1 条
  // 不是同一条**，这正是按条数扣会错位的地方）
  const official = mkEl('div');
  official.className = 'think-box';
  const ob = mkEl('div'); ob.className = 'think-body';
  ob.textContent = '官方已渲染的（转交那一步的）思考';
  official.appendChild(ob);
  Q2.gbox.appendChild(official);
  Q2.gbox.appendChild(Q1.gbox.children[0] || mkEl('div'));   // 占位：保持 B 块非空

  applyChunk({k:'think', s:'B 真正的新思考', ag:'B'});
  renderLive();
  applyChunk({k:'status', s:'正在执行 read_file…', ag:'B'});
  applyChunk({k:'event', e:ev({id:'R7-E03', type:'tool_call', agent:'B',
    thinking:'B 真正的新思考',
    tool_calls:[{id:'q1', function:{name:'read_file', arguments:'{}'}}]})});
  renderLive();
  const box = Q2.gbox.querySelector('.livebox');
  const done = box && box.querySelector('.live-done');
  const txt = done ? String(done.innerHTML) : '';
  if (!/B 真正的新思考/.test(txt)) {
    problems.push('Q: 事件区已画过别的内容时，直播这一步的思考被去重吃掉了（=一闪一闪）');
  }
  console.log(`   Q 新思考还在=${/B 真正的新思考/.test(txt)}`);
}

console.log('场景 R｜运行中按**时间顺序**画：思考 → 工具卡 → 思考 → 最终回答（用户报的核心）');
resetLive();
const R1 = mountRound(DOM.content, 7, false, 'Main');
META.round_list = [{seq:7, active_view:'Main', events:4, steps_used:2}];
{
  // 服务端现在每落一个事件就推一份扁平副本（与 /rounds 同形状）
  const seq = [
    ev({id:'R7-E01', type:'tool_call', agent:'Main', thinking:'第一步：先看看文件',
        tool_calls:[{id:'c1', function:{name:'read_file', arguments:'{"path":"a.py"}'}}]}),
    ev({id:'R7-E02', type:'tool_result', agent:'Main', role:'tool',
        tool_call_id:'c1', content:'文件内容'}),
    ev({id:'R7-E03', type:'tool_call', agent:'Main', thinking:'第二步：改掉它',
        tool_calls:[{id:'c2', function:{name:'edit_file', arguments:'{"path":"a.py"}'}}]}),
    ev({id:'R7-E04', type:'final_answer', agent:'Main', role:'assistant',
        content:'**改好了**'}),
  ];
  const order = [];
  for (const e of seq) { applyChunk({k:'event', e}); renderLive(); }
  const box = R1.gbox.querySelector('.livebox');
  const done = box && box.querySelector('.live-done');
  const html = done ? String(done.innerHTML) : '';
  if (process.env.WEBUI_RENDER_DUMP) {
    console.log('   [dbg R2]', JSON.stringify({
      kids: done ? done.children.length : -1,
      cls: done ? [...done.children].map(c => String(c.className || '')) : [],
      res: done ? [...done.querySelectorAll('.t-result')].map(e => String(e.textContent || '')) : [],
      calls: Object.keys(LIVE.calls).map(k => k + '=' + JSON.stringify(LIVE.calls[k].result)),
    }));
  }
  if (!done) problems.push('R: 直播块没了');
  // 工具卡必须在：而且是**两张**（两次调用）
  const cards = done ? done.querySelectorAll('.tcard').length : 0;
  if (cards !== 2) problems.push(`R: 运行中应有 2 张工具卡，实际 ${cards}（工具块没画出来）`);
  // 顺序：思考1 → 卡1 → 思考2 → 卡2 → 正文
  const marks = [];
  if (done) {
    const walk = (el) => {
      for (const c of el.children) {
        const cl = String(c.className || '');
        if (cl.includes('think-box')) marks.push('think:' + c.textContent.slice(0, 3));
        else if (cl.includes('tcard')) marks.push('tool');
        else if (cl.includes('bub') && !cl.includes('live-ans')) marks.push('bub');
        walk(c);
      }
    };
    walk(done);
  }
  const want = ['think', 'tool', 'think', 'tool', 'bub'];
  const got = marks.map(m => m.startsWith('think') ? 'think' : m);
  if (got.join('>') !== want.join('>')) {
    problems.push(`R: 顺序不对：期望 ${want.join('>')}，实际 ${got.join('>')}`);
  }
  // 工具结果要回填进调用卡（不是新开一行）——**查 textContent**：回填写的是
  // textContent，迷你 DOM 的 innerHTML 不带它（这个假象踩过一次）；
  // 又因为迷你 DOM 把 innerHTML 解析成**扁平**子节点，`[data-call] .t-result`
  // 这种后代选择器不成立（第二次踩），故直接看所有 `.t-result`。
  const resTexts = done ? [...done.querySelectorAll('.t-result')]
    .map(el => String(el.textContent || '')) : [];
  if (!resTexts.some(t => t === '文件内容'))
    problems.push('R: 工具结果没有回填到调用卡里');
  // 最终回答要走 MD 渲染
  if (done && !/<strong>改好了<\/strong>/.test(String(done.innerHTML)))
    problems.push('R: 最终回答没走 MD 渲染');
  console.log(`   R 工具卡=${cards} 顺序=${got.join('>')} 结果已回填=${resTexts.some(t=>t==='文件内容')}`);
}

function DOC_HAS_LIVEBOX(){ return !!DOM.content.querySelector('.livebox'); }

console.log('');
if (problems.length) {
  console.log('渲染核对：失败 ' + problems.length + ' 项');
  problems.slice(0, 12).forEach(p => console.log('  ✗ ' + p));
  process.exit(1);
}
console.log('渲染核对：通过（15 个场景，无 undefined/NaN，正文无机制说明词，直播区四症状 + 收尾/轮号/整理态不变量全查）');
"""


def _static_ids(html: str) -> list[str]:
    """页面**静态标签**里的 id（剔掉 <script> 里的字符串，避免把脚本里的
    选择器名字当成静态节点——那会让 `getElementById` 骗过"节点不存在"）。"""
    markup = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.S)
    seen: list[str] = []
    for m in re.finditer(r'\bid="([\w-]+)"', markup):
        if m.group(1) not in seen:
            seen.append(m.group(1))
    return seen


def main() -> int:
    html = pathlib.Path("webui/index.html").read_text(encoding="utf-8")
    blocks = re.findall(r"<script[^>]*>(.*?)</script>", html, re.S)
    if not blocks:
        sys.exit("没有找到 <script> 块")
    out = pathlib.Path("output")
    out.mkdir(parents=True, exist_ok=True)
    (out / "_webui_render.js").write_text("\n;\n".join(blocks), encoding="utf-8")
    # 一个文件：DOM 桩 → webui 脚本 → 场景（同一作用域，故共享 META/VIEWSIZES…）
    prefix = PREFIX.replace("__STATIC_IDS__",
                            json.dumps(_static_ids(html), ensure_ascii=False))
    (out / "_webui_render_check.js").write_text(
        prefix + "\n;\n".join(blocks) + SCENARIOS, encoding="utf-8")
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
