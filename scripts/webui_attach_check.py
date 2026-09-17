"""前端附件条核对（常驻仪器，零 LLM）：在 Node 里真跑一次附件函数。

用户口径（2026-09-17）：粘贴的大段内容/文件/图片转成附件——内容由装配期注入，
用户消息里只留一行引用。服务端与装配有 pytest 覆盖，**前端没有**；本仪器把它
补上（与 `webui_render_check.py` 同一套迷你 DOM 桩，复用它的 PREFIX）。

核对五件事：
1. 大段粘贴 → 进附件条（不按原文进用户消息）；
2. 短粘贴 → 照原文走（"用户说的话"不该被搬走）；
3. 粘贴图片 → 走 data URL；
4. 上传换回 `【附件】path=…` 引用行，列表清空；
5. 移除按钮能摘掉一条。

用法：uv run --no-sync python scripts/webui_attach_check.py（失败退出码 1）
"""
import json
import pathlib
import re
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from webui_render_check import PREFIX, _static_ids  # noqa: E402

SCENARIOS = r"""
// ---- 附件核对（2026-09-17）----
const problems = [];
const REQ = [];
globalThis.fetch = async (url, opt) => {
  REQ.push({url, body: JSON.parse(opt.body)});
  return {json: async () => ({ok: true, path: 'attachments/t-pasted.txt',
                              marker: '【附件】path=attachments/t-pasted.txt'})};
};
function FR(){}
FR.prototype.readAsDataURL = function(f){
  this.result = 'data:image/png;base64,AAAA';
  if (this.onload) this.onload();
};
globalThis.FileReader = FR;
CUR = 's1';
const box = $('#compose');
const bar = $('#attachbar');

// 1) 大段粘贴 → 转附件
const BIG = 'a'.repeat(2000);
const e1 = {clipboardData: {files: [], getData: () => BIG}, preventDefault(){ this.pd = true; }};
box._h.paste(e1);
if (!e1.pd) problems.push('大段粘贴没被拦下（会按原文进上下文）');
if (ATTACH.length !== 1) problems.push('大段粘贴没进附件列表');
if (!bar.classList.contains('show')) problems.push('附件条没显示');
if (!/行/.test(bar.innerHTML)) problems.push('chip 没显示行数');
if (!/pasted/.test(bar.innerHTML)) problems.push('chip 没显示文件名');

// 2) 短粘贴 → 照原文走
const e2 = {clipboardData: {files: [], getData: () => '短消息'}, preventDefault(){ this.pd = true; }};
box._h.paste(e2);
if (e2.pd) problems.push('短粘贴被误拦（正常输入进不去了）');
if (ATTACH.length !== 1) problems.push('短粘贴误入附件');

// 3) 粘贴图片 → data URL
box._h.paste({clipboardData: {files: [{name: 'shot.png', size: 4096}], getData: () => ''},
              preventDefault(){}});
if (ATTACH.length !== 2) problems.push('粘贴图片没进附件');
if (!/shot\.png/.test(bar.innerHTML)) problems.push('图片 chip 没渲染');
if (!/4K|4096/.test(bar.innerHTML)) problems.push('图片 chip 没显示体积');

// 4) 上传 → 换回引用行并清空
flushAttach().then(marks => {
  if (REQ.length !== 2) problems.push('上传请求数不对：' + REQ.length);
  const textReq = (REQ[0] || {}).body || {};
  const imgReq = (REQ[1] || {}).body || {};
  if (!textReq.name || !textReq.text) problems.push('文本附件上传体不对');
  if (!imgReq.name || !String(imgReq.data_url || '').startsWith('data:'))
    problems.push('图片附件没走 data URL');
  if (marks.length !== 2 || !/^【附件】path=/.test(String(marks[0])))
    problems.push('没换回引用行');
  if (ATTACH.length) problems.push('上传后列表没清空');
  if (bar.classList.contains('show')) problems.push('上传后附件条没隐藏');

  // 5) 移除按钮
  addAttachText('又粘了一段'.repeat(400));
  if (ATTACH.length !== 1) problems.push('第二次粘贴没进附件');
  const xs = bar.querySelectorAll('.x');
  if (!xs.length || !xs[0]._h || !xs[0]._h.click) problems.push('移除按钮没挂上事件');
  else xs[0]._h.click({});
  if (ATTACH.length) problems.push('移除按钮没生效');

  console.log('');
  if (problems.length) {
    console.log('附件核对：失败 ' + problems.length + ' 项');
    problems.forEach(p => console.log('  ✗ ' + p));
    process.exitCode = 1;
    return;
  }
  console.log('附件核对：通过（大段/短粘贴分流、图片 data URL、上传换引用行、移除）');
});
"""


def main() -> int:
    html = pathlib.Path("webui/index.html").read_text(encoding="utf-8")
    if "addEventListener('paste'" not in html:
        print("附件核对：失败 1 项")
        print("  ✗ 接线：输入框的 paste 监听没挂上（粘贴不会转附件）")
        return 1
    blocks = re.findall(r"<script[^>]*>(.*?)</script>", html, re.S)
    if not blocks:
        sys.exit("没有找到 <script> 块")
    out = pathlib.Path("output")
    out.mkdir(parents=True, exist_ok=True)
    prefix = PREFIX.replace("__STATIC_IDS__",
                            json.dumps(_static_ids(html), ensure_ascii=False))
    target = out / "_webui_attach_check.js"
    target.write_text(prefix + "\n;\n".join(blocks) + SCENARIOS, encoding="utf-8")
    r = subprocess.run(["node", str(target)], capture_output=True, text=True)
    sys.stdout.write(r.stdout)
    if r.returncode != 0 and not r.stdout.strip():
        sys.stderr.write(r.stderr[-2000:])
    return r.returncode


if __name__ == "__main__":
    raise SystemExit(main())
