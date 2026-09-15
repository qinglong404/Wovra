"""前端成败判定表的**同步仪器**（零依赖，常驻）。

为什么需要：`webui/index.html` 的 `statusOf()` 必须与 Python 侧
`wovra/tools/status.py` **同表同规则**——四处消费者里只要有一处自己另写一套，
用户看到的成/败就会互相打架（2026-09-14 报障的形态）。工具结果成败判定表现
在只有两个物理位置：Python 表（唯一权威）+ 前端镜像，本仪器负责让镜像不许漂移。

三层保证：
1. **表同步**：比对前端标记块里的四个数组与 Python 的四个元组（`--write` 重新生成）；
2. **规则同步**：把前端那段 JS 抽出来在 node 里跑，喂共享夹具
   `tests/fixtures/tool_status_cases.json` 的每一条，逐条比对 expect；
3. **诚实性**：标记块找不到、或夹具为空，直接报错退出（空转的仪器等于没有仪器）。

用法：
    uv run --no-sync python scripts/webui_status_sync.py          # 检查（默认）
    uv run --no-sync python scripts/webui_status_sync.py --write   # 重新生成前端表
"""
from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from wovra.tools import status as status_module  # noqa: E402

PAGE = ROOT / "webui" / "index.html"
FIXTURE = ROOT / "tests" / "fixtures" / "tool_status_cases.json"
BEGIN = "/* >>> STATUS_TABLE（由 scripts/webui_status_sync.py 生成，勿手改） */"
END = "/* <<< STATUS_TABLE */"

_TABLES = (
    ("STATUS_DENY_PREFIXES", status_module.DENY_PREFIXES),
    ("STATUS_DENY_INFIXES", status_module.DENY_INFIXES),
    ("STATUS_ERROR_PREFIXES", status_module.ERROR_PREFIXES),
    ("STATUS_ERROR_INFIXES", status_module.ERROR_INFIXES),
)

# 前端判定函数本体（生成物的一部分）：与 Python `tools/status.classify` 逐条对齐。
FRONTEND_RULES_JS = r"""// 只看首行（剥掉 task 记录的 `工具名 -> ` 包装）；exit_code 优先；三分类。
const STATUS_EXIT_CODE=/\bexit_code=(-?\d+)/;
const STATUS_WRAPPER=/^[A-Za-z_][A-Za-z0-9_.]*(?:\([^\n]*?\))? -> /;
function statusOf(content){
  let head=String(content==null?'':content).replace(/^[\s\uFEFF]+/,'').split('\n')[0];
  head=head.replace(STATUS_WRAPPER,'');
  if(!head.trim())return 'ok';
  const m=head.match(STATUS_EXIT_CODE);
  if(m)return m[1]==='0'?'ok':'error';
  if(STATUS_DENY_PREFIXES.some(p=>head.startsWith(p))
     ||STATUS_DENY_INFIXES.some(p=>head.indexOf(p)>=0))return 'deny';
  if(STATUS_ERROR_PREFIXES.some(p=>head.startsWith(p))
     ||STATUS_ERROR_INFIXES.some(p=>head.indexOf(p)>=0))return 'error';
  return 'ok';
}"""


def _js_array(name: str, items: tuple[str, ...]) -> str:
    body = ",\n  ".join(json.dumps(x, ensure_ascii=False) for x in items)
    return f"const {name}=[\n  {body}\n];"


def render_block() -> str:
    """按 Python 表渲染前端标记块（含判定函数本体）。"""
    lines = [BEGIN]
    lines.append("/* 与 Python 侧 wovra.tools.status 同表同规则：只看首行（剥掉 task")
    lines.append("   记录的 `工具名 -> ` 包装）、结构化 exit_code 优先、三分类 ok/error/deny。")
    lines.append(f"   共 {len(status_module.DENY_PREFIXES)} 条拒绝前缀 + "
                 f"{len(status_module.ESCAPE_MARKERS)} 条越界判据等；改判定请改 "
                 "src/wovra/tools/status.py，再跑同步仪器。 */")
    for name, items in _TABLES:
        lines.append(_js_array(name, items))
    lines.append(FRONTEND_RULES_JS)
    lines.append(END)
    return "\n".join(lines)


def extract_block(page_text: str) -> str | None:
    start = page_text.find(BEGIN)
    if start == -1:
        return None
    end = page_text.find(END, start)
    if end == -1:
        return None
    return page_text[start:end + len(END)]


def check_tables(current: str) -> list[str]:
    problems = []
    for name, items in _TABLES:
        found = re.search(rf"const {name}=\[(.*?)\];", current, re.S)
        if found is None:
            problems.append(f"前端缺少 {name}")
            continue
        got = re.findall(r'"((?:[^"\\]|\\.)*)"', found.group(1))
        got = [json.loads(f'"{x}"') for x in got]
        if got != list(items):
            missing = [x for x in items if x not in got]
            extra = [x for x in got if x not in items]
            problems.append(
                f"{name} 与 Python 表不一致（缺 {missing}；多 {extra}）"
            )
    return problems


def run_fixture_in_node(block: str) -> tuple[bool, str]:
    if shutil.which("node") is None:
        return True, "（node 不可用，跳过行为核对）"
    cases = json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]
    if not cases:
        return False, "共享夹具为空——仪器不许空转"
    harness = block + "\n" + """
const CASES = __CASES__;
const bad = [];
for (const c of CASES) {
  const got = statusOf(c.content);
  if (got !== c.expect) bad.push(`${c.expect} != ${got}（${c.why}）:: ${JSON.stringify(String(c.content).slice(0,60))}`);
}
if (bad.length) { console.log(bad.join('\\n')); process.exit(1); }
console.log(`OK ${CASES.length}`);
"""
    harness = harness.replace("__CASES__", json.dumps(cases, ensure_ascii=False))
    with tempfile.TemporaryDirectory() as tmp:
        script = pathlib.Path(tmp) / "status_check.js"
        script.write_text(harness, encoding="utf-8")
        proc = subprocess.run(["node", str(script)], capture_output=True,
                              text=True, encoding="utf-8", errors="replace")
    return proc.returncode == 0, (proc.stdout or proc.stderr or "").strip()


def main() -> int:
    page = PAGE.read_text(encoding="utf-8")
    block = extract_block(page)
    if "--write" in sys.argv:
        fresh = render_block()
        if block is None:
            sys.exit(f"页面里找不到标记块：{BEGIN}")
        PAGE.write_text(page.replace(block, fresh), encoding="utf-8")
        print(f"已重新生成前端判定表（{len(status_module.DENY_PREFIXES)+len(status_module.ERROR_PREFIXES)} 条前缀）")
        return 0
    if block is None:
        sys.exit(f"页面里找不到标记块：{BEGIN}")
    problems = check_tables(block)
    ok, detail = run_fixture_in_node(block)
    if not ok:
        problems.append("共享夹具在 node 侧不通过：\n" + detail)
    if problems:
        for p in problems:
            print("  ✗ " + p)
        return 1
    print(f"前端成败判定：表与 Python 一致，共享夹具 {detail}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
