"""新整理契约的练兵场：在**会话副本**上按 V4 契约真跑一次"写"。

谈的都是机制，没人见过产物。这台仪器把新契约（`docs/context-management-v4-intent.md`
§3.2/§6.1）钉成一条命令：输入 = 真实装配快照 + 尾部追加的新指令；输出 = 每轮一句话
+ 账本增量。**输入输出都打出来**，效果能当场评。

与 `split_rehearsal.py` 同一套约定（AGENTS.md 一致）：
* **只动副本**——任务目录拷到 `/tmp/wovra-notes-rehearsal/tasks/`，原会话一个字节不碰；
* 副本上把全部已闭合轮按"未整理"渲染（清 org 产物），练的就是"第一次整理这些轮"；
* 真 LLM 一次调用，打印 usage（prompt/cached/miss/completion）——成本当场可见。

用法（项目根执行）：

    .venv/bin/python scripts/rehearse_round_notes.py <会话ID> --dry   # 只打输入，不调 LLM
    .venv/bin/python scripts/rehearse_round_notes.py <会话ID>         # 真跑一次

脚本不删（AGENTS.md §0.3）。
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from wovra import blocks as blocks_module          # noqa: E402
from wovra import lifecycle as lifecycle_module    # noqa: E402
from wovra import task as task_module              # noqa: E402
from wovra import tokens as tokens_module          # noqa: E402
from wovra.agent import MODE_MANAGED               # noqa: E402
from wovra.cli import _build_agent                 # noqa: E402
from wovra.llm import LLM                          # noqa: E402

REHEARSAL_ROOT = Path("/tmp/wovra-notes-rehearsal/tasks")
_PLAIN = False          # --plain：只给动作清单（不喂失败候选与结论草稿），用于对照

# ---------------------------------------------------------------- 新契约（提示词）
_CONTRACT = (
    "\n要求：\n"
    "1. **每轮一句话**：这轮做了什么、结论是什么。关键数字（行数/条数/次数）、文件路径、"
    "命令名原样保留。**不要复述用户要什么**——用户原话由 Runtime 逐字保留，你只写对这轮"
    "要求的响应。\n"
    "    **只写这一轮动作清单里的事**：清单里没有的文件、没调过的工具，一个字都不要提"
    "（跨轮借内容会被机械校验抓出来，整批退回重写）。\n"
    "2. 一句话里**不要用英文双引号**（会截断 JSON），需要引用用「」。\n"
    "3. **失败与坑**：以【失败候选】为底逐条核对——候选里有的必须写（同类可合并），"
    "候选里没有但你知道的照样要写；没有就给空数组。这一档**免裁剪**——原文折叠后就再也"
    "找不回来。\n"
    "6. 每条是**这一轮自己的交班记录**，不是这一批的总述。验收标准：把某一条单独拿给"
    "一个没看过会话的人，他能说清这轮发生了什么（**轮号是他唯一的坐标**）。\n"
    "7. 一句话 **80–250 字符**（信息量小的轮可更短，但不得空话）。\n"
    "4. **账本增量**：只写**新增**条目，不要重复账本里已有的。标量（现状/目标）不用你写，"
    "Runtime 从最新一轮取。\n"
    "5. 一轮一条、一个不落、与上面的轮号一一对应。\n\n"
    "完成后调用 submit_round_notes 提交（唯一出口，不要在正文输出 JSON）。"
)

_SCHEMA = {
    "type": "function",
    "function": {
        "name": "submit_round_notes",
        "description": (
            "提交「每轮一句话 + 账本增量」。仅限后台整理阶段调用（作为整理结果的"
            "唯一出口）；工作对话中调用无效，只返回说明文本。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "notes": {
                    "type": "array",
                    "description": "与待整理轮一一对应，一个不落、顺序一致",
                    "items": {
                        "type": "object",
                        "properties": {
                            "seq": {"type": "integer", "description": "轮次号"},
                            "sentence": {
                                "type": "string",
                                "description": (
                                    "一句话：这轮做了什么、结论是什么；关键数字/路径/命令"
                                    "原样保留；不复述用户输入"
                                ),
                            },
                            "failures": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": (
                                    "失败与坑：非零退出/越界拦截/路径不存在/方案被推翻。"
                                    "没有给空数组。免裁剪档"
                                ),
                            },
                        },
                        "required": ["seq", "sentence", "failures"],
                    },
                },
                "ledger_append": {
                    "type": "object",
                    "description": "本批新增的账本条目（只增不减；不重复已有条目）",
                    "properties": {
                        "constraints": {"type": "array", "items": {"type": "string"}},
                        "decisions": {"type": "array", "items": {"type": "string"}},
                        "known_issues": {"type": "array", "items": {"type": "string"}},
                        "open_questions": {"type": "array", "items": {"type": "string"}},
                    },
                },
            },
            "required": ["notes", "ledger_append"],
        },
    },
}


def _stage_copy(src_id: str) -> Path:
    src = Path(task_module.TASKS_ROOT) / src_id
    if not (src / "task.json").is_file():
        raise SystemExit(f"会话不存在：{src}")
    REHEARSAL_ROOT.mkdir(parents=True, exist_ok=True)
    dst = REHEARSAL_ROOT / src_id
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)
    return dst


def _round_files(r: dict) -> list[str]:
    """本轮触及的文件（代码机械取，模型不写这个字段）。"""
    out: list[str] = []
    for b in blocks_module.segment_round_by_file(r):
        if b.get("kind") != "file":
            continue
        f = str(b.get("file") or "")
        if f and f not in out:
            out.append(f)
    return out


# 像文件路径的 token（含命令输出里出现的）——机械校验用
_PATH_RE = re.compile(r"[\w./-]*[\w-]+\.[A-Za-z]{1,6}\b")


def _round_actions(r: dict) -> dict:
    """本轮的动作清单（代码从事件里机械抽出）——**锚**。

    2026-09-17 实测教训：把分块地图删掉之后，"每轮一句话"开始张冠李戴——5 个纯对话轮
    （零工具调用）里有 3 条产物在描述工具工作（R4 写"启动 4 题重测"、R5 写"停掉 bg-1
    exit_code=-9"、R9 写"逐行审查 prompt.py 并交付 349 行报告"，那些工作在别的轮）。
    旧契约里"块 ID 与内容必须严格一一对应"那句不是啰嗦，它是**位置锚**。锚由代码给，
    不要指望模型自觉。
    """
    tools: dict[str, int] = {}
    files: dict[str, str] = {}
    nonzero: list[str] = []
    pending: dict[str, str] = {}
    for e in r.get("events") or []:
        m = e.get("message") or {}
        for c in m.get("tool_calls") or []:
            fn = c.get("function") or {}
            name = str(fn.get("name") or "")
            if name:
                tools[name] = tools.get(name, 0) + 1
            pending[str(c.get("id") or "")] = str(fn.get("arguments") or "")
        if m.get("role") == "tool":
            args = pending.get(str(m.get("tool_call_id") or ""), "")
            code = re.search(r"exit_code=(-?\d+)", str(m.get("content") or ""))
            if code and code.group(1) != "0":
                cmd = re.search(r'"command"\s*:\s*"([^"]{0,50})', args)
                nonzero.append(f"{cmd.group(1) if cmd else args[:30]}(exit={code.group(1)})")
    for b in blocks_module.segment_round_by_file(r):
        if b.get("kind") == "file" and b.get("file"):
            ops = {str(o.get("op")) for o in (b.get("ops") or [])}
            mark = "".join(sorted({
                "write": "写", "edit": "改", "read": "读", "delete": "删",
            }.get(o, "") for o in ops)) or "动"
            files[str(b["file"])] = mark
    return {"tools": tools, "files": files, "nonzero": nonzero}


# 失败候选的机械特征（label, 正则）——把"回忆失败"降级成"核对候选"
_FAIL_SIGNS = (
    ("越界拦截", re.compile(r"路径越界|只允许访问项目目录|安全层")),
    ("不存在", re.compile(r"文件不存在|不存在|No such file|is not a file")),
    ("HTTP 错误", re.compile(r"HTTP\s*[45]\d\d|返回 HTTP|status[ =:]+[45]\d\d")),
    ("工具报错", re.compile(r"工具执行出错|Traceback|请求失败|连接失败")),
    ("被中止", re.compile(r"exit_code=-9|已被用户中止|中止本轮")),
    ("空/占位", re.compile(r"未找到|没有找到|占位|placeholder")),
)


# 只扫"执行/抓取/检索"类工具的返回：纯读类工具（read_file/page_text…）的返回是**文件正文**，
# 正文里出现"不存在""工具执行出错"这类字样全是假阳性（v4 实测：R2 的候选里大半是这么来的）。
_READ_LIKE = {
    "read_file", "page_text", "list_files", "glob_files", "search_files",
}


def _failure_hints(r: dict, limit: int = 6) -> list[tuple[str, str]]:
    """本轮工具结果里带错误特征的片段（机械扫；模型据此**核对**而不是回忆）。

    返回 (事件 ID, 候选文本)——带来源指针，产物里的失败项可一键 `expand_history` 回原文。
    """
    out: list[tuple[str, str]] = []
    calls: dict[str, str] = {}
    for e in r.get("events") or []:
        m = e.get("message") or {}
        for c in m.get("tool_calls") or []:
            calls[str(c.get("id") or "")] = str((c.get("function") or {}).get("name") or "")
        if m.get("role") != "tool":
            continue
        name = calls.get(str(m.get("tool_call_id") or ""), "")
        if name in _READ_LIKE:
            continue
        text = str(m.get("content") or "")
        # 执行类工具只在**真失败**时才扫：exit_code=0 的输出里 cat 出来的文档正文会把
        # "不存在""工具执行出错"这类字样全带进来（v4 实测：R2 的候选大半是这么来的）。
        if name.startswith(("run_command", "run_background")):
            code = re.search(r"exit_code=(-?\d+)", text)
            if not code or code.group(1) == "0":
                continue
        text = text.replace("\n", " ")
        for label, pat in _FAIL_SIGNS:
            hit = pat.search(text)
            if not hit:
                continue
            start = max(0, hit.start() - 20)
            snippet = text[start:hit.end() + 50].strip()
            item = (str(e.get("id") or ""), f"{label}：{snippet}")
            if item[1][:40] not in {x[1][:40] for x in out}:
                out.append(item)
            break
    return out[:limit]


def _conclusion_draft(r: dict, limit: int = 320) -> str:
    """本轮最终回答的首段（结论草稿）——模型把它改写成一句话，不必再从历史里找结论。"""
    for e in reversed(r.get("events") or []):
        if e.get("type") == "final_answer":
            text = str((e.get("message") or {}).get("content") or "").replace("\n", " ").strip()
            return text[:limit] + ("…" if len(text) > limit else "")
    return ""


def _actions_lines(batch: list[dict]) -> list[str]:
    """每轮一个**独立块**：轮号独占一行当锚点，块内分工具/文件/非零退出/失败候选/结论草稿。

    为什么分块而不是一行（2026-09-17）：锚的全部作用就是"边界清楚"，让轮号独占一行、
    块间空行分隔，比把三段信息塞进一条长行更醒目；代价每轮两三个 token。
    人性化那部分只在这里做——**产物里模型写的句子保持纯句子**，布局归代码。

    为什么给"失败候选"与"结论草稿"（同日）：这两样原本要模型自己从上百 K 的工具结果里
    找回来（R2 的失败信息埋在 101,796 字符里），是纯回忆；机械扫出来喂给它，任务就从
    "读历史做总结"退化成"核对 + 改写"——**拿便宜的新增输入换昂贵的推理 token**。
    """
    lines = ["\n[本轮动作清单]（Runtime 从每轮事件里机械抽出；**只许据此写，不许跨轮借内容**）"]
    for r in batch:
        a = _round_actions(r)
        lines.append("")
        lines.append(f"R{r['seq']}")
        if not a["tools"]:
            lines.append(
                "  无工具动作（纯对话轮）——只写谈了什么、结论是什么；"
                "草稿里出现的文件路径与工具名不要照抄"
            )
        else:
            lines.append("  工具：" + "、".join(
                f"{k}×{v}" for k, v in sorted(a["tools"].items())
            ))
            if a["files"]:
                lines.append("  文件：" + "；".join(
                    f"{v} {k}" for k, v in list(a["files"].items())[:12]
                ))
            if a["nonzero"]:
                lines.append("  非零退出：" + "；".join(a["nonzero"][:3]))
            hints = [] if _PLAIN else _failure_hints(r)
            if hints:
                lines.append("  失败候选（逐条核对，同类可合并；〔〕里是事件 ID，可回原文）：")
                lines += [f"    - 〔{eid}〕{text}" for eid, text in hints]
        draft = "" if _PLAIN else _conclusion_draft(r)
        if draft:
            lines.append(f"  结论草稿（改写成一句话，别照抄）：{draft}")
    return lines


def _check_alignment(notes: dict, batch: list[dict]) -> dict[str, list[str]]:
    """机械校验：产物里提到的路径/工具必须在本轮自己出现过（跨轮借内容当场抓）。

    两档（2026-09-17）：
    * **硬门**：路径（做了后缀匹配——`FINDINGS.md` 是 `output/gaia/FINDINGS.md` 的合法简称，
      不算跨轮；这一档无歧义、无假阳性，违者带诊断重发）；工具名只在**零工具动作轮**上
      算硬门——那种轮根本不可能是"提到某个通道"，只能是错位（R3 的 `web_search` 指的是
      检索通道，属软档）。
    * **软提示**：其余情况只留痕，人工或后续版本再判。
    """
    all_tools = {t for r in batch for t in _round_actions(r)["tools"]}
    hard: list[str] = []
    soft: list[str] = []
    for r in batch:
        n = notes.get(int(r["seq"]))
        if not n:
            continue
        text = str(n.get("sentence") or "") + " " + " ".join(n.get("failures") or [])
        own = _round_actions(r)
        zero_action = not own["tools"]
        allowed_paths = {
            m.group(0)
            for m in _PATH_RE.finditer(json.dumps(r.get("events") or [], ensure_ascii=False))
        }
        for p in {m.group(0) for m in _PATH_RE.finditer(text)}:
            frags = p.split("/")
            def _allowed(tok: str) -> bool:
                return any(a == tok or a.endswith("/" + tok) for a in allowed_paths)
            if _allowed(p) or all(_allowed(f) for f in frags if f):
                continue
            hard.append(f"R{r['seq']}: 提到本轮从没出现过的路径 `{p}`")
        for t in all_tools:
            if t in text and t not in own["tools"]:
                line = f"R{r['seq']}: 提到本轮没调用的工具 `{t}`"
                (hard if zero_action else soft).append(line)
    return {"hard": hard, "soft": soft}


def _hint_coverage(notes: dict, batch: list[dict]) -> tuple[int, int]:
    """失败候选的覆盖率：候选里有多少条能在产物里找到落点（按强特征 token 匹配）。"""
    strong = re.compile(r"[\w./-]*[\w-]+\.[A-Za-z]{1,6}\b|\b[45]\d\d\b|越界|不存在|超时|中止|占位")
    matched = total = 0
    for r in batch:
        n = notes.get(int(r["seq"])) or {}
        text = " ".join([str(n.get("sentence") or "")] + list(n.get("failures") or []))
        for _eid, h in _failure_hints(r):
            tokens = set(strong.findall(h))
            if not tokens:
                continue
            total += 1
            if any(t in text for t in tokens):
                matched += 1
    return matched, total


def _instruction(rounds: list[dict]) -> str:
    seqs = "、R".join(str(r["seq"]) for r in rounds)
    return (
        "[整理指令]\n"
        "以上是本会话的完整上下文。请把下列轮次各压缩成**一句话**，并给出账本增量："
        f"R{seqs}。\n"
        + "\n".join(_actions_lines(rounds)) + "\n"
        + _CONTRACT
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="新整理契约练兵（写：每轮一句话 + 账本增量）")
    ap.add_argument("session_id")
    ap.add_argument("--dry", action="store_true", help="只打输入与指令，不调 LLM")
    ap.add_argument("--plain", action="store_true",
                    help="对照档：只给动作清单，不喂失败候选/结论草稿")
    args = ap.parse_args()
    global _PLAIN
    _PLAIN = bool(args.plain)

    dst = _stage_copy(args.session_id)
    task_module.TASKS_ROOT = dst.parent
    task = task_module.Task.load(args.session_id)
    if not task.rounds:
        raise SystemExit("会话没有轮")

    # 副本上重做一次"第一次整理"：清掉旧产物（内存对象；不落盘）
    for r in task.rounds:
        r["org_state"] = ""
        r.pop("pending_org", None)
        r["blocks"] = blocks_module.segment_round_by_file(r)
    # 账本也清空，让这一次调用必须自己产出增量（展示最大信息量那一档）
    for field in ("goal", "current_status", "completed", "decisions",
                  "known_issues", "open_questions", "escalations",
                  "experiments", "constraints"):
        if hasattr(task.get_state(), field):
            setattr(task.get_state(), field, [] if field != "current_status" else "")

    agent = _build_agent(task, mode=task.mode or MODE_MANAGED, async_organization=False)
    agent.rounds = task.rounds
    batch = [r for r in task.rounds if str(r.get("end_state")) == "completed"]
    if not batch:
        raise SystemExit("没有已闭合轮")

    msgs = agent._assemble_full_messages()
    print(f"副本：{dst}")
    print(f"模型：{__import__('os').environ.get('Wovra_MODEL', '?')}")
    print(f"批次：R{batch[0]['seq']}–R{batch[-1]['seq']}（{len(batch)} 轮）")
    print(f"── 输入（{len(msgs)} 条消息，约 {agent._estimate_messages(msgs):,} tok）──")
    for m in msgs:
        content = m.get("content")
        if isinstance(content, list):
            head = "[多模态] " + str(content[0].get("text") or "")[:60]
        else:
            head = str(content or "")[:60].replace("\n", " ")
        print(f"  {m.get('role'):<9} {len(str(content or '')):>7} 字符  {head}")

    instruction = _instruction(batch)
    print("\n── 尾部追加的指令（逐字）──")
    print(instruction)
    if args.dry:
        return 0

    messages = msgs + [{"role": "user", "content": instruction}]
    print(f"\n调 LLM（输入约 {agent._estimate_messages(messages):,} tok）…", flush=True)
    llm = LLM()
    resp = llm.chat(messages, tools=[_SCHEMA])
    usage = resp.usage
    cached = int(getattr(getattr(usage, "prompt_tokens_details", None),
                         "cached_tokens", 0) or 0)
    reasoning = int(getattr(getattr(usage, "completion_tokens_details", None),
                            "reasoning_tokens", 0) or 0)
    print(f"usage: prompt={usage.prompt_tokens:,} cached={cached:,} "
          f"miss={usage.prompt_tokens - cached:,} "
          f"completion={usage.completion_tokens:,}（含推理 {reasoning:,}）")
    print("注：本模型把推理计入 completion；实测产物只占其中约 2K——"
          "**成本主体是模型读历史做压缩时的思考，不是产物体量**。")

    calls = (resp.choices[0].message.tool_calls or [])
    if not calls:
        print("\n（模型没有走出口，正文如下）\n" + str(resp.choices[0].message.content)[:2000])
        return 1
    raw = calls[0].function.arguments
    try:
        product = json.loads(raw)
    except json.JSONDecodeError as error:
        print(f"\n产物 JSON 解析失败：{error}\n原文前 2000 字：\n{raw[:2000]}")
        return 1

    notes = {int(n.get("seq", 0)): n for n in (product.get("notes") or [])}
    print("\n── 产物：每轮一句话（👤原文与涉及文件是**代码**填的）──")
    total_chars = 0
    for r in batch:
        n = notes.get(int(r["seq"]))
        total_chars += len(str((n or {}).get("sentence") or ""))
        print(f"\n[R{r['seq']}] 👤 {str((r.get('user_input') or {}).get('original') or '')[:70]}")
        files = _round_files(r)
        if files:
            print(f"      涉及文件：{'、'.join(files[:6])}" + ("…" if len(files) > 6 else ""))
        if not n:
            print("      ⚠ 这一轮没收到产物")
            continue
        print(f"      ▸ {n.get('sentence')}")
        for f in n.get("failures") or []:
            print(f"      ⚠ {f}")

    check = _check_alignment(notes, batch)
    print("\n── 机械校验（跨轮借内容）──")
    for d in check["hard"]:
        print(f"  ✗ 硬门 {d}")
    for d in check["soft"]:
        print(f"  · 软提示 {d}")
    if not check["hard"] and not check["soft"]:
        print("  ✓ 干净：产物提到的路径与工具，都在本轮自己出现过")
    hit, tot = _hint_coverage(notes, batch)
    print(f"  失败候选覆盖率：{hit}/{tot}"
          + ("（候选都写进了 failures）" if tot and hit == tot else ""))

    ledger = product.get("ledger_append") or {}
    print("\n── 产物：账本增量 ──")
    labels = {"constraints": "约束", "decisions": "决策",
              "known_issues": "已知问题", "open_questions": "待决"}
    for key, label in labels.items():
        items = ledger.get(key) or []
        if items:
            print(f"  {label}：")
            for it in items:
                print(f"    - {it}")

    print(f"\n── 体量对照 ──")
    print(f"新产物（每轮一句话）：{total_chars:,} 字符 / {len(notes)} 轮")
    print(f"旧产物（逐块描述，本会话原样）："
          f"{sum(len(str(v)) for r in batch for v in (r.get('block_summaries') or {}).values()):,} 字符")
    print("（旧产物不含 state_patch 重写与分块地图指令那些开销）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
