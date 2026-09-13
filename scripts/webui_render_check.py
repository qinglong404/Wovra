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
// （直播骨架是扁平的，所以这样 `box.querySelector('#live-think-body')` 找得到）。
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
function parseInto(root, html){
  root.children = [];
  const re = /<(\w+)([^>]*)>/g;
  let m;
  while((m = re.exec(String(html||'')))){
    const attrs = m[2] || '';
    const idm = /\bid="([^"]*)"/.exec(attrs);
    const cm = /\bclass="([^"]*)"/.exec(attrs);
    if(!idm && !cm) continue;              // 只登记带 id/class 的（骨架全带）
    const c = mkEl(m[1]);
    if(idm) c.id = idm[1];
    if(cm) c.className = cm[1];
    root.appendChild(c);
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
global.setInterval = ()=>0;
global.clearInterval = ()=>{};
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
// "#livebox 挂在 #content 直下" = 没有消息块包着它（用户报的"跑到块外面"）。
// 注意用**直接子节点**判断：querySelector 会连后代一起命中，那样连正确挂载
// 都会被误判（我自己先踩了一次）。
function liveboxDirectUnderContent(){
  return [...DOM.content.children].some(c=>c.id==='livebox');
}
function mountRound(content, seq, isProv){
  const row = mkEl('div');
  row.className = 'crow agent' + (isProv ? ' live-prov' : '');
  row.dataset.seq = String(seq);
  const av = mkEl('div'); av.className = 'avatar'; row.appendChild(av);
  const col = mkEl('div'); col.className = 'ccol'; row.appendChild(col);
  const g = mkEl('div'); g.className = 'gbox'; col.appendChild(g);
  content.appendChild(row);
  return {row, gbox:g};
}
function resetLive(){
  DOM = resetDom();
  META = { id:'s1', status:'in_progress', round_list:[{seq:7, active_view:'A'}],
           registry:[{id:'Main', name:'主agent'},{id:'A', name:'域甲'}] };
  REG = { Main:{id:'Main', name:'主agent'}, A:{id:'A', name:'域甲'} };
  TAB = 'conv'; CUR = 's1';
  LIVE.job = 'j1'; LIVE.cur = 's1'; LIVE.seq = 7; LIVE.think=''; LIVE.ans='';
  LIVE.events = []; LIVE.doneSig=''; LIVE.prov = null; LIVE.keep = null;
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
  const box = F.gbox.querySelector('#livebox');
  if (process.env.WEBUI_RENDER_DUMP) console.log('   [dbg]', JSON.stringify({
    box:!!box, boxId:box&&box.id, kids:box&&box.children.length,
    html:box&&box._html.slice(0,60), TAB:TAB, CUR:CUR, liveCur:LIVE.cur,
    evLen:LIVE.events.length, think:LIVE.think, ans:LIVE.ans }));
  if (!box) problems.push('F: 直播节点没挂在正式消息块里');
  if (liveboxDirectUnderContent()) problems.push('F: 直播节点挂到了 #content 直下（跑到块外面）');
  const tb = box && box.querySelector('#live-think');
  if (!tb) problems.push('F: 没有思考块');
  else if (tb.classList.contains('open')) problems.push('F: 思考块默认是展开的（应像工具卡一样一行折叠）');
  const head = box.querySelector('#live-think-head');
  if (head && !/点击展开/.test(head.textContent)) problems.push('F: 思考块头部没有"点击展开"提示');
  const body = box.querySelector('#live-think-body');
  if (body && body.textContent !== '再想第二段。') problems.push('F: 在飞思考文本不对');
  // ② 一步结束的内容不许消失：应当留在已完成区
  const kept = LIVE.events.map(e=>e.text).join('|');
  if (!kept.includes('先想第一段。')) problems.push('F: 一步结束后思考被清空（会"消失一下再出现"）');
  if (!kept.includes('**加粗**的正文') && LIVE.ans!=='**加粗**的正文')
    problems.push('F: 正文既不在已完成区也不在飞（丢了）');
  // ④ 正文必须走 md()，不是 textContent
  const ab = box.querySelector('#live-ans');
  if (!ab) problems.push('F: 没有正文节点');
  else if (!/<strong>加粗<\/strong>/.test(ab.innerHTML))
    problems.push('F: 正文没有实时渲染 MD（应出 <strong>）');
  console.log(`  F 已完成条目=${LIVE.events.length} 思考折叠=${tb?!tb.classList.contains('open'):'?'} 正文MD=${!!(ab&&/<strong>/.test(ab.innerHTML))}`);
}

