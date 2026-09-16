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

# ---------------------------------------------------------------- 新契约（提示词）
_CONTRACT = (
    "\n要求：\n"
    "1. **每轮一句话**：这轮做了什么、结论是什么。关键数字（行数/条数/次数）、文件路径、"
    "命令名原样保留。**不要复述用户要什么**——用户原话由 Runtime 逐字保留，你只写对这轮"
    "要求的响应。\n"
    "2. 一句话里**不要用英文双引号**（会截断 JSON），需要引用用「」。\n"
    "3. **失败与坑**：本轮只要有命令非零退出、越界拦截、路径不存在、方案被推翻，"
    "必须逐条写进 failures；没有就给空数组。这一档**免裁剪**——原文折叠后就再也找不回来。\n"
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


def _instruction(rounds: list[dict]) -> str:
    seqs = "、R".join(str(r["seq"]) for r in rounds)
    return (
        "[整理指令]\n"
        "以上是本会话的完整上下文。请把下列轮次各压缩成**一句话**，并给出账本增量："
        f"R{seqs}。\n" + _CONTRACT
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="新整理契约练兵（写：每轮一句话 + 账本增量）")
    ap.add_argument("session_id")
    ap.add_argument("--dry", action="store_true", help="只打输入与指令，不调 LLM")
    args = ap.parse_args()

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
