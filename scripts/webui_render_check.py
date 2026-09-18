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
// （骨架的层级现在按真嵌套还原，后代选择器可用；见 `parseInto`）。
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
  const s = String(html||'');
  const re = /<\/?(\w+)((?:"[^"]*"|'[^']*'|[^>])*)>/g;
  // 栈里每层记住 `{el, start}`：元素闭合时用 `s.slice(start, 闭合位置)` 填 `_html`。
  // **parsed 子元素的 innerHTML 也必须有值**（2026-09-13 修，第七个同类假象）：
  // 原实现只有被赋值的那一层有 `_html`，于是 `ab.innerHTML`（查 MD 渲染结果）
  // 恒为空 → 场景 F/I 假红。真 DOM 里查谁都有 innerHTML，仪器就得一样。
  const frames = [{el:root, start:0}];
  let m, last = 0;
  // **标签之间的文本要落进 textContent**（第六个同类假象）：原实现只认标签，
  // 解析出来的元素 `textContent` 恒为空串。真 DOM 的语义是**父含子孙文本**，
  // 所以这里往整条栈上累加。
  const eatText = upto => {
    const text = s.slice(last, upto);
    if(text) frames.forEach(f => { f.el.textContent += text; });
    last = upto;
  };
  const closeFrame = (idx, upto) => {
    for(let i=frames.length-1;i>0;i--){
      if(frames[i].el.tagName===String(idx).toUpperCase()){
        frames[i].el._html = s.slice(frames[i].start, upto);
        frames[i].el._parsed = true;
        frames.length=i;
        return;
      }
    }
  };
  while((m = re.exec(s))){
    eatText(m.index);
    const raw = m[0], tag = String(m[1]).toLowerCase();
    if(raw.startsWith('</')){
      closeFrame(tag, m.index);
      last = re.lastIndex;
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
      frames[frames.length-1].el.appendChild(c);
      c.textContent = ''; c._html = '';             // 由后面的文本/子标签累加填
      if(selfClose){ c._parsed = true; }
      else frames.push({el:c, start:re.lastIndex});
    }else if(!selfClose){
      // 没身份的元素也要占一层，否则它的子元素会挂错层（层级照样要真）
      const ghost = mkEl(tag);
      frames[frames.length-1].el.appendChild(ghost);
      ghost._ghost = true; ghost.textContent = ''; ghost._html = '';
      frames.push({el:ghost, start:re.lastIndex});
    }
    last = re.lastIndex;
  }
  eatText(s.length);
  // 没闭合的（截断的 HTML）也把剩下的原文给它
  for(let i=frames.length-1;i>0;i--){
    if(!frames[i].el._parsed){ frames[i].el._html = s.slice(frames[i].start); frames[i].el._parsed = true; }
  }
}
// **DOM 查询计数**（性能体检用）：一次重绘查了多少次 DOM、扫到多少个节点。
// "前端必须流畅"是硬指标，而前端卡顿的典型来源就是"每次重绘都全量扫一遍 DOM"——
// 次数随**条目数**涨（O(n)），就会在长轮次里肉眼可见地卡。这里把它变成可断言的数。
const DOMQ={n:0,nodes:0};
function domqReset(){DOMQ.n=0;DOMQ.nodes=0}
function mkEl(tag){
  const e = {
    tagName:String(tag||'div').toUpperCase(), id:'', className:'',
    textContent:'', value:'', dataset:{}, children:[],
    parentNode:null, parentElement:null, isConnected:true,
    // **真存储**（2026-09-13 修）：原先是假 Proxy（get 恒 ''、set 丢弃），于是
    // `display='none'` 存不下来 → **所有"隐藏/显示"断言都是假的**（场景 P 的
    // "那一行有没有空档"、Q 的隐藏、R 的显隐全测不出来）。这类假象已经骗过
    // 我五次；仪器得跟产品代码一个语义：写过就是写过。
    style:{},
    _html:'',
    appendChild(c){ c.parentNode=this; c.parentElement=this; c.isConnected=true;
                    this.children.push(c); return c; },
    removeChild(c){ const i=this.children.indexOf(c); if(i>=0)this.children.splice(i,1);
                    c.parentNode=null; c.isConnected=false; },
    remove(){ if(this.parentNode)this.parentNode.removeChild(this); },
    insertBefore(c,ref){ if(!ref)return this.appendChild(c);
      const i=this.children.indexOf(ref); if(i<0)return this.appendChild(c);
      c.parentNode=this; c.parentElement=this; c.isConnected=true;
      this.children.splice(i,0,c); return c; },
    // **closest 必须真走祖先链**（2026-09-14 修桩）：原先是 `return null` 的空桩——
    // 产品代码里 `box.parentNode.closest('.crow.live-prov')`／`hit.closest('.crow')`
    // 这类判定在仪器里恒为空 → 场景空跑（AM/AG 都被它骗过）。
    closest(sel){ let p=this; while(p){ if(matchOne(p,sel))return p; p=p.parentNode; } return null; },
    addEventListener(t,f){ (this._h=this._h||{})[t]=f; }, removeEventListener(){},
    fire(t,ev){ const f=this._h&&this._h[t]; if(f) f(ev||{}); },
    setAttribute(){}, getAttribute(){return null},
    scrollIntoView(){}, focus(){}, click(){}, replaceChildren(){ this.children=[]; },
    querySelector(sel){ DOMQ.n++; const g=matchAll(this,sel); DOMQ.nodes+=g.length;
                        return g.length?g[0]:null; },
    querySelectorAll(sel){ DOMQ.n++; const g=matchAll(this,sel); DOMQ.nodes+=g.length; return g; },
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
      // `[k]`（只看存在）与 `[k="v"]`（值）两种都要支持（2026-09-15 加：工作目录
      // 列表的委托判定是 `closest('[data-p]')`，只有后者会**静默匹配不到** → 空跑）
      const a = /^\[([\w-]+)(?:=(?:"([^"]*)"|'([^']*)'|([^"\]]*)))?\]$/.exec(t);
      if(!a) return false;
      // `data-seq` → `dataset.seq`（真 DOM 就是这么映射的，桩要跟上，
      // 否则 `.crow[data-seq="7"]` 永远匹配不到——踩过）
      const key = a[1].startsWith('data-')
        ? a[1].slice(5).replace(/-(\w)/g, (_m,c)=>c.toUpperCase())
        : a[1];
      const v = (key==='id') ? el.id
        : (el.dataset[key]!==undefined ? el.dataset[key] : el.getAttribute(a[1]));
      const hasVal = a[2] !== undefined || a[3] !== undefined || a[4] !== undefined;
      if(hasVal){
        const want = a[2]!==undefined ? a[2] : (a[3]!==undefined ? a[3] : a[4]);
        if(String(v===undefined||v===null?'':v) !== want) return false;
      }else if(v===undefined || v===null) return false;
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
global.setTimeout = (fn, ms)=>{ const id=++_timerSeq; _timers.push({id,fn,ms,once:true}); return id; };
global.clearTimeout = ()=>{};
global.fetch = ()=>Promise.reject(new Error('no net in check'));
global.requestAnimationFrame = ()=>0;
global.CSS = { escape:(s)=>String(s) };
// marked/purify 是 vendor 里的真库（浏览器里加载）；这里给**等价的最小替身**，
// 目的是证明"正文走的是 md() 这条渲染路"，而不是 textContent 直出。
global.marked = { parse:(s)=>String(s||'').replace(/\*\*(.+?)\*\*/g,'<strong>$1</strong>') };
global.DOMPurify = { sanitize:(s)=>String(s||'') };
global.hljs = null;
const ES_INSTANCES = [];
global.EventSource = function(){ const es={close(){}, addEventListener(){}};
  ES_INSTANCES.push(es); return es; };   // 场景 AF 要驱动 onmessage

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
// **异步场景的待办**（2026-09-15 加）：整段场景是同步块，`await` 的续体只在微任务里跑——
// 所以需要"等待网络桩 + 断言画出来的 DOM"的场景把断言包成 promise 推到这里，
// 收尾统一 `Promise.all` 后再出结论（node 退出前会先排空微任务队列）。
const __deferred = [];
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

console.log('场景 E2｜V4 fork 之后（各家有自己的上下文）');
META = { id:'s4', registry:[
  {id:'Main', name:'主agent', status:'active', ctx_cur:120000, ctx_peak:130000, window:1000000, rounds:9, steps:40},
  {id:'A', name:'附件', status:'active', ctx_cur:42000, ctx_peak:46000, window:1000000, rounds:3, steps:11, forked_at:3}],
  round_list:[] };
VIEWSIZES = { sig:'f', window:1000000, basis:'tiktoken:cl100k_base', shared:false, forked:true,
  agents:[{id:'Main', name:'主agent', is_main:true, count:0, total_chars:0, tokens:120000, forked_at:0},
          {id:'A', name:'附件', is_main:false, count:0, total_chars:0, tokens:42000, forked_at:3}],
  note:'各 agent 已有自己的上下文' };
renderCtxbar();
html = byId('ctxbar').innerHTML;
check('E2 各家一行', html, 2);
if (!html.includes('120K') || !html.includes('42K')) problems.push('E2: 各家实测体量没显示');
if (html.includes('0 条消息')) problems.push('E2: 读了 fork 行没有的 count（显示 0 条消息）');
if (html.includes('重组后')) problems.push('E2: fork 会话仍写"重组后"（V4 没有重组这回事）');

console.log('场景 E3｜上下文窗口**按步更新**（/plan 的现读观测要真的重画顶部条）');
// 共享态（V4）：这份体量来自注册表**每步都在刷**的观测，不是投影
META = { id:'s5', registry:[
  {id:'Main', name:'主agent', status:'active', ctx_cur:120000, ctx_peak:120000, window:1000000, rounds:2, steps:9},
  {id:'A', name:'域甲', status:'active', ctx_cur:0, ctx_peak:0, window:0, rounds:0, steps:0}],
  round_list:[] };
VIEWSIZES = { sig:'s', window:1000000, basis:'tiktoken:cl100k_base', shared:true,
  agents:[{id:'Main', name:'主agent', is_main:true, count:0, total_chars:0, tokens:999999}] };
renderCtxbar();
html = byId('ctxbar').innerHTML;
if (!html.includes('120K')) problems.push('E3: 共享态没读注册表观测（读了投影 999999）');
// 行、色点、进度条都要走**有样式的那几类**：产品 CSS 里只有 `.ctx-cell`/`.ctx-dot`/
// `.ctx-bar`，自造 `ctxrow`/`dot`/`nm` 等于没样式——页面上一片空（截图核对抓到的）
check('E3 共享态一行', html, 1);
if (!html.includes('class="ctx-dot"')) problems.push('E3: 共享态的色点没样式（页面上一片空）');
if (!html.includes('class="ctx-bar"')) problems.push('E3: 共享态的占比条没样式');
// 服务端每 tick 报的现读值：合并进来就要立刻重画
applyPlanCtx({ ctx:[{id:'Main', name:'主agent', ctx_cur:131072, ctx_peak:131072, window:1000000}] });
html = byId('ctxbar').innerHTML;
if (!html.includes('131.1K')) problems.push('E3: applyPlanCtx 没把新观测画上屏（按步更新没生效）');
if (!html.includes('13.1%')) problems.push('E3: 占比没跟着新观测走');
// 没变化时不许白刷（返回 false）
if (applyPlanCtx({ ctx:[{id:'Main', name:'主agent', ctx_cur:131072, ctx_peak:131072, window:1000000}] })) {
  problems.push('E3: 观测没变却报"有变化"（每 tick 白刷 DOM）');
}
// fork 态同样按步更新（那一支读的也是同一个 ctx_cur）
META = { id:'s6', registry:[
  {id:'Main', name:'主agent', status:'active', ctx_cur:200000, ctx_peak:200000, window:1000000, rounds:9, steps:40},
  {id:'A', name:'附件', status:'active', ctx_cur:42000, ctx_peak:46000, window:1000000, rounds:3, steps:11, forked_at:3}],
  round_list:[] };
VIEWSIZES = { sig:'f2', window:1000000, basis:'tiktoken:cl100k_base', shared:false, forked:true,
  agents:[{id:'Main', name:'主agent', is_main:true, count:0, total_chars:0, tokens:200000, forked_at:0},
          {id:'A', name:'附件', is_main:false, count:0, total_chars:0, tokens:42000, forked_at:3}] };
renderCtxbar();
applyPlanCtx({ ctx:[{id:'A', name:'附件', ctx_cur:51200, ctx_peak:51200, window:1000000}] });
html = byId('ctxbar').innerHTML;
check('E3 fork 按步', html, 2);
if (!html.includes('51.2K')) problems.push('E3: fork 态没按步更新（还停在 42K）');

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

// **清掉"官方已画过"的覆盖**：夹具 `realConv()` 自带 seq 7 的事件，而模拟的直播事件
// 常用同一批 id（R7-E01…）——不清掉的话 `coveredEvIds()` 会认为"官方画过"从而跳过它们
// （那是产品**正确**行为；这里是夹具冲突）。真实场景里官方只覆盖"刷新那一刻的前缀"。

/* **把一棵子树里的文本都收出来**（含每个节点自己的 innerHTML 串）。
   为什么不能直接读 `root.innerHTML`：`mkEl` 直接造出来的节点只有一个空 `_html`，
   只有被 `innerHTML=` 赋过值的节点才有值——而直播内容正是在子节点的 `_html` 里。
   不遍历就会"数出零个"，重复检查永远不触发（咬合空跑，踩过一次）。 */