console.log('场景 G｜直播区：轮号未知时也要挂在**消息块**里（临时块），不再落到 #content');
resetLive();
LIVE.seq = 0;                                     // 刚发出去、分片还没回来
applyChunk({k:'think', s:'还没拿到轮号时的思考。'});
renderLive();
{
  const prov = DOM.content.querySelector('.crow.live-prov');
  if (!prov) problems.push('G: 没有造出临时消息块');
  if (!prov || !prov.querySelector('.gbox #livebox'))
    problems.push('G: 临时块里没有直播节点（会跑到块外面）');
  if (liveboxDirectUnderContent())
    problems.push('G: 直播节点挂到了 #content 直下（正是用户报的"思考在消息块外面"）');
  console.log(`   G 临时块=${!!prov} 挂在块内=${!!(prov&&prov.querySelector('.gbox #livebox'))}`);
}

console.log('场景 H｜直播区：交接（closing）时不再回挂，避免一帧两份正文');
resetLive();
const H = mountRound(DOM.content, 7, false);
applyChunk({k:'ans', s:'正式版正文'});
renderLive();
LIVE.closing = true;
drawConv(DOM.content, false);
{
  if (DOC_HAS_LIVEBOX()) problems.push('H: closing 之后直播节点又被挂回去了（正文会重复一帧）');
  console.log(`   H closing 后直播节点在场=${DOC_HAS_LIVEBOX()}`);
}

console.log('场景 I｜直播区：跑轮中途整段重绘（切页/刷新）不许把直播区冲掉');
resetLive();
mountRound(DOM.content, 7, false);
applyChunk({k:'think', s:'跑到一半的思考'});
applyChunk({k:'ans', s:'**跑到一半**的正文'});
renderLive();
{
  const before = DOM.content.querySelector('#livebox');
  // 跑轮中途来一次整段重绘（renderTab/刷新都会走这条）
  CONV = { session:'s1', seqs:[7], cache:{7:{events:[], blocks:[]}} };
  META.round_list = [{seq:7, active_view:'A', events:0, steps_used:1}];
  META.status = 'in_progress';
  drawConv(DOM.content, false);
  const after = DOM.content.querySelector('#livebox');
  if (!after) problems.push('I: 整段重绘把直播区冲掉了（会闪一下、甚至消失）');
  else if (before && after !== before) problems.push('I: 重绘换了新节点（展开状态/增量会丢）');
  const ab = after && after.querySelector('#live-ans');
  if (!ab || !/strong/.test(ab.innerHTML))
    problems.push('I: 重绘后正文的 MD 渲染丢了');
  if (liveboxDirectUnderContent()) problems.push('I: 重绘后直播节点跑到 #content 直下');
  console.log(`   I 重绘后节点在场=${!!after} 同一节点=${!!(before&&after===before)}`);
}
function DOC_HAS_LIVEBOX(){ return !!DOM.content.querySelector('#livebox'); }

console.log('');
if (problems.length) {
  console.log('渲染核对：失败 ' + problems.length + ' 项');
  problems.slice(0, 12).forEach(p => console.log('  ✗ ' + p));
  process.exit(1);
}
console.log('渲染核对：通过（9 个场景，无 undefined/NaN，正文无机制说明词，直播区四症状全查）');
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