function domAllText(root){
  const out=[];
  const walk=el=>{
    if(!el)return;
    if(el._html)out.push(String(el._html));
    if(!el.children||!el.children.length){
      if(el.textContent&&!el._html)out.push(String(el.textContent));
      return;
    }
    el.children.forEach(walk);
  };
  walk(root);
  return out.join('\u0000');
}

function clearOfficial(seq){
  // **改 id，不清内容**：官方那边照旧有东西可画（H/J 要断言正式块在场），但 id 与
  // 模拟的直播事件不撞——真实情形就是"官方只覆盖前缀，直播重放同一批 id"。
  const d = CONV && CONV.cache && CONV.cache[seq];
  if (!d) return;
  (d.events || []).forEach(e => {
    if (e && e.id && !String(e.id).startsWith('O-')) e.id = 'O-' + e.id;
  });
  // 覆盖集合按"数组身份+长度"记忆化——这里**原地**改了 id，得让它重算
  if (typeof _covArr !== 'undefined') { _covArr = null; _covLen = -1; _covSet = null; }
}

function resetLive(){
  DOM = resetDom();
  META = { id:'s1', status:'in_progress', round_list:[{seq:7, active_view:'A'}],
           registry:[{id:'Main', name:'主agent'},{id:'A', name:'域甲'}] };
  REG = { Main:{id:'Main', name:'主agent'}, A:{id:'A', name:'域甲'} };
  clearOfficial(7);      // 上一个场景可能在 CONV.cache[7] 里留了东西
  TAB = 'conv'; CUR = 's1';
  LIVE.job = 'j1'; LIVE.cur = 's1'; LIVE.seq = 7; LIVE.think=''; LIVE.ans='';
  LIVE.events = {}; LIVE.calls = {}; LIVE.doneSig = {}; LIVE.boxes = {}; LIVE.provs = {};
  LIVE.seenEv = {}; LIVE.callsVer = 0; LIVE.filledSig = {}; LIVE.step = -1;
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
  const tailEl = box && box.querySelector('.live-tail');
  const tb = tailEl && tailEl.querySelector('.think-box');
  if (!tb) problems.push('F: 在飞思考没画在尾部区块里');
  else if (tb.classList.contains('open')) problems.push('F: 思考块默认是展开的（应像工具卡一样一行折叠）');
  const head = tailEl && tailEl.querySelector('.think-head');
  if (head && !/点击展开/.test(head.textContent)) problems.push('F: 思考块头部没有"点击展开"提示');
  const body = tailEl && tailEl.querySelector('.think-body');
  const bodyText = String((body && body.textContent) || '');
  if (!/再想第二段。/.test(bodyText)) problems.push('F: 在飞思考文本不对');
  // **状态到达不许清掉上一段**（2026-09-14 修，用户报"思考完/输出完会消失一下"）：
  // 旧断言期望这里只剩第二段——那正是被报障的行为；收口改由**事件到达**做（见下方 ②）。
  if (!/先想第一段。/.test(bodyText))
    problems.push('F: 状态一到就清了上一段思考（会"消失一下再出现"）');
  // ④ 在飞正文必须走 md()（不是 textContent）——**在事件到达之前**查（事件一到，
  //    这段文本就由事件条目接管，在飞缓冲被清空，尾部区块清空是**正确**行为）
  const ab = tailEl && tailEl.querySelector('.bub');
  if (!ab) problems.push('F: 没有在飞正文节点');
  else if (!/<strong>加粗<\/strong>/.test(ab.innerHTML))
    problems.push('F: 在飞正文没有实时渲染 MD（应出 <strong>）');
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
  // 事件接管之后：尾部区块必须是**空的**（不是隐藏着旧文本）
  const tailNow = box.querySelector('.live-tail');
  if (tailNow && String(tailNow.innerHTML || '').trim() !== '')
    problems.push('F: 事件到达后尾部还留着旧内容（用户报的"卡住两行"）');
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
CONV = realConv();
clearOfficial(7);                              // 跑到一半时事件区已有部分内容
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
  const ab = after && after.querySelector('.live-tail .bub');
  if (!ab || !/strong/.test(ab.innerHTML))
    problems.push('I: 重绘后正文的 MD 渲染丢了');
  if (liveboxDirectUnderContent()) problems.push('I: 重绘后直播节点跑到 #content 直下');
  console.log(`   I 重绘后节点在场=${!!after} 同一节点=${!!(before&&after===before)}`);
}

console.log('场景 H｜交接（closing）时不再回挂，避免一帧两份正文');
resetLive();
mountRound(DOM.content, 7, false);
CONV = realConv();
clearOfficial(7);                              // **必须有真数据**（否则空跑）
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
clearOfficial(7);
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
clearOfficial(7);
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
  // 复现一次真实跑轮：主 agent 想一步 → 转交 → B 想两步（中间夹工具执行）。
  // **每步的正文由紧随其后的事件接住**（§92：直播流推事件）——所以事件必须跟着
  // 那一步的文本，否则 `status` 清了在飞缓冲之后就真的什么都不剩了（这不是空档
  // bug，是测试没喂事件）。
  const chunks = [
    {k:'think', s:'主 agent 先看这活归谁', ag:'Main'},
    {k:'event', e:ev({id:'R7-E01', type:'tool_call', agent:'Main',
      thinking:'主 agent 先看这活归谁',
      tool_calls:[{id:'p1', function:{name:'route_to', arguments:'{"agent":"B"}'}}]})},
    {k:'status', s:'正在转交…', ag:'Main'},
    {k:'think', s:'B 第一步的思考', ag:'B'},
    {k:'event', e:ev({id:'R7-E02', type:'tool_call', agent:'B',
      thinking:'B 第一步的思考',
      tool_calls:[{id:'p2', function:{name:'read_file', arguments:'{}'}}]})},
    {k:'status', s:'正在执行 read_file…', ag:'B'},
    {k:'think', s:'B 第二步的思考', ag:'B'},
    {k:'event', e:ev({id:'R7-E03', type:'final_answer', agent:'B',
      role:'assistant', content:'**最终**答复'})},
  ];
  // 注意：迷你 DOM 把 innerHTML 解析成**扁平**子节点（不还原嵌套），所以查询一律
  // **从 box 根出发**（产品代码也是这么查的）。
  // 会永远找不到——这一条踩过一次，让场景 P 报了假空档。
  const visible = aid => {
    const row = (aid === 'Main' ? P1 : P2).gbox.querySelector('.livebox');
    if (!row) return false;
    const tl = row.querySelector('.live-tail');
    const th = tl && tl.querySelector('.think-box');
    const head = tl && tl.querySelector('.think-head');
    const dn = row.querySelector('.live-done');
    const an = tl && tl.querySelector('.bub');
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
      const hd = row && row.querySelector('.live-think-head');
      console.log(`   [dbg ${c.k}/${c.ag}] row=${!!row} box=${!!LIVE.boxes[c.ag]}`
        + ` think=${JSON.stringify(LIVE.think).slice(0, 20)}`
        + ` thinkDisp=${th ? JSON.stringify(th.style.display) : 'n/a'}`
        + ` head=${hd ? JSON.stringify(hd.textContent).slice(0, 30) : 'n/a'}`
        + ` html=${row ? JSON.stringify(String(row.innerHTML).slice(0, 40)) : 'n/a'}`);
    }
    const who = String(c.ag || (c.e && c.e.agent) || '');
    trace.push(`${c.k}/${who}:Main=${visible('Main') ? '有' : '空'}`
      + `,B=${visible('B') ? '有' : '空'}`);
  }
  // 前两条是"主 agent 在转交"，B 还没轮到，允许空；从 B 开始干活起不许空
  const afterB = trace.slice(4);
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

console.log('场景 S｜过程发言之后继续调工具：尾部**不许留**着上一次的正文（用户报的"卡住两行"）');
resetLive();
const S1 = mountRound(DOM.content, 7, false, 'Main');
META.round_list = [{seq:7, active_view:'Main', events:4, steps_used:2}];
META.status = 'in_progress';
{
  // 用户描述的顺序：思考 → 给出回复（过程发言）→ 继续推进事件、调用工具
  const seq = [
    {k:'think', s:'先想一下'},
    {k:'ans',   s:'两行正文。\n第二行。'},                  // 过程发言（边做边说）
    {k:'status', s:'正在执行 read_file…'},                 // 工具执行前
    {k:'event', e:ev({id:'R7-E01', type:'tool_call', agent:'Main',
        thinking:'先想一下', content:'两行正文。\n第二行。',
        tool_calls:[{id:'s1', function:{name:'read_file', arguments:'{}'}}]})},
    {k:'event', e:ev({id:'R7-E02', type:'tool_result', agent:'Main', role:'tool',
        tool_call_id:'s1', content:'读到了'})},
    {k:'think', s:'看完了，接着改'},
    {k:'ans',   s:'改这里的第二段。'},                      // 第二步的过程发言
    {k:'event', e:ev({id:'R7-E03', type:'tool_call', agent:'Main',
        thinking:'看完了，接着改', content:'改这里的第二段。',
        tool_calls:[{id:'s2', function:{name:'edit_file', arguments:'{}'}}]})},
  ];
  const boxOf = () => S1.gbox.querySelector('.livebox');   // 渲染之后才存在
  const trace = [];
  for (const c of seq) {
    applyChunk(c);
    renderLive();
    const box = boxOf();
    const ansEl = box && box.querySelector('.live-tail .bub');
    const ansVisible = !!(ansEl && ansEl.style.display !== 'none'
      && String(ansEl.innerHTML).trim());
    const tailText = String((ansEl && ansEl.innerHTML) || '');
    trace.push(`${c.k}:在飞正文=${ansVisible ? '**在**' : '无'}`);
    // **不变量**：在飞正文缓冲区空了，那个节点就必须是隐藏的、且不许再带着旧文本
    if (!LIVE.ans && ansVisible) {
      problems.push(`S: ${c.k} 之后尾部还显示着旧正文（缓冲区已空但节点还可见）`);
    }
    if (!LIVE.ans && /第二行|改这里的第二段/.test(tailText) && ansEl.style.display !== 'none') {
      problems.push(`S: ${c.k} 之后尾部留着上一次渲染过的正文（用户报的"卡住两行"）`);
    }
  }
  // 收尾：全部事件落完之后，尾部必须是干净的（内容都在完成区里）
  const box = boxOf();
  const ansEl = box && box.querySelector('.live-ans');
  const doneEl = box.querySelector('.live-done');
  const doneCards = doneEl ? doneEl.querySelectorAll('.tcard').length : 0;
  if (doneCards !== 2) problems.push(`S: 两张工具卡（实际 ${doneCards}）`);
  console.log(`   S ${trace.join(' | ')} 尾部正文=${ansEl && ansEl.style.display !== 'none' ? '仍可见' : '已隐'} 卡=${doneCards}`);
}

console.log('场景 T｜收尾后重绘：直播节点不许**冻在尾部**显示旧正文（用户报的"卡着了"）');
resetLive();
CONV = realConv();
clearOfficial(7);
const T1 = mountRound(DOM.content, 7, false, 'Main');
META.round_list = [{seq:7, active_view:'Main', events:3, steps_used:2}];
META.status = 'in_progress';
{
  // 跑一段：思考 → 过程发言（两行正文）→ 工具事件
  applyChunk({k:'think', s:'先想一下'});
  applyChunk({k:'ans', s:'两行正文。\n第二行。'});
  renderLive();
  applyChunk({k:'event', e:ev({id:'R7-E01', type:'tool_call', agent:'Main',
    thinking:'先想一下', content:'两行正文。\n第二行。',
    tool_calls:[{id:'t1', function:{name:'read_file', arguments:'{}'}}]})});
  renderLive();
  // 收尾：正式渲染接管（内容由事件重画），随后**再来一次重绘**——
  // 真实世界里的成因就是这一次重绘（切页/刷新/任何一次 drawConv）
  LIVE.closing = true;
  CONV.cache[7] = {seq:7, user_input:'干活', events:[
    {id:'R7-E01', type:'user', agent:'Main', time:'2026-09-13T17:00:00',
     role:'user', content:'干活'},
    {id:'R7-E02', type:'tool_call', agent:'Main', time:'2026-09-13T17:00:01',
     role:'assistant', thinking:'先想一下', content:'两行正文。\n第二行。',
     tool_calls:[{id:'t1', function:{name:'read_file', arguments:'{}'}}]},
    {id:'R7-E03', type:'tool_result', agent:'Main', time:'2026-09-13T17:00:02',
     role:'tool', tool_call_id:'t1', content:'读到了'},
  ]};
  CONV.seqs = [7];
  drawConv(DOM.content, false);          // 收尾那一次（closing）
  liveDetach();                          // 撤直播态
  // **收官以后再画一次**（用户切页、刷新、任何一次重绘都可能）
  drawConv(DOM.content, false);
  // 而且任何一次重绘/轮询之后都可能再调到 `renderLive`——没有作业时它必须**什么都不做**
  renderLive();
  // 兜底那一层也测：**人为塞一个残留直播节点**（模拟"某条路径把它挂了回去"），
  // 正式渲染必须自己把它清掉（不靠"谁记得调 liveDetach"）
  const fake = mkEl('div'); fake.className = 'livebox';
  const fakeAns = mkEl('div'); fakeAns.className = 'bub md';
  fakeAns._html = '残留的旧正文'; fakeAns.textContent = '残留的旧正文';
  fake.appendChild(fakeAns);
  T1.gbox.appendChild(fake);
  drawConv(DOM.content, false);
  const stray = DOM.content.querySelectorAll('.livebox').length;
  const provs = DOM.content.querySelectorAll('.crow.live-prov').length;
  const stale = [];
  DOM.content.querySelectorAll('.live-tail,.live-ans,.live-think').forEach(el=>{
    if(el.style.display !== 'none' && String(el.innerHTML).trim()) stale.push(String(el.innerHTML).slice(0, 20));
  });
  if (stray || provs) problems.push(`T: 收尾后还留着直播节点/临时块（${stray}/${provs}）`);
  if (stale.length) problems.push(`T: 尾部还显示着旧正文（${stale.join(' / ')}）`);
  const body = domAllText(DOM.content);
  if (!/两行正文/.test(body)) problems.push('T: 正式渲染没把过程发言画出来（事件里是有的）');
  console.log(`   T 残留直播节点=${stray} 临时块=${provs} 冻住的旧正文=${stale.length} 正式正文在=${/两行正文/.test(body)}`);
}

console.log('场景 V｜性能：流式重绘的 DOM 查询次数**不许随条目数线性涨**（"流畅"的硬指标）');
resetLive();
const V1 = mountRound(DOM.content, 7, false, 'Main');
META.round_list = [{seq:7, active_view:'Main', events:200, steps_used:200}];
META.status = 'in_progress';
{
  // 先落 **60 个工具事件**（长轮次的现实规模：实测 R7 一轮 43 步、多轮上百事件）
  for (let i = 1; i <= 60; i++) {
    applyChunk({k:'event', e:ev({id:'R7-E' + String(i).padStart(2, '0'),
      type:'tool_call', agent:'Main', thinking:'第 ' + i + ' 步的思考',
      tool_calls:[{id:'v' + i, function:{name:'read_file', arguments:'{}'}}]})});
    applyChunk({k:'event', e:ev({id:'R7-R' + i, type:'tool_result', agent:'Main',
      role:'tool', tool_call_id:'v' + i, content:'结果' + i})});
  }
  renderLive();
  // 然后模拟"一步在流式输出"：200 个正文分片，期间**事件集合完全没变**
  domqReset();
  applyChunk({k:'think', s:'正在想'});
  for (let i = 0; i < 200; i++) {
    applyChunk({k:'ans', s:'字'});
    renderLive();
  }
  const perRender = DOMQ.n / 200;
  const perNodes = DOMQ.nodes / 200;
  // 只更新尾部区块时，每次重绘不该去全量扫那 120 个条目
  if (perRender > 12) {
    problems.push(`V: 每次流式重绘查了 ${perRender.toFixed(1)} 次 DOM（条目 ${(LIVE.events.Main || []).length * 2} 个）→ 随条目数线性涨，长轮次会卡`);
  }
  if (perNodes > 60) {
    problems.push(`V: 每次流式重绘扫到 ${perNodes.toFixed(0)} 个节点（应只碰尾部那几个）`);
  }
  console.log(`   V 条目=${(LIVE.events.Main || []).length}  每次重绘 DOM 查询=${perRender.toFixed(1)} 次 / 节点=${perNodes.toFixed(0)} 个`);

  // ② md() 的成本：流式正文每个分片都重渲染一次整段 → 文本越长越贵（O(n²) 的形状）。
  //    这里量"一个分片平均要喂给 md 多少字"，好判断要不要做合并渲染。
  const realMd = md;
  let mdChars = 0, mdCalls = 0;
  md = (t) => { mdChars += String(t || '').length; mdCalls++; return realMd(t); };
  domqReset();
  mdChars = 0; mdCalls = 0;
  for (let i = 0; i < 100; i++) { applyChunk({k:'ans', s:'字'}); renderLive(); }
  const charsPerDelta = mdChars / 100;
  md = realMd;
  console.log(`   V md() 每分片均价 ${charsPerDelta.toFixed(0)} 字（分片数 ${mdCalls}；`
    + `文本约 ${(LIVE.ans || '').length} 字）`);
}

console.log('场景 W｜六个页签都要画得出来、且不许出现 undefined/NaN（崩溃=黑屏）');
resetLive();
CONV = realConv();
clearOfficial(7);
META.round_list = [{seq:7, active_view:'A', events:3, steps_used:2, org_state:'raw'}];
META.status = 'done';
META.todo = {items:[{text:'一件事', done:false}]};
META.todo_log = [{seq:7, time:'2026-09-13T17:00:00', action:'add_item', text:'记一笔'}];
{
  const bad = [];
  for (const [k, name] of [['conv','对话'],['timeline','时间线'],['ledger','账本'],
                           ['todo','计划'],['project','项目'],['usage','用量']]) {
    try {
      setTab(k);
      const html = String(DOM.content.innerHTML || '');
      if (/undefined|NaN|\[object Object\]/.test(html)) {
        const where = html.replace(/\s+/g, ' ')
          .match(/.{0,60}(undefined|NaN|\[object Object\]).{0,40}/);
        bad.push(`${name}(${k})：渲染里出现了 undefined/NaN → …${where ? where[0] : ''}…`);
      }
      if (!html.trim()) bad.push(`${name}(${k})：什么都没画出来`);
    } catch (e) {
      bad.push(`${name}(${k})：抛异常 ${e && e.message}`);
    }
  }
  bad.forEach(b => problems.push('W: ' + b));
  setTab('conv');
  console.log(`   W 六页签=${bad.length === 0 ? '全部正常' : bad.length + ' 个有问题'}`);
}

console.log('场景 W2｜数据**字段稀疏**时也不许露出 undefined（后端改字段/老数据都不该让用户看见破绽）');
resetLive();
CONV = realConv();
clearOfficial(7);
// 只给必需字段，可选字段全缺：六页签都得能画、且不许出现 undefined/NaN
META = { id:'s1', status:'done', round_list:[{seq:7, active_view:'A', events:1}],
         registry:[{id:'Main', name:'主agent'},{id:'A', name:'域甲'}],
         todo:{items:[{}]}, todo_log:[{}], project:{}, usage:{} };
{
  const bad = [];
  for (const [k, name] of [['conv','对话'],['timeline','时间线'],['ledger','账本'],
                           ['todo','计划'],['project','项目'],['usage','用量']]) {
    try {
      setTab(k);
      const html = String(DOM.content.innerHTML || '');
      if (/undefined|NaN|\[object Object\]/.test(html)) {
        const where = html.replace(/\s+/g, ' ')
          .match(/.{0,50}(undefined|NaN|\[object Object\]).{0,30}/);
        bad.push(`${name}(${k}) → …${where ? where[0] : ''}…`);
      }
    } catch (e) {
      bad.push(`${name}(${k})：抛异常 ${e && e.message}`);
    }
  }
  setTab('conv');
  bad.forEach(b => problems.push('W2: 字段稀疏时露破绽：' + b));
  console.log(`   W2 稀疏数据=${bad.length === 0 ? '六页签都干净' : bad.length + ' 处露破绽'}`);
}

console.log('场景 X｜刷新后接着看（catch-up）：不再从 0 重放，且内容与直播一致');
resetLive();
CONV = realConv();
clearOfficial(7);
const X1 = mountRound(DOM.content, 7, false, 'Main');
META.round_list = [{seq:7, active_view:'Main', events:3, steps_used:1}];
META.status = 'in_progress';
{
  // 服务端这一轮已经推过的分片（刷新前推的）：思考增量 + 工具事件 + 结果
  const chunks = [
    {k:'status', s:'等待模型响应…', ag:'Main'},
    {k:'think', s:'先读文件', ag:'Main'},
    {k:'event', e:ev({id:'R7-E01', type:'tool_call', agent:'Main',
      thinking:'先读文件',
      tool_calls:[{id:'x1', function:{name:'read_file', arguments:'{}'}}]}), ag:'Main'},
    {k:'event', e:ev({id:'R7-E02', type:'tool_result', agent:'Main', role:'tool',
      tool_call_id:'x1', content:'读到了'}), ag:'Main'},
    {k:'status', s:'正在执行 read_file…', ag:'Main'},
  ];
  // 刷新后的重挂：一次性 catch-up（`liveAttach` 的那条路）
  LIVE.job = 'j1'; LIVE.cur = 's1'; LIVE.next = 0; LIVE.sse = false;
  let dirty = false;
  for (const c of chunks) dirty = applyChunk(c) || dirty;
  LIVE.next = chunks.length;
  if (dirty) renderLive();
  const box = X1.gbox.querySelector('.livebox');
  const done = box && box.querySelector('.live-done');
  const cards = done ? done.querySelectorAll('.tcard').length : 0;
  const resTexts = done ? [...done.querySelectorAll('.t-result')]
    .map(el => String(el.textContent || '')) : [];
  if (cards !== 1) problems.push(`X: catch-up 后应有 1 张工具卡，实际 ${cards}`);
  if (!resTexts.some(t => t === '读到了')) problems.push('X: catch-up 后工具结果没回填');
  if (!/先读文件/.test(String((done && done.innerHTML) || '')))
    problems.push('X: catch-up 后思考丢了');
  // 再来一次同样的 catch-up（模拟重连重复拉）：不许出现两份
  for (const c of chunks) applyChunk(c);
  renderLive();
  const cards2 = done ? done.querySelectorAll('.tcard').length : 0;
  if (cards2 !== cards) problems.push(`X: 重连重放后又多画了（${cards}→${cards2}）`);
  console.log(`   X 卡=${cards} 结果回填=${resTexts.some(t => t === '读到了')} 重放后仍=${cards2}`);
}

console.log('场景 Y｜作业没了（服务重启/清理）：要收尾，不许卡在"运行中"');
resetLive();
CONV = realConv();
clearOfficial(7);
mountRound(DOM.content, 7, false, 'Main');
META.round_list = [{seq:7, active_view:'Main', events:1, steps_used:1}];
{
  LIVE.job = 'dead'; LIVE.cur = 's1'; LIVE.sse = true;
  const btn = document.getElementById('send');
  renderLive();
  const titleRun = document.title;
  // `pollLive` 的 SSE 分支：404 → `{gone:true}` → sseFinish()
  // 这里直接驱动它关心的那一步（不引异步 fetch）
  if (!(typeof jobStatus === 'function')) problems.push('Y: 没有 jobStatus');
  if (typeof sseFinish !== 'function') problems.push('Y: 没有 sseFinish');
  // `sseFinish` 是 **async**（要先让正式渲染落位再撤直播区）：同步只能断言
  // "收尾已经开始"（`finishing` 置位 = 幂等闸生效）；它返回 promise，吞掉避免
  // 未处理拒绝把整个核对脚本带崩
  const p = sseFinish();
  if (p && typeof p.then === 'function') p.catch(() => {});
  if (!LIVE.finishing) problems.push('Y: 作业没了却没启动收尾（界面会一直"运行中"）');
  console.log(`   Y 收尾已启动=${!!LIVE.finishing} 发送键禁用=${!!(btn && btn.disabled)}`);
}

console.log('场景 V2｜流式合并渲染：一批文字分片只画一次（md 的成本是 O(n²)，必须合并）');
resetLive();
const V2r = mountRound(DOM.content, 7, false, 'Main');
META.round_list = [{seq:7, active_view:'Main', events:1, steps_used:1}];
META.status = 'in_progress';
{
  const realRender = renderLive;
  let renders = 0;
  renderLive = (...a) => { renders++; return realRender(...a); };
  let dirty = 0;
  // 首片带着 `ag`（执行方第一次报上来）→ 那是**结构性**的一次，按真实流程先走掉
  if (applyChunk({k:'ans', s:'首', ag:'Main'}) >= 2) renderLive();
  renders = 0;                        // 从这里开始量"纯文字分片"的成本
  for (let i = 0; i < 500; i++) {
    dirty = Math.max(dirty, applyChunk({k:'ans', s:'字', ag:'Main'}) || 0);
  }
  if (dirty >= 2) renderLive(); else if (dirty) scheduleLiveRender();
  const immediately = renders;
  // 把到点的定时器跑掉（模拟 100ms 后那一帧）
  const once = _timers.filter(t => t.once);
  once.forEach(t => { clearInterval(t.id); t.fn(); });
  const afterDrain = renders;
  renderLive = realRender;
  if (immediately !== 0)
    problems.push(`V2: 500 个文字分片里有 ${immediately} 次是立刻渲染的（没合并 → 长回答会卡）`);
  if (afterDrain !== 1)
    problems.push(`V2: 合并后应只渲染 1 次，实际 ${afterDrain}`);
  // 结构性变化（事件）必须**立刻**渲染，不许等
  renders = 0;
  let lv = applyChunk({k:'event', e:ev({id:'R7-E01', type:'tool_call', agent:'Main',
    tool_calls:[{id:'v2c', function:{name:'read_file', arguments:'{}'}}]})});
  if (lv < 2) problems.push('V2: 事件分片没被标成"结构性变化"（会被合并延迟）');
  console.log(`   V2 500 分片立刻渲染=${immediately} 次，合并后共=${afterDrain} 次；`
    + `事件级别=${lv}`);
}

console.log('场景 Z｜事件**迟到/丢失**时，在飞正文也不许跨步堆积（用户报的"留在最后、越来越多"）');
resetLive();
const Z1 = mountRound(DOM.content, 7, false, 'Main');
META.round_list = [{seq:7, active_view:'Main', events:3, steps_used:3}];
META.status = 'in_progress';
{
  // 真实形状（取自实测：一步 = 大段思考 + 小段过程发言；思考 1k~12k 字、正文 0~180 字）
  const narr = ['先看一眼索引', '索引被清空了，查是谁动的', '跑一下编译闸门'];
  const box = () => Z1.gbox.querySelector('.livebox');
  const tailText = () => {
    const tl = box() && box().querySelector('.live-tail');
    return String((tl && tl.innerHTML) || '');
  };
  const seen = [];
  for (let s = 0; s < 3; s++) {
    // 每步：大段思考增量 → 小段正文增量，**都带步号**
    for (let i = 0; i < 20; i++) {
      applyChunk({k:'think', s:'想' + i, ag:'Main', st:s * 2});
      renderLive();
    }
    applyChunk({k:'ans', s:narr[s], ag:'Main', st:s * 2});
    renderLive();
    seen.push(`第${s}步:${/先看一眼|索引被清空|编译闸门/.test(tailText()) ? '在飞有它' : '无'}`);
    // 下一步开始（步号 +1）——**故意不推事件**，只推进步号
    applyChunk({k:'think', s:'', ag:'Main', st:s * 2 + 1});
    renderLive();
    const leftover = tailText();
    const stale = ['先看一眼索引', '索引被清空了，查是谁动的', '跑一下编译闸门']
      .some(t => t !== narr[s] && leftover.includes(t));
    if (stale) {
      problems.push(`Z: 第${s}步之后的尾部还留着**更早那一步**的正文（越堆越多）`);
    }
    if (!LIVE.think && !LIVE.ans && leftover.trim() !== '') {
      problems.push(`Z: 缓冲区已空但尾部还有内容（第${s}步）`);
    }
  }
  // 最后：缓冲区空 → 尾部必须为空
  applyChunk({k:'status', s:'等待模型响应…', ag:'Main', st:6});
  renderLive();
  if (tailText().trim() !== '') problems.push('Z: 收口之后尾部仍有内容');
  console.log(`   Z ${seen.join(' | ')} 最终尾部=${tailText().trim() === '' ? '空' : '非空'}`);
}

console.log('场景 AA｜多 agent 多步：块的**归属与顺序**必须与事件时间序一致、且不许重复');
resetLive();
CONV = realConv();
clearOfficial(7);
const AA1 = mountRound(DOM.content, 7, false, 'Main');
const AA2 = mountRound(DOM.content, 7, false, 'A');
META.round_list = [{seq:7, active_view:'Main', events:6, steps_used:3}];
META.status = 'in_progress';
{
  // 真实形状：主 agent 干一步 → 转交 A → A 干一步 → 回主 agent 再干一步
  const evs = [
    {id:'R7-E01', type:'tool_call', agent:'Main', thinking:'主看活归谁',
     content:'先看看这活归谁',
     tool_calls:[{id:'a1', function:{name:'list_files', arguments:'{}'}}]},
    {id:'R7-E02', type:'tool_result', agent:'Main', role:'tool', tool_call_id:'a1',
     content:'列出来了'},
    {id:'R7-E03', type:'tool_call', agent:'A', thinking:'A 动手',
     content:'这活归我，我来',
     tool_calls:[{id:'a2', function:{name:'read_file', arguments:'{}'}}]},
    {id:'R7-E04', type:'tool_result', agent:'A', role:'tool', tool_call_id:'a2',
     content:'读到了'},
    {id:'R7-E05', type:'tool_call', agent:'Main', thinking:'主收尾',
     content:'我来收尾',
     tool_calls:[{id:'a3', function:{name:'run_command', arguments:'{}'}}]},
    {id:'R7-E06', type:'tool_result', agent:'Main', role:'tool', tool_call_id:'a3',
     content:'跑完了'},
  ];
  for (const e of evs) { applyChunk({k:'event', e, ag:e.agent, st:evs.indexOf(e) + 1}); renderLive(); }
  // 直播内容在 `.live-done`/`.live-tail` 子节点里（box 自己的 innerHTML 是骨架串，
  // 读它等于没读——这个坑踩过一次）
  const boxOf = id => {
    const row = (id === 'Main' ? AA1 : AA2).gbox.querySelector('.livebox');
    return row ? String((row.querySelector('.live-done') || {}).innerHTML || '')
               + String((row.querySelector('.live-tail') || {}).innerHTML || '') : '';
  };
  const main = boxOf('Main'), a = boxOf('A');
  const all = main + '\u0000' + a;
  const dup = ['先看看这活归谁', '这活归我，我来', '我来收尾']
    .filter(t => (all.split(t).length - 1) !== 1);
  if (dup.length) problems.push(`AA: 同一段内容画了不止一次（${dup.join(' / ')}）`);
  if (/这活归我，我来/.test(main))
    problems.push('AA: A 那一步的内容串进了主 agent 的块');
  if (!main && !a) problems.push('AA: 直播区根本没挂上（两块都空）');
  if (a && !/这活归我，我来/.test(a)) problems.push('AA: A 的内容没进 A 的块');
  // A 的块是**第二块**（事件序里 A 在 Main 之后）——顺序不许反
  const rows = [...DOM.content.querySelectorAll('.crow.agent[data-seq="7"]')];
  const order = rows.map(r => (r.querySelector('.gbox') || {}).dataset?.agent || '?');
  if (order.length && order.join('>') !== 'Main>A') {
    problems.push(`AA: 块的顺序与事件序不一致（实际 ${order.join('>')}，应为 Main>A）`);
  }
  console.log(`   AA 块序=${order.join('>')} 主块=${main ? '有' : '空'} A块=${a ? '有' : '空'}`
    + ` 重复=${dup.length === 0 ? '无' : dup.join('/')}`);
  if (process.env.WEBUI_RENDER_DUMP) {
    const rows = [...DOM.content.querySelectorAll('.crow.agent[data-seq="7"]')];
    console.log('   [dbg AA]', JSON.stringify(rows.map(r => ({
      agent: (r.querySelector('.gbox') || {}).dataset?.agent,
      boxes: r.querySelectorAll('.livebox').length,
      html: String((r.querySelector('.gbox') || {}).innerHTML || '').slice(0, 160),
    })), null, 1));
  }
}

console.log('场景 AB｜刷新后官方已画过一段：直播**不许把同一段再画一遍**（用户报的"方块重复、续到最下面"）');
resetLive();
CONV = realConv();
clearOfficial(7);
const AB1 = mountRound(DOM.content, 7, false, 'Main');
const AB2 = mountRound(DOM.content, 7, false, 'A');   // 空的另一块：直播块会落到它这儿
META.round_list = [{seq:7, active_view:'Main', events:4, steps_used:2}];
META.status = 'in_progress';
{
  // ① 刷新时那一轮已经落到盘上（部分事件）→ 官方渲染把它画了一遍
  const official = [
    {id:'R7-E01', type:'tool_call', agent:'Main', time:'2026-09-13T17:00:00',
     role:'assistant', thinking:'（官方那次的思考）', content:'先看看这活归谁',
     tool_calls:[{id:'b1', function:{name:'list_files', arguments:'{}'}}]},
  ];
  CONV.cache[7] = {seq:7, user_input:'干活', events:official};
  CONV.seqs = [7];
  drawConv(DOM.content, false);
  // ② 直播从头重放（catch-up 从 0 开始）：同一批分片再喂一遍
  LIVE.job = 'j1'; LIVE.cur = 's1';
  applyChunk({k:'event', e:{...official[0]}, ag:'Main', st:1});
  applyChunk({k:'event', e:{id:'R7-E02', type:'final_answer', agent:'Main',
    time:'2026-09-13T17:00:05', role:'assistant', content:'干完了'}, ag:'Main', st:2});
  renderLive();
  // ③ 整轮范围内的重复检查：同一段内容只许出现一次（跨块、跨官方/直播都要算）
  const body = String(DOM.content.innerHTML || '');
  const dup = ['先看看这活归谁', '干完了'].filter(t => (body.split(t).length - 1) > 1);
  if (dup.length) problems.push(`AB: 同一段内容在整轮里画了不止一次（${dup.join(' / ')}）`);
  // **按块**数一遍：同一段内容只许出现在一个块里（跨块重复才是用户看到的"方块重复"）
  const perRow = [...DOM.content.querySelectorAll('.crow.agent[data-seq="7"]')]
    .map(r => domAllText(r.querySelector('.gbox')));
  const crossRowDup = perRow.filter(h => /先看看这活归谁/.test(h)).length;
  if (crossRowDup > 1) {
    problems.push(`AB: 同一段内容出现在 ${crossRowDup} 个块里（跨块重复）`);
  }
  const boxes = DOM.content.querySelectorAll('.livebox').length;
  const provs = DOM.content.querySelectorAll('.crow.live-prov').length;
  console.log(`   AB 重复=${dup.length === 0 ? '无' : dup.join('/')} 直播块=${boxes} 临时块=${provs}`);
}

console.log('场景 AC｜元数据还没到时渲染：任何入口都不许抛错（用户报"页面一直加载中"的真因）');
resetLive();
META = null;                       // 会话还没选中 / 元数据还没到
{
  const bad = [];
  // 页面函数在同一个词法作用域里（不在 globalThis 上）→ 直接按名字调，`typeof` 兜底
  const probe = (label, thunk) => {
    try {
      const r = thunk();
      if (r && typeof r.then === 'function') r.catch(() => {});
    } catch (e) {
      bad.push(`${label} 抛错：${e && e.message}`);
    }
  };
  const entries = [
    ['renderHeader', () => renderHeader()],
    ['renderTab', () => renderTab()],
    ['renderConv', () => renderConv(DOM.content)],
    ['renderTimeline', () => renderTimeline(DOM.content)],
    ['renderLedger', () => renderLedger(DOM.content)],
    ['renderTodo', () => renderTodo(DOM.content)],
    ['renderProject', () => renderProject(DOM.content)],
    ['renderUsage', () => renderUsage(DOM.content)],
    ['renderCtxbar', () => renderCtxbar()],
    ['loadTree', () => loadTree()],
    ['setTab:conv', () => setTab('conv')],
  ];
  for (const [label, thunk] of entries) probe(label, thunk);
  bad.forEach(b => problems.push('AC: 元数据缺失时 ' + b));
  console.log(`   AC 入口 ${entries.length} 个 → ${bad.length === 0 ? '全都不抛错' : bad.length + ' 个抛错'}`);
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
        else if (cl.includes('bub')) marks.push('bub');
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

console.log('场景 AR｜分裂状态落到轮头：待生效 / 已回入水位都要看得见');
// 2026-09-15：维护状态原先只写 history（并发写时会被覆盖），现在落到轮的
// `split_state` 并由 serve 透出——轮头要有 chip，否则用户只能看到"分裂没结束"。
resetLive();
CONV = {session:'s1', seqs:[7], cache:{7:{seq:7, blocks:[], events:[
  {id:'R7-E01', type:'user', role:'user', content:'干活', time:''},
  {id:'R7-E02', type:'final_answer', role:'assistant', agent:'Main', content:'完成', time:''}]}}};
META.round_list = [{seq:7, active_view:'Main', events:2, steps_used:1,
                    org_state:'done', split_state:'deferred'}];
drawConv(DOM.content, false);
{
  const html = String(DOM.content.innerHTML || '');
  if (!/分裂待生效/.test(html)) problems.push('AR: split_state=deferred 没显示"分裂待生效"');
  META.round_list = [{seq:7, active_view:'Main', events:2, steps_used:1,
                      org_state:'done', split_state:'stale'}];
  drawConv(DOM.content, false);
  const html2 = String(DOM.content.innerHTML || '');
  if (!/已回入水位/.test(html2)) problems.push('AR: split_state=stale 没显示"已回入水位"');
  META.round_list = [{seq:7, active_view:'Main', events:2, steps_used:1,
                      org_state:'done', split_state:'done'}];
  drawConv(DOM.content, false);
  const html3 = String(DOM.content.innerHTML || '');
  if (/分裂/.test(html3)) problems.push('AR: split_state=done 不该再显示分裂 chip');
  console.log(`   AR deferred=${/分裂待生效/.test(html)} stale=${/已回入水位/.test(html2)} done静默=${!/分裂/.test(html3)}`);
}

console.log('场景 AM｜续轮：直播块必须挂在"最后一段"，不许跑到用户消息上面');
// 用户审计（2026-09-14）："我消息在最下面，消息块，在上面加载"。续轮（用户消息落进
// 同一个开放轮）时同一个 agent 有多个块被用户消息切开；旧 `liveGbox` 取**第一个**匹配行
// → 直播内容挂到了那条用户消息**上面**。修法：取最后一个匹配行，且其后还有本轮用户行时
// 改用临时块（追加在末尾 = 用户消息之下）。
resetLive();
LIVE.seq = 7;
META.round_list = [{seq:7, active_view:'Main', events:3, steps_used:2}];
META.status = 'in_progress';
CONV = {session:'s1', seqs:[], cache:{}};
// 造出"被用户消息切开的同一轮"：agent 段 → user → （直播内容应落在这里之后）
const AM1 = mountRound(DOM.content, 7, false, 'Main');
const u = mkEl('div'); u.className = 'crow user'; DOM.content.appendChild(u);
applyChunk({k:'think', s:'续轮之后的思考'});
renderLive();
{
  const box = DOM.content.querySelector('.livebox');
  const host = box && box.closest ? box.closest('.crow') : null;
  const hostIsFirst = !!(host && host === AM1.row);
  const order = [...DOM.content.children].map(el => String(el.className).slice(0, 20));
  const boxIdx = order.findIndex(x => x.startsWith('crow agent live-prov'));
  const userIdx = order.findIndex(x => x === 'crow user');
  if (hostIsFirst) problems.push('AM: 直播块挂进了用户消息**上面**那一段（"消息块在上面加载"）');
  if (boxIdx >= 0 && userIdx >= 0 && boxIdx < userIdx)
    problems.push('AM: 直播块排在用户行之前（时间顺序反了）');
  console.log(`   AM 宿主=第一段?${hostIsFirst} 顺序=${order.join(' | ')}`);
}

console.log('场景 AL｜流式追问：在飞思考块的**元素身份**跨帧保持（否则点不开）');
// 用户审计（2026-09-14）："正在思考的消息块点不开"。根因：尾巴每次分片都 `innerHTML=`
// 整体重建，`onclick=toggleThink(this)` 的那个元素在 mousedown 与 click 之间被替换掉。
// 修法：条目种类/个数不变时原地更新内容，元素身份保持。
resetLive();
mountRound(DOM.content, 7, false, 'Main');
META.round_list = [{seq:7, active_view:'Main', events:0, steps_used:1}];
META.status = 'in_progress';
applyChunk({k:'think', s:'第一段'});
renderLive();
const AL1 = DOM.content.querySelector('.livebox .live-tail .think-box');
applyChunk({k:'think', s:'，第二段'});
renderLive();
{
  const AL2 = DOM.content.querySelector('.livebox .live-tail .think-box');
  if (!AL1 || !AL2) problems.push('AL: 前置条件没造出来（应有在飞思考块）');
  else if (AL1 !== AL2) problems.push('AL: 追问后思考块换了新元素（点击会落空 → 点不开）');
  const body = AL2.querySelector('.think-body');
  if (!body || String(body.textContent || '') !== '第一段，第二段')
    problems.push('AL: 原地更新后文本不对（应累积到"第一段，第二段"）');
  console.log(`   AL 同一元素=${AL1 === AL2} 文本=${body ? String(body.textContent).slice(0, 12) : '-'}`);
}

console.log('场景 AK｜状态到达不许清在飞内容（用户报"思考完/输出完会消失一下再重新 MD 渲染"）');
// 根因：状态是"工具名一分片到达"就发的（core.py `_stream_call`），而承载这段内容的**事件**
// 要等参数全部流完才落（大文件参数流几秒）——在 status 上清缓冲，内容就先消失。
// 正确的清点是事件到达（与重新渲染同帧）。本场景钉住两件事：
// ① status 到达后，在飞正文/思考**仍在**；② 事件到达后，才由事件接管（缓冲清空、内容成条目）。
resetLive();
mountRound(DOM.content, 7, false, 'Main');
META.round_list = [{seq:7, active_view:'Main', events:0, steps_used:1}];
META.status = 'in_progress';
applyChunk({k:'ans', s:'**最终回答**正在流式输出的一段'});
renderLive();
{
  const tail = DOM.content.querySelector('.livebox .live-tail');
  if (!tail || !/最终回答/.test(String(tail.innerHTML || '')))
    problems.push('AK: 前置条件没造出来（在飞正文应在尾巴里）');
}
applyChunk({k:'status', s:'正在写入文件…'});     // 工具名刚到（事件还没落）
renderLive();
{
  const tail = DOM.content.querySelector('.livebox .live-tail');
  const keep = tail && /最终回答/.test(String(tail.innerHTML || ''));
  if (!keep) problems.push('AK: 状态一到就把在飞内容清了（用户看到"输出完消失一下"）');
  console.log(`   AK 状态后仍在飞=${!!keep}`);
}
applyChunk({k:'event', st:1, ag:'Main', e:ev({id:'R7-E61', type:'final_answer', agent:'Main',
  role:'assistant', content:'**最终回答**正在流式输出的一段'})});
renderLive();
{
  const tail = DOM.content.querySelector('.livebox .live-tail');
  const cleared = !tail || !String(tail.innerHTML || '').trim();
  const done = DOM.content.querySelector('.livebox .live-done');
  const landed = done && /strong/.test(String(done.innerHTML || ''));
  if (!cleared) problems.push('AK: 事件落地后在飞缓冲没让位（会与条目重复）');
  if (!landed) problems.push('AK: 事件内容没有落成条目（正文没接住）');
  console.log(`   AK 事件后缓冲清空=${cleared} 条目接住=${!!landed}`);
}

console.log('场景 AI｜工具动作状态先到：块里要立刻有一条临时行，事件落地后被顶掉');
// 用户审计（2026-09-14）："下面状态老是先于消息块，例如'修改文件中…/写入中…'，块要等几秒
// 才出现"。服务端在**工具名一分片到达**就报这个状态（core.py `_stream_call`），而工具卡
// 要等**参数全部流完**才落成事件——中间几秒块内空着。修法：状态到达时在直播块里画一条
// 浅色临时行；工具卡（事件）落地即被顶掉。
resetLive();
mountRound(DOM.content, 7, false, 'Main');
META.round_list = [{seq:7, active_view:'Main', events:0, steps_used:1}];
META.status = 'in_progress';
applyChunk({k:'status', s:'正在写入文件…', ag:'Main'});
renderLive();
{
  const tail = DOM.content.querySelector('.livebox .live-tail');
  const has = tail && /正在写入文件/.test(String(tail.innerHTML || ''));
  if (!has) problems.push('AI: 工具动作状态到了，块里却没有临时行（用户看到"状态先于块"）');
  console.log(`   AI 状态到达后临时行=${has}`);
}
applyChunk({k:'event', st:1, ag:'Main', e:ev({id:'R7-E41', type:'tool_call', agent:'Main',
  role:'assistant', content:'',
  tool_calls:[{id:'w1', function:{name:'write_file', arguments:'{}'}}]})});
renderLive();
{
  const tail = DOM.content.querySelector('.livebox .live-tail');
  const still = tail && /正在写入文件/.test(String(tail.innerHTML || ''));
  if (still) problems.push('AI: 工具卡已落地，临时状态行还赖着（会与卡片重复）');
  console.log(`   AI 事件落地后临时行仍在=${still}`);
}

console.log('场景 AJ｜过程发言（带工具调用那步的正文）也要走 MD 渲染');
// 用户审计（2026-09-14）："正文有时候会不 MD 渲染"。`eventItems` 里只有
// `type==='final_answer'` 才 `md:true`；过程发言走纯文本，于是长任务里大段的
// 说明/清单把 `**加粗**`、`- 列表` 原样显示。修法：interim 也过 `md()`，弱化仍由样式管。
resetLive();
mountRound(DOM.content, 7, false, 'Main');
META.round_list = [{seq:7, active_view:'Main', events:0, steps_used:1}];
applyChunk({k:'event', st:1, ag:'Main', e:ev({id:'R7-E51', type:'tool_call', agent:'Main',
  role:'assistant', content:'**先看现状**，再动手：\n- 读文件\n- 改实现',
  tool_calls:[{id:'r1', function:{name:'read_file', arguments:'{}'}}]})});
renderLive();
{
  const done = DOM.content.querySelector('.livebox .live-done');
  const html = String((done && done.innerHTML) || '');
  const ok = /<strong>先看现状<\/strong>/.test(html);
  if (!ok) problems.push('AJ: 过程发言没过 MD 渲染（**加粗** 会原样显示）');
  console.log(`   AJ 过程发言含 <strong>=${ok}`);
}

console.log('场景 AH｜一轮里多名 agent：临时块的上下顺序必须与创建（=时间）序一致');
// 用户审计（2026-09-14）："分裂后运行中，下面的消息块老是跑到上面 Main 中"。
// 根因：drawConv 保活后 renderLive 按 `who`（**当前 actor 排第一**）逐个挂回，
// 接手方的块被 append 在主 agent 块之后又轮到主 agent 追加 → 顺序颠倒。
resetLive();
LIVE.seq = 7;
META.round_list = [{seq:7, active_view:'Main', events:1, steps_used:1}];
META.status = 'in_progress';
META.registry = [{id:'Main',name:'主agent'},{id:'B',name:'web 线'}];
REG = {Main:{id:'Main',name:'主agent'}, B:{id:'B',name:'web 线'}, 'web 线':{id:'B',name:'web 线'}};
// 主 agent 先干（创建 Main 的临时块）
applyChunk({k:'think', s:'主 agent 的思考', ag:'Main'});
renderLive();
// 换手给 B（创建 B 的临时块）
applyChunk({k:'event', st:2, ag:'B', e:ev({id:'R7-E01', type:'tool_call', agent:'B',
  role:'assistant', content:'', tool_calls:[{id:'b1', function:{name:'read_file', arguments:'{}'}}]})});
renderLive();
{
  const provs = [...DOM.content.querySelectorAll('.crow.live-prov')];
  const order = provs.map(p => ((p.querySelector('.gbox') || {}).dataset || {}).agent);
  if (order.length !== 2) problems.push(`AH: 前置条件不对（应有 2 个临时块，实际 ${order.length}）`);
  else if (order[0] !== 'Main' || order[1] !== 'B')
    problems.push(`AH: 临时块顺序应为 Main 在上、B 在下，实际 ${order.join(' / ')}`);
  console.log(`   AH 顺序=${order.join(' → ')}`);
}
// 整段重绘（保活摘挂）之后顺序仍须一致——这才是用户实际遇到的场景
// 夹具：CONV 清空（否则 drawConv 会从 CONV 画出正式行、把临时块合法地并进去）
{
  // 夹具：CONV 里放**上一轮**（不空，避免 drawConv 走"还没有对话"提前返回把 DOM 清空；
  // 也不放本轮 seq 7 的事件，避免它合法地把临时块并进正式行）
  CONV = {session:'s1', seqs:[6], cache:{6:{seq:6, blocks:[], events:[
    {id:'R6-E01', type:'user', role:'user', content:'上一轮', time:''}]}}};
  drawConv(DOM.content, false);
  const order = [...DOM.content.querySelectorAll('.crow.live-prov')]
    .map(p => ((p.querySelector('.gbox') || {}).dataset || {}).agent);
  if (order.length !== 2 || order[0] !== 'Main' || order[1] !== 'B')
    problems.push(`AH: 重绘后顺序变了（${order.join(' / ')}）——就是"下面的块跑到上面"`);
  console.log(`   AH 重绘后=${order.join(' → ')}`);
}

console.log('场景 AG｜换步那一帧缓冲为空：只要这轮还没有正式块兜底，直播块不许消失');
// 用户审计（2026-09-14）："消失"。真浏览器实测：首分钟里直播块被 `display:none` 藏掉
// 850ms / 2540ms 各一次——完成区与在飞缓冲在换步那一帧同时为空（flushLiveStep 清了
// 缓冲、事件还没落成条目），而"两边都空就整块隐藏"没有区分"还有没有正式块兜底"。
resetLive();
LIVE.seq = 7;                                  // 轮号已知；**不挂正式块**（只有临时块）
META.round_list = [{seq:7, active_view:'Main', events:1, steps_used:1}];
META.status = 'in_progress';
applyChunk({k:'think', s:'第一段思考'});
renderLive();
{
  const box = DOM.content.querySelector('.livebox');
  if (!box) problems.push('AG: 前置条件没造出来（应有直播节点）');
}
applyChunk({k:'status', s:'等待模型响应…'});   // 换步：缓冲清空、事件未落 → 两区都空
renderLive();
{
  const box = DOM.content.querySelector('.livebox');
  const hidden = !box || box.style.display === 'none';
  if (hidden) problems.push('AG: 缓冲空一帧就把直播块藏了（这轮还没有正式块兜底 → 整轮从页面消失）');
  console.log(`   AG 唯一表示时 隐藏=${hidden}`);
}
// 对照：正式块里**已有别人的内容**时，空的直播块允许收起（那才是"纯重复"）
{
  const m = mountRound(DOM.content, 7, false, 'Main');
  // 夹具注意：迷你 DOM 的元素内容要靠**标签间文本**才有 textContent（空标签算空）
  m.gbox.innerHTML = '<div class="tcard" data-call="x9">正式块已有的一条</div>';
  applyChunk({k:'think', s:''});                                   // 保持两区都空
  LIVE.think = ''; LIVE.ans = '';
  renderLive();
  const box = DOM.content.querySelector('.livebox');
  const hidden = !box || box.style.display === 'none';
  if (!hidden) problems.push('AG: 正式块已有内容时，空直播块应收起（否则留一个纯重复块）');
  console.log(`   AG 有兜底时 收起=${hidden}`);
}

console.log('场景 AE｜官方（轮询）覆盖了直播已画的事件后，直播区必须同步让位，不许两份并存');
// 用户审计（2026-09-14）："复制"。真浏览器实测窗口 1.2s / 11.3s / 2.1s——官方
// 轮询把同一条事件拉进 CONV 后，正式块画了它，而直播完成区因为**重绘签名里没有
// 覆盖度**而不重绘，于是同一张工具卡两份并存（长命令期间没有新事件，窗口拖到分钟级）。
resetLive();
CONV = { session:'s1', seqs:[7], cache:{7:{events:[
  {id:'R7-E01', type:'user', role:'user', content:'干活', time:''}], blocks:[]}} };
mountRound(DOM.content, 7, false, 'Main');
META.round_list = [{seq:7, active_view:'Main', events:1, steps_used:1}];
META.status = 'in_progress';
const AE_TOOLCALL = [{id:'ae1', function:{name:'read_file', arguments:'{}'}}];
const AE_EV = {id:'R7-E09', type:'tool_call', agent:'Main', time:'2026-09-13T17:00:09',
               role:'assistant', thinking:'', content:'', tool_calls:AE_TOOLCALL};
// ① 直播事件到达（官方还没覆盖）→ 直播完成区画出工具卡
applyChunk({k:'event', e:AE_EV, ag:'Main', st:2});
renderLive();
renderLive();          // 再画一帧：等渲染副作用（calls 登记）把签名稳定下来
{
  const done = DOM.content.querySelector('.livebox .live-done');
  // 用 `dataset` 读：迷你 DOM 的 `getAttribute` 是空桩（踩过），`dataset` 才是真存储
  const liveCards = done ? [...done.querySelectorAll('.tcard')].map(c=>c.dataset.call) : [];
  if (!liveCards.includes('ae1')) problems.push('AE: 前置条件没造出来（直播区应有工具卡 ae1）');
}
// ② 官方轮询把它拉进 CONV（覆盖度变化；**没有**新的直播事件）→ 官方那侧画它
CONV.cache[7].events.push({id:'R7-E09', type:'tool_call', agent:'Main',
                           time:'2026-09-13T17:00:09', role:'assistant', content:'',
                           tool_calls:AE_TOOLCALL});
drawConv(DOM.content, false);
renderLive();                                   // 直播这一侧的一帧（无新事件）
{
  const liveCards = [...DOM.content.querySelectorAll('.livebox .tcard')].map(c=>c.dataset.call);
  const formalCards = [...DOM.content.querySelectorAll('.crow.agent[data-seq="7"]:not(.live-prov) .gbox .tcard')]
    .map(c=>c.dataset.call);
  const both = liveCards.filter(x=>x!=='?'&&formalCards.includes(x));
  if (both.length) problems.push(`AE: 同一张工具卡在直播区与正式块并存（${both.join(',')}）——官方覆盖后直播区没让位`);
  console.log(`   AE 直播卡=${liveCards.length} 正式卡=${formalCards.length} 并存=${both.length}`);
}

console.log('场景 AF｜SSE 事件到达：尾巴必须**同步**清掉（不许被节流留 120ms）');
// 用户审计（2026-09-14）："尾部滞留"。真浏览器定位：SSE 路径对**结构性变化**
// 也走 120ms 节流（`scheduleLive`），而事件到达时在飞缓冲已被清空
// （`applyChunk` → `flushLiveStep`）——DOM 里的旧尾巴就多留最多 120ms 才消失。
// 轮询路径（dirty>=2 立刻 renderLive）本来就是对的，两条路要同口径。
resetLive();
mountRound(DOM.content, 7, false, 'Main');
META.round_list = [{seq:7, active_view:'Main', events:0, steps_used:1}];
META.status = 'in_progress';
LIVE.next = 0;
applyChunk({k:'ans', s:'上一段在飞正文，事件到达后必须立刻让位'});
renderLive();
{
  const tail = DOM.content.querySelector('.livebox .live-tail');
  // 迷你 DOM：清空走 `innerHTML=''`（setter 会清 children，但**不清 textContent**
  // ——那是第八个同类假象，断言必须看 innerHTML）
  if (!tail || !String(tail.innerHTML || '').length)
    problems.push('AF: 前置条件没造出来（在飞正文应渲染在尾巴里）');
}
sseConnect('jx', 0);
const AF_ES = ES_INSTANCES[ES_INSTANCES.length - 1];
if (!AF_ES || !AF_ES.onmessage) problems.push('AF: SSE 桩没拿到 onmessage（夹具失效）');
else {
  // 一条**事件**到达：applyChunk 会清空在飞缓冲；渲染必须同步跟上（节流不许留尾巴）
  AF_ES.onmessage({ data: JSON.stringify({ k:'event', st:1, ag:'Main',
    e: ev({id:'R7-E31', type:'final_answer', agent:'Main', role:'assistant',
           content:'正式落地的回答'}) }) });
  const tail = DOM.content.querySelector('.livebox .live-tail');
  const tailLen = tail ? String(tail.innerHTML || '').length : -1;
  const done = DOM.content.querySelector('.livebox .live-done');
  const doneTxt = done ? domAllText(done) : '';
  if (tailLen > 0)
    problems.push(`AF: 事件到达后尾巴还留着 ${tailLen} 字（被 120ms 节流拖住 → 视觉上"残留一下"）`);
  if (!/正式落地的回答/.test(doneTxt))
    problems.push('AF: 事件内容没有同步画进完成区');
  console.log(`   AF 事件后尾巴=${tailLen} 完成区含正文=${/正式落地的回答/.test(doneTxt)}`);
}

console.log('场景 AD｜轮号已知但正式块还没画出来：连画两帧不许把自己的临时块摘掉');
// 用户报（2026-09-14）："R3 的消息块一会出现、一会消失"。真浏览器逐帧记录 +
// 调用追踪定位到的根因：临时块（`.live-prov`）为了挂进消息流也带 `data-seq` 与
// `.gbox[data-agent]`，于是 `liveGbox` 查"正式块"时**匹配到它自己** → 走精确命中
// 分支 `removeProv` 摘掉自己，而直播盒子还留在那个被摘除的节点里
// （`parentNode===host` 成立，不会重挂）→ 整块从页面上消失，直到下一帧重建
// 临时块（实测每 ~150ms 闪一次）。
resetLive();
LIVE.seq = 9;                                   // 轮号由服务端报回；正式块还没渲染出来
META.round_list = [{seq:9, active_view:'Main', events:1, steps_used:1}];
applyChunk({k:'think', s:'第一段思考'});
renderLive();                                   // 第 1 帧：造出临时块 + 直播节点
const AD_FIRST = DOM.content.querySelector('.livebox');
{
  const provs = DOM.content.querySelectorAll('.crow.live-prov').length;
  if (!AD_FIRST || provs !== 1)
    problems.push('AD: 前置条件没造出来（第 1 帧后应有 1 个临时块 + 直播节点）');
}
applyChunk({k:'think', s:'，第二段思考'});      // 文字继续长（每帧都会 renderLive）
renderLive();                                   // 第 2 帧
{
  const after = DOM.content.querySelector('.livebox');
  const provs = DOM.content.querySelectorAll('.crow.live-prov').length;
  if (!after) problems.push('AD: 第 2 帧把直播节点弄丢了（临时块被当成正式块摘掉）');
  else if (after !== AD_FIRST) problems.push('AD: 第 2 帧换了新节点（展开态/增量会丢）');
  if (provs !== 1) problems.push(`AD: 临时块应恒为 1，实际 ${provs}（自己被自己摘掉）`);
  console.log(`   AD 两帧后 直播节点=${after?1:0} 临时块=${provs} 同一节点=${!!(AD_FIRST&&after===AD_FIRST)}`);
}

console.log('场景 AT｜终止本轮按钮：文案不带时长（用户口径 2026-09-15）');
// 原先把运行时长每 300ms 写进按钮（"⏹ 终止本轮 · 6m39s"）——按钮宽度来回抖，而且
// 时长在顶部 runline 本来就一直走字。口径：按钮只留文案。
resetLive();
{
  const stop = document.getElementById('stop');
  if (!stop) problems.push('AT: 静态 id 里没有 #stop（夹具失效）');
  else {
    stop.style.display = '';
    LIVE.job = 'j1'; LIVE.startedAt = Date.now() - 125000;   // 跑了 2 分钟多
    tickStopBtn(); tickStopBtn();
    const txt = String(stop.textContent || '');
    if (/[0-9]/.test(txt)) problems.push(`AT: 终止按钮仍带数字（${JSON.stringify(txt)}）——时长只留给顶部 runline`);
    if (!/终止本轮/.test(txt)) problems.push('AT: 终止按钮文案丢了');
    console.log(`   AT 按钮=${JSON.stringify(txt)}`);
  }
}

console.log('场景 AU｜开放轮：界面上要看得见"未闭合"，并给一个「▶ 续跑」');
// 用户口径（2026-09-15）："开放轮，添加UI显示，同时增加续跑按钮"。轮的闭合规则是
// "只有 AI 产出最终回答才算一轮"——中断/步数用尽的轮一直开着，新输入会并入它。
// 这件事原先在界面上完全看不见（用户看到的是"新消息跑去了一个子 agent"）。
resetLive();
{
  const bar = document.getElementById('openbar');
  if (!bar) problems.push('AU: 静态 id 里没有 #openbar（夹具失效）');
  else {
    META.round_list = [{seq:7, active_view:'A', steps_used:3, events:9, end_state:'open',
                        route_hops:2}];
    META.live_job = null; LIVE.job = null; TAB = 'conv'; CUR = 's1';
    renderOpenBar();
    const txt = String(bar.innerHTML || '');
    if (!bar.classList.contains('show')) problems.push('AU: 末轮未闭合却没有显示未闭合条');
    if (!/R7/.test(txt)) problems.push('AU: 未闭合条没写轮号');
    if (!/续跑/.test(txt)) problems.push('AU: 未闭合条没有「续跑」按钮');
    if (!/新消息将并入本轮/.test(txt)) problems.push('AU: 没说明"新输入并入本轮"（用户困惑的就是这个）');
    // **只看可见文字**：`innerHTML` 里还带着 title 提示（提示里也有"当前执行方"，
    // 拿它当判据就会假绿——写这条时踩到）
    const visTxt = bodyOf(txt);
    if (!/当前执行方/.test(visTxt)) problems.push('AU: 没写"当前执行方"（容易和最初接手的那位混淆）');
    if (!/第 2 跳/.test(visTxt)) problems.push('AU: 有轮内转交时没写跳数（用户看到执行方变了会以为写错）');
    // 作业在跑 → 收起（runline 在说这事，两条会打架）
    LIVE.job = 'j1'; renderOpenBar();
    const runningOn = bar.classList.contains('show');
    if (runningOn) problems.push('AU: 作业在跑时未闭合条没收起');
    LIVE.job = null;
    // 轮已闭合 → 收起
    META.round_list = [{seq:7, active_view:'A', end_state:'completed'}];
    renderOpenBar();
    const closedOn = bar.classList.contains('show');
    if (closedOn) problems.push('AU: 轮已闭合还挂着未闭合条');
    // 无转交时不该出现跳数话术
    META.round_list = [{seq:7, active_view:'A', route_hops:0, end_state:'open'}];
    renderOpenBar();
    const noHop = !/跳/.test(bodyOf(String(bar.innerHTML || '')));
    if (!noHop) problems.push('AU: 没有轮内转交却写了跳数');
    console.log(`   AU 未闭合条=${!runningOn && !closedOn ? '开(合并)/跑收起/闭合收起' : '不对'} 轮号/续跑/说明=${/R7/.test(txt)&&/续跑/.test(txt)&&/新消息将并入本轮/.test(txt)} 当前执行方=${/当前执行方/.test(txt)} 跳数=${/第 2 跳/.test(txt)} 无跳不写=${noHop}`);
  }
}

console.log('场景 AY｜维护进度条：整理/分裂在后台跑，界面要有进度与结果');
// 用户报（2026-09-15）："分裂又失败了，且没有UI进度提示"。维护（整理+分裂）跑在
// **后台线程**里、轮一闭合作业就结束了——旧前端的 `/plan` 轮询以"有作业在跑"为前提，
// 于是那 1~2 分钟页面毫无反馈（实测一批 89s）。`/plan` 现在带 `maint` 现算态。
resetLive();
{
  const bar = document.getElementById('maintbar');
  if (!bar) problems.push('AY: 静态 id 里没有 #maintbar（夹具失效）');
  else {
    TAB = 'conv'; CUR = 's1'; META = {id:'s1', round_list:[]};
    // ① 整理中
    applyMaint({active:true, phase:'整理', elapsed:12});
    let txt = String(bar.textContent || '');
    if (!bar.classList.contains('show')) problems.push('AY: 维护在跑却没显示进度条');
    if (!/整理中/.test(txt) || !/12s/.test(txt)) problems.push('AY: 整理中没写阶段与已耗时：' + txt);
    // ② 分裂中
    applyMaint({active:true, phase:'分裂', elapsed:45});
    txt = String(bar.textContent || '');
    if (!/分裂中/.test(txt) || !/45s/.test(txt)) problems.push('AY: 分裂中没写阶段与已耗时：' + txt);
    // ③ 结束且被拒收 → 原因必须看得见（以前只落在 history 里）
    applyMaint({active:false, last_defect:'分裂产物被拒收（根基缺陷）：历史未落点：x.md'});
    txt = String(bar.textContent || '');
    if (!/拒收/.test(txt) || !/历史未落点/.test(txt)) problems.push('AY: 拒收原因没显示：' + txt);
    if (!bar.classList.contains('bad')) problems.push('AY: 拒收没标成异常色');
    // ④ 收起
    dismissMaint();
    if (bar.classList.contains('show')) problems.push('AY: 收起后进度条还在');
    // ⑤ 非对话页签不显示（与未闭合条同一策略：别的页签有各自的进度语义）
    applyMaint({active:true, phase:'分裂', elapsed:3});
    TAB = 'ledger'; renderMaintBar();
    if (bar.classList.contains('show')) problems.push('AY: 非对话页签也在显示维护条');
    TAB = 'conv'; dismissMaint();
  }
}

console.log('场景 AV｜跟随尾部：内容一次长高超过阈值，不许"跟着跟着突然停住"');
// 用户报（2026-09-15）："跟随着跟随着突然停到某一位置不跟随了"。两个叠加的毛病：
//   ① 自动 pin 有"离底部 < tol(≥160px)"门控——内容一次长高超过它，`near` 恒假、
//      再也不 pin，而没有任何滚动事件来纠正（FOLLOW 还挂着 ✓）；
//   ② 关跟随的条件是"任何滚动 + 离底 >140"——内容长高/原生锚定让浏览器自己调的
//      scrollTop 也被算成"用户滚的"，一关就不回来。
// 口径：FOLLOW 是**用户意图**（只由真实手势改），跟随亮着就钉到底。
resetLive();
{
  const c = DOM.content;
  // 元素监听没法在桩里跨 resetDom 存活（监听注册在旧节点上）——接线由 Python 侧
  // 静态核对（main() 里查 `$('#content').addEventListener('scroll'`）；这里测语义。
  META.round_list = [{seq:7, active_view:'A', events:1, steps_used:1}];
  META.status = 'in_progress';
  c.scrollHeight = 1000; c.clientHeight = 300; c.scrollTop = 0;   // 离底 700px
  FOLLOW = true; LAST_TOP = null; SCROLL_SELF_TS = 0; USER_TS = 0;
  REBUILDING = false;   // 桩里 rAF 是 no-op（真浏览器 anchorDone 会清），不手动清就会空跑
  applyChunk({k:'ans', s:'正文在长'});
  renderLive();
  const pinned = c.scrollTop === 1000;
  if (!pinned)
    problems.push(`AV: FOLLOW 亮着却没钉到底（scrollTop=${c.scrollTop}）——"跟着跟着突然停住"`);
  // ①' 不跟随时不许把人拽走
  FOLLOW = false; c.scrollTop = 0; renderLive();
  const stayed = c.scrollTop === 0;
  if (!stayed) problems.push('AV: FOLLOW=false 时直播重绘把视口拽到底部了');
  // ② 程序滚动（锚定/重建钳位，带 self 时间戳）不许关跟随
  FOLLOW = true; SCROLL_SELF_TS = Date.now(); c.scrollTop = 200;
  followScrollEvent(c);
  const afterSelf = FOLLOW;
  if (!afterSelf) problems.push('AV: 程序滚动把跟随关掉了');
  // ②' 无手势的自动上跳（距离不大）也不许关：从"贴底"被挪上 100/300px
  LAST_TOP = null; SCROLL_SELF_TS = 0; USER_TS = 0;
  c.scrollTop = 700; followScrollEvent(c);   // 建立 LAST_TOP（贴底）
  c.scrollTop = 600; followScrollEvent(c);   // 上跳 100 → 离底 100
  const small = FOLLOW;
  c.scrollTop = 400; followScrollEvent(c);   // 上跳 200 → 离底 300
  const mid = FOLLOW;
  if (!small || !mid) problems.push('AV: 内容自己长高导致的自动上跳把跟随关掉了');
  // ③ 真实手势 + 明显上滚 → 关（这是用户"我要看上面"的表达）
  USER_TS = Date.now(); c.scrollTop = 0; followScrollEvent(c);
  const off = !FOLLOW;
  if (!off) problems.push('AV: 用户滚轮上滚了，跟随没关');
  // ④ 滚回底部 → 恢复
  c.scrollTop = 700; followScrollEvent(c);
  const back = FOLLOW;
  if (!back) problems.push('AV: 滚回底部后跟随没恢复');
  console.log(`   AV 钉底=${pinned}｜不拽=${stayed}｜程序滚动不关=${afterSelf}｜自动上跳不关=${small&&mid}｜手势关=${off}｜回底恢复=${back}`);
}

console.log('场景 AW｜consult（传话）回复要有那位 agent 的消息块——名字含中文/括号也要认');
// 用户报（2026-09-15）："我看 B 和 A 交流了，但是在 UI 渲染中，看不到关于 A 的消息块。"
// 根因：`consultSender` 的归因正则只认 `[\w-]+`（ASCII），而 ledger 的产物是
// `{名字} 的回复：…`，名字可以是任意文本（实测 "CLI 核心线（数据层+命令+测试）"）——
// 匹配不上 → 回复被当普通工具结果折进调用卡（默认折叠）→ 界面上没有 A 的块。
resetLive();
{
  META.registry = [{id:'Main',name:'主agent'}, {id:'B',name:'Web 界面线'},
                   {id:'A',name:'CLI 核心线（数据层+命令+测试）'}];
  buildReg();   // 真实路径：登记册按 id 与 name 双索引（手搓 REG 会漏 name 键——踩过）
  META.round_list = [{seq:7, active_view:'B', events:4, steps_used:2, end_state:'open'}];
  META.live_job = null; LIVE.job = null; TAB = 'conv'; CUR = 's1';
  CONV = {session:'s1', seqs:[7], cache:{7:{events:[
    ev({id:'R7-E01', type:'user', role:'user', content:'干活'}),
    ev({id:'R7-E02', type:'tool_call', agent:'B', role:'assistant', content:'',
        tool_calls:[{id:'c1', function:{name:'consult',
                     arguments:'{"agent":"A","question":"对齐一下"}'}}]}),
    ev({id:'R7-E03', type:'tool_result', agent:'B', role:'tool', tool_call_id:'c1',
        content:'CLI 核心线（数据层+命令+测试） 的回复：同意，**数据层我来改**。'}),
    ev({id:'R7-E04', type:'tool_call', agent:'B', role:'assistant', content:'好的',
        tool_calls:[{id:'c2', function:{name:'run_command', arguments:'{}'}}]}),
  ], blocks:[]}}};
  drawConv(DOM.content, false);
  const rows = [...DOM.content.querySelectorAll('.crow.agent[data-seq="7"]')];
  const senders = rows.map(r => (r.querySelector('.gbox') || {dataset:{}}).dataset.agent);
  const aRow = rows.find(r => (r.querySelector('.gbox') || {dataset:{}}).dataset.agent === 'A');
  const txt = aRow ? domAllText(aRow) : '';
  if (!aRow) problems.push('AW: 传话回复没有生成回复方（A）的消息块——名字含中文/括号时归因失败');
  else if (!/同意，/.test(txt)) problems.push('AW: A 的块里没有回复正文');
  if (aRow && !/CLI 核心线/.test(txt)) problems.push('AW: 回复正文上方没有"谁回复的"标记');
  // 回复正文要**走 MD**（用户 2026-09-15 报："为什么 A 的回复没有 MD 渲染"）
  if (aRow && !/<strong>数据层我来改<\/strong>/.test(String(aRow.innerHTML || '')))
    problems.push('AW: 回复正文没走 MD 渲染（加粗的 markdown 原样显示）');
  if (consultSender('查无此人 的回复：x') !== null)
    problems.push('AW: 名册外的前缀被硬认成 agent（会误伤普通结果）');
  // ASCII 名字（老正则唯一能匹配的情形）也要能认——而且**不许抛**：2026-09-12 起的
  // 那版 `m.group(1)` 是 Python 写法，JS 里一匹配上就 TypeError（整次 drawConv 画不出来）
  REG['reader'] = {id:'reader', name:'reader'};
  const ascii = consultSender('reader 的回复：好');
  if (!ascii || ascii.id !== 'reader') problems.push('AW: ASCII 名字的传话回复认不出来（或抛错）');
  console.log(`   AW 块序=${senders.join('>')} A块=${!!aRow} 正文=${/同意/.test(txt)} 名册外不认=${consultSender('查无此人 的回复：x')===null} ASCII=${!!consultSender('reader 的回复：好')}`);
}

console.log('场景 AX｜选工作目录：点文件夹要拼出**绝对路径**（分隔符由服务端报）');
// 用户报（2026-09-15）："选择路径时可以返回上一级，不过我点文件夹时，会一直提示
// 目录不存在，导致我无法选择想要的工作路径。" 根因：前端拼子目录路径写死反斜杠
// （BS=92），Linux 上 `/home` + `\` + `lkf` → 服务端判"目录不存在"；另外 POSIX 根
// （`/`）在 `trimSep` 后是空串，会退化成裸名字（相对路径）。
// 现在拼路径用 `/api/fs/ls` 响应里的 `sep`，根单独处理。
const AX_ASK = [];
__deferred.push((async () => {
  const flush = async (n) => { for (let i = 0; i < (n || 8); i++) await Promise.resolve(); };
  const payload = {
    '/home':       {path:'/home', sep:'/', dirs:['lkf']},
    '/home/lkf':   {path:'/home/lkf', sep:'/', dirs:[]},
    '/':           {path:'/', sep:'/', dirs:['home']},
    'C:\\Users':   {path:'C:\\Users', sep:'\\', dirs:['me']},
    'C:\\Users\\me':{path:'C:\\Users\\me', sep:'\\', dirs:[]},
  };
  // **只接自己的工作目录请求，其余转交上一个桩**（写时踩到：两个场景都写 global.fetch，
  // 后安装的那个会把前一个盖掉，断言就对着别人的桩说话）
  const axPrev = global.fetch;
  global.fetch = (u) => {
    const p = String(u);
    if (!/\/api\/fs\/ls/.test(p)) return axPrev(u);
    AX_ASK.push(p);
    const q = /path=([^&]*)/.exec(p);
    const key = q ? decodeURIComponent(q[1]) : '';
    return Promise.resolve({ok:true, json:async () => (payload[key] || {path:key, sep:'/', dirs:[]})});
  };
  const errBox = () => String((document.getElementById('ws-err') || {}).textContent || '');
  const rowsOf = () => [...document.getElementById('ws-dirs').children];
  // ① Linux：/home 点 lkf → 必须是 /home/lkf（绝对、正斜杠）
  await browseTo('/home'); await flush();
  if (wsBrowsed !== '/home') problems.push(`AX: 进 /home 后路径不对（${wsBrowsed}）`);
  const r1 = rowsOf().find(r => /lkf/.test(r.textContent || ''));
  if (!r1) problems.push('AX: /home 下没列到 lkf（夹具失效）');
  else if (r1.dataset.p !== '/home/lkf')
    problems.push(`AX: 目录行的 data-p 不是绝对路径（${r1.dataset.p}）`);
  else {
    await browseTo(r1.dataset.p); await flush();
    const asked = AX_ASK[AX_ASK.length - 1] || '';
    if (wsBrowsed !== '/home/lkf')
      problems.push(`AX: 点文件夹后路径成了 ${wsBrowsed}（应为 /home/lkf）——反斜杠/相对路径又回来了`);
    if (!/path=%2Fhome%2Flkf/.test(asked))
      problems.push(`AX: 请求的不是绝对路径（${asked}）`);
    if (errBox()) problems.push(`AX: 点文件夹报错：${errBox()}`);
  }
  // ② POSIX 根：/ 点 home → 必须是 /home（不能退化成裸 "home"）
  await browseTo('/'); await flush();
  const r2 = rowsOf().find(r => /home/.test(r.textContent || ''));
  if (!r2) problems.push('AX: / 下没列到 home（夹具失效）');
  else {
    await browseTo(r2.dataset.p); await flush();
    if (wsBrowsed !== '/home')
      problems.push(`AX: 从根进子目录成了 ${wsBrowsed}（应为 /home）——根被 trimSep 吃掉了`);
  }
  // ③ Windows：分隔符仍是反斜杠（不能为了修 Linux 把 Windows 弄坏）
  await browseTo('C:\\Users'); await flush();
  const r3 = rowsOf().find(r => /me/.test(r.textContent || ''));
  if (!r3) problems.push('AX: C:\\Users 下没列到 me（夹具失效）');
  else {
    await browseTo(r3.dataset.p); await flush();
    if (wsBrowsed !== 'C:\\Users\\me')
      problems.push(`AX: Windows 拼接坏了（${wsBrowsed}）`);
  }
  console.log(`   AX /home→${'/home/lkf'}｜根→/home｜Windows→${'C:\\\\Users\\\\me'}｜报错=${errBox()||'无'}｜最后请求=${AX_ASK[AX_ASK.length-1]}`);
})());

console.log('场景 AZ｜顶部六格按步更新（跑轮期间随 /plan 重画，不再只等轮闭合）');
// 用户口径（2026-09-15）："改成按步更新，不要再按轮更新"；同日两条口径修正：
// ① "总步数"换成**工具调用**（步 = 一次 LLM 交互，与"LLM 调用"重复；工具调用数看并行度）；
// ② "TTFT 均值"只统计**干活轮**（ttft_work_*）——整理/分裂的批量调用输入 8–20 万 tok、
//    首字本来就有 15–123s，混进均值就"虚大"（用户报的就是这个）。
// `/plan` 除计划账本还带 usage/rounds/tools 的现算值，pollPlan 每 tick 拉一次 →
// `applyPlanKpis` 独立签名比对，一变就只重画六格。
{
  META = {id:'s1', status:'in_progress', rounds:7,
          usage:{calls:1,prompt:1000,cached:900,miss:100,completion:10,
                 ttft_sum:2.0,ttft_work_sum:2.0,ttft_work_n:1},
          tools:1, round_list:[{seq:7, steps_used:3}], registry:[]};
  CUR = 's1'; TAB = 'conv';
  window._kpiSig = '';
  const kpiText = () => { const el = document.getElementById('kpis'); return el ? domAllText(el) : ''; };
  const cellText = (i) => { const el = (document.getElementById('kpis').children||[])[i];
                            return el ? String(el.textContent || '') : ''; };
  renderHeader();                       // 首屏：META 里的（旧）数
  const t0 = kpiText();
  // /plan 现算值：prompt=900、工具调用 6、TTFT 干活轮样本 2 个共 6.0s。注意
  // ttft_sum=100（含一条维护批量调用）——若把维护算进均值会显示 50.0s，正是要防的。
  const plan1 = {usage:{calls:2,prompt:900,cached:810,miss:90,completion:20,
                        ttft_sum:100.0,ttft_work_sum:6.0,ttft_work_n:2},
                 rounds:7, steps:4, tools:6};
  const changed = applyPlanKpis(plan1);
  const t1 = kpiText();
  if (!changed) problems.push('AZ: 用量变了却没认（按步更新失效）');
  if (!/900/.test(t1)) problems.push('AZ: 六格没吃到 /plan 的现算值（Σprompt=900 没出现）');
  const toolTxt = cellText(4);
  if (!/6/.test(toolTxt) || !/次/.test(toolTxt)) problems.push('AZ: 工具调用没按 /plan 更新');
  if (/步/.test(toolTxt)) problems.push('AZ: 第 5 格还是"总步数"（应换成工具调用）');
  const ttftTxt = cellText(5);
  if (!/3\.0/.test(ttftTxt)) problems.push('AZ: TTFT 均值没按干活轮样本算（应 6.0/2=3.0）');
  if (/50/.test(ttftTxt)) problems.push('AZ: TTFT 均值把维护调用也平均进去了（虚大的来源）');
  if (t1 === t0) problems.push('AZ: 六格内容没变');
  // 同一份现算值再来一次：一个字节都不许动（不白刷 DOM）
  const again = applyPlanKpis(plan1);
  if (again) problems.push('AZ: 用量没变却重画了（签名比对失效）');
  const t2 = kpiText();
  if (t2 !== t1) problems.push('AZ: 幂等调用把六格改了');
  // 再长一步 → 又变
  const plan2 = {usage:{calls:3,prompt:999,cached:899,miss:100,completion:30,
                        ttft_sum:150.0,ttft_work_sum:9.0,ttft_work_n:3},
                 rounds:7, steps:5, tools:11};
  applyPlanKpis(plan2);
  const t3 = kpiText();
  if (!/999/.test(t3)) problems.push('AZ: 又长一步后六格没跟上');
  if (!/11/.test(cellText(4))) problems.push('AZ: 工具调用没跟着长（应 11）');
  // 六格之外不许被牵连（#tabs 每步重建会丢悬停态）
  const tabsBefore = String((document.getElementById('tabs')||{}).innerHTML||'');
  applyPlanKpis({usage:{calls:4,prompt:4000,cached:3600,miss:400,completion:40,
                        ttft_sum:200.0,ttft_work_sum:12.0,ttft_work_n:4},
                 rounds:7, steps:6, tools:13});
  const tabsAfter = String((document.getElementById('tabs')||{}).innerHTML||'');
  if (tabsAfter !== tabsBefore) problems.push('AZ: 按步刷新把 #tabs 也重建了（应只动六格）');
  console.log(`   AZ 变化认=${changed} Σ900=${/900/.test(t1)} 工具调用=${toolTxt.replace(/\s+/g,'')}`
              +` TTFT=${ttftTxt.replace(/\s+/g,'')} 幂等=${!again} 再长一步=${/999/.test(t3)}`
              +` tabs不动=${tabsAfter===tabsBefore}`);
}

console.log('场景 AS｜收尾交接：缓存落后/为空时必须"先补齐、再上屏"');
// 用户报（2026-09-15）：①"正文加载完会消失一会才出来" ②"会话结束整个消息块都没了，
// 只剩上面那条 R7 的信息，重新刷新之后才出来"。同一个根因：`CONV.cache` 是快照，
// 收尾（refreshAfterTurn）只补"还没进缓存的轮"，**已经进缓存的轮永不回补**——
// 而开放轮只要被 2.5s 轮询/首屏取过一次就在缓存里（可能只有 0～2 条事件）。
// 于是收尾那一刻正式渲染拿着旧（甚至空）事件表上屏、直播区同时被撤：
// 最后那段回答/整轮消失，等下一次轮询（或手动刷新）才回来。
// 修法：`convSyncPlan` 算差集（落后=增量补、新轮=整取），收尾/切回对话页先补齐再画。
resetLive();
{
  // ① 纯判定：落后的轮与新冒出来的轮必须分开算出来
  META.round_list = [{seq:7, events:3, active_view:'Main'},
                     {seq:8, events:1, active_view:'Main'}];
  CONV = {session:'s1', seqs:[7], cache:{7:{events:[{id:'R7-E01'}]}}};
  const plan = convSyncPlan();
  if (!plan.stale.includes(7))
    problems.push('AS: 事件数落后的轮没进 stale（收尾还会拿旧缓存上屏 → 正文消失一会）');
  if (!plan.fresh.includes(8))
    problems.push('AS: 新冒出来的轮没进 fresh（新轮画不出来）');
  console.log(`   AS 差集 落后=[${plan.stale}] 新轮=[${plan.fresh}]`);
}
// ② 走一遍收尾：stub 服务端（只有 /rounds 真数据），断言"画出来的那一帧就是完整的"
const AS_META = {id:'s1', status:'in_progress', workspace:'/tmp', usage:{},
  registry:[{id:'Main', name:'主agent'},{id:'A', name:'域甲'}],
  round_list:[{seq:7, events:3, steps_used:1, active_view:'Main'},
              {seq:8, events:1, steps_used:1, active_view:'Main'}]};
const AS_ROUND7 = [
  ev({id:'R7-E01', type:'tool_call', agent:'Main', thinking:'整轮的思考',
      tool_calls:[{id:'c1', function:{name:'read_file', arguments:'{}'}}]}),
  ev({id:'R7-E02', type:'tool_result', agent:'Main', role:'tool',
      tool_call_id:'c1', content:'文件内容'}),
  ev({id:'R7-E03', type:'final_answer', agent:'Main', role:'assistant',
      content:'最后那段回答'})];
const AS_ASK = [];
const asPrev = global.fetch;
global.fetch = (u)=>{
  const p = String(u); AS_ASK.push(p);
  let body = {};
  const mr = /\/rounds\/(\d+)/.exec(p);
  if (mr) {
    const seq = Number(mr[1]);
    const all = seq === 7 ? AS_ROUND7
      : [ev({id:'R8-E01', type:'final_answer', agent:'Main', role:'assistant',
             content:'新一轮的正文'})];
    const am = /after=([^&]*)/.exec(p);
    const after = am ? decodeURIComponent(am[1]) : '';
    const idx = all.findIndex(e=>e.id===after);
    body = {seq, events: idx>=0?all.slice(idx+1):all, blocks:[]};
  } else if (/\/api\/sessions\/s1$/.test(p)) body = AS_META;
  else if (/\/api\/sessions$/.test(p)) body = {sessions:[]};
  else return asPrev(p);        // 不是本场景关心的路径 → 交给上一个桩（别吞掉别人的请求）
  return Promise.resolve({ok:true, json:async()=>body});
};
// ②a 缓存落后两条：收尾后正式块里必须**已经有**最后的正文（不许"先空、再补"）
CONV = {session:'s1', seqs:[7], cache:{7:{events:[AS_ROUND7[0]]}}};
__deferred.push(refreshAfterTurn().then(()=>{
  const d = CONV.cache[7] || {events:[]};
  const g = DOM.content.querySelector('.crow.agent[data-seq="7"] .gbox');
  const txt = g ? domAllText(g) : '';
  const incReq = AS_ASK.some(p=>/\/rounds\/7\?after=/.test(p));   // 只认走了增量，id 值不重要（夹具会改名）
  if ((d.events||[]).length !== 3)
    problems.push(`AS: 落后轮没补齐（缓存 ${(d.events||[]).length}/3）`);
  if (!incReq)
    problems.push('AS: 补齐没走增量（?after=）——整轮重放会白拉一大坨');
  if (!/最后那段回答/.test(txt))
    problems.push('AS: 收尾那一帧正式块里没有最后的正文（正文消失一会/整块只剩 R 行）');
  if (!/R7/.test(domAllText(DOM.content)))
    problems.push('AS: 轮头 R7 丢了');
  console.log(`   AS 落后补齐 缓存=${(d.events||[]).length}/3 增量取=${incReq} 正式块含正文=${/最后那段回答/.test(txt)}`);
  // ②b 缓存为空（轮询在轮刚开时取过、随后页面在后台没再动）：整块不许只剩 R 行
  CONV = {session:'s1', seqs:[7], cache:{7:{events:[]}}};
  return refreshAfterTurn().then(()=>{
    const g2 = DOM.content.querySelector('.crow.agent[data-seq="7"] .gbox');
    const txt2 = g2 ? domAllText(g2) : '';
    if (!/最后那段回答/.test(txt2))
      problems.push('AS: 空缓存收尾后正式块里没有正文（只剩 R 行，要手动刷新）');
    if (!/整轮的思考/.test(txt2))
      problems.push('AS: 空缓存收尾后思考也没补上（整轮丢了）');
    console.log(`   AS 空缓存补齐 正式块含正文=${/最后那段回答/.test(txt2)} 含思考=${/整轮的思考/.test(txt2)}`);
  // ②c 切回对话页 / 点 ↻（renderConv 非 fresh 路径）同样口径：落后就先补再画
  CONV = {session:'s1', seqs:[7], cache:{7:{events:[AS_ROUND7[0]]}}};
  return renderConv(DOM.content).then(()=>{
    const g3 = DOM.content.querySelector('.crow.agent[data-seq="7"] .gbox');
    const txt3 = g3 ? domAllText(g3) : '';
    if (!/最后那段回答/.test(txt3))
      problems.push('AS: 切回对话页/点 ↻ 时没先补缓存（页内刷新看不到最新正文，只能整页刷新）');
    console.log(`   AS 页内刷新 正式块含正文=${/最后那段回答/.test(txt3)}`);
  });
  });
}));

console.log('');
console.log('场景 AU｜时间线卡片要带上**与对话流轮头同一套**状态（用户口径 2026-09-17：'
  + '"将每轮对话上面的状态信息，也显示到时间线中"）');
{
  META.registry = [{id:'Main', name:'主agent'}, {id:'A', name:'serve 接口层'}];
  REG = {Main:{id:'Main', name:'主agent'}, A:{id:'A', name:'serve 接口层'}};
  META.round_list = [
    {seq:1, active_view:'Main', aid:'Main', landing:'Main', events:9, steps_used:2,
     t0:'2026-09-17 10:00:00', t1:'2026-09-17 10:00:30', user_input:'改一下 serve.py',
     // serve 侧已把"折叠"折算成整理状态（折了＝已整理），前端只认 org_state
     folded:true, split_state:'ready', org_state:'done', stage:0,
     note:{seq:1, sentence:'把 attach 端点加到 serve.py', executor:'Main', failures:[]},
     usage:{prompt:12000, cached:11000, miss:1000, completion:300, calls:2,
            context:9000, by_agent:{Main:{prompt:12000, steps:2}}}},
    {seq:2, active_view:'A', aid:'A', landing:'A', hops:1, events:5, steps_used:1,
     t0:'2026-09-17 10:01:00', user_input:'@A 这个接口谁维护',
     split_state:'failed', split_note:'产物里的 R2 不在这批轮里', org_state:'',
     note_segments:[{executor:'A', sentence:'接口层由 A 维护', failures:[{text:'越界被拦'}]}],
     usage:{prompt:3000, cached:2500, miss:500, completion:80, calls:1, context:3000}},
  ];
  renderTimeline(DOM.content);
  const html = DOM.content.innerHTML;
  const brief = (html.match(/class="badge[^"]*"[^>]*>([^<]*)</g) || []).join('｜');
  check('AU 时间线', html);
  for (const [need, why] of [
    ['已整理', '折叠状态没以"已整理"进时间线'],
    ['分裂失败', '分裂状态没进时间线'],
    ['Σ 12K', '轮级 tok 用量没进时间线'],
    ['命中', '缓存命中率没进时间线'],
    ['⬤ A', '落点 agent 没进时间线'],
    ['Main → ', '轮内转交没进时间线'],
    ['分裂前', '阶段徽标没进时间线'],
    ['◆', '段落（一句话）没进时间线'],
  ]) {
    if (!html.includes(need)) problems.push('AU: ' + why + '（缺 ' + need + '）');
  }
  if (!html.includes('产物里的 R2 不在这批轮里'))
    problems.push('AU: 分裂失败的原因没挂进 title');
  if (html.includes('已折叠'))
    problems.push('AU: 又冒出"已折叠"——折叠要并进整理状态，不单列（用户口径）');
  console.log('   AU 时间线徽标：' + brief.slice(0, 150));
  // 只看**已折的那一轮**（R2 是未折轮，它显示"未整理"是对的）
  const card1=(html.split('data-seq="1"')[1]||'').split('data-seq="2"')[0];
  if (!card1.includes('已整理'))
    problems.push('AU: 已折的轮（R1）没显示"已整理"');
  if (card1.includes('未整理'))
    problems.push('AU: 已折的轮（R1）同时显示了"未整理"');
}

Promise.all(__deferred).then(()=>{
  if (problems.length) {
    console.log('渲染核对：失败 ' + problems.length + ' 项');
    problems.slice(0, 12).forEach(p => console.log('  ✗ ' + p));
    process.exitCode = 1;
    return;
  }
  console.log('渲染核对：通过（51 个场景，无 undefined/NaN，正文无机制说明词，直播区四症状 + 收尾/轮号/整理态 + 缓存补齐/未闭合条/维护进度条/跟随尾部不变量全查）');
});
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
    # 静态接线核对（渲染桩照不到的地方）：#content 的 scroll 监听必须真的挂着——
    # "跟随尾部"整条逻辑都由它触发，线断了桩里测不出来（元素监听没法跨 resetDom 存活）。
    for need, why in (
        ("closest('[data-p]')", "工作目录列表的点击委托（点文件夹不会进目录）"),
        ("el.dataset.p = join(", "目录行的 data-p（委托消费的路径）"),
        ("applyPlanKpis(d);", "顶部六格按步更新（/plan 的现算值没人应用）"),
        ("applyPlanCtx(d);", "上下文窗口按步更新（/plan 的现读观测没人应用）"),
    ):
        if need not in html:
            print("渲染核对：失败 1 项")
            print(f"  ✗ 接线：{why} 没接上")
            return 1
    if "$('#content').addEventListener('scroll'" not in html:
        print("渲染核对：失败 1 项")
        print("  ✗ 接线：#content 的 scroll 监听没挂上（跟随尾部永远不响应滚动）")
        return 1
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
