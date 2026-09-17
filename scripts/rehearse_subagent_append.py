"""分裂后：子 agent 的**追加式**压缩演练（真跑一次，输入输出都打出来）。

形状（2026-09-17 用户口径：「记住是追加式的，例如输入 R1～R20，输出 R20 的总结」）：

    第一阶段（批次档）：把 R1…R{N-1} 各压缩成一句话 → 这就是**已累积的共享叙事**
    第二阶段（追加档）：输入 = 共享叙事（R1…R{N-1} 的一句话）＋ **本轮 R{N} 的原文**
                        ＋ 该域自己的文件清单 → 输出**只有 R{N} 的一句话**与失败

两阶段都是真 LLM 调用；追加档的机械校验：产物里**只允许出现 R{N} 一条**（写了别的轮就是
越界），另核对失败候选是否落进 failures。

用法（项目根执行）：

    .venv/bin/python scripts/rehearse_subagent_append.py <会话ID> [--round 8] [--domain 提示词审查] [--dry]

约定与 AGENTS.md 一致：**只动副本**（`/tmp/wovra-notes-rehearsal/`），原会话一个字节不碰。
脚本不删（§0.3）。契约与 schema 从 `rehearse_round_notes` 复用，不重写。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import rehearse_round_notes as R                      # noqa: E402
from wovra.agent import MODE_MANAGED                  # noqa: E402
from wovra.cli import _build_agent                    # noqa: E402
from wovra.llm import LLM                             # noqa: E402
from wovra import task as task_module                 # noqa: E402

REHEARSAL_ROOT = Path("/tmp/wovra-notes-rehearsal/tasks")

_APPEND_HEAD = (
    "\n[整理指令·追加档]\n"
    "以上是**共享叙事**（前面每轮已经各自压缩成一句话，全 agent 共享、逐字节相同）"
    "与**本轮原文**（尚未折叠）。\n"
    "**只写本轮 R{seq} 的一句话**：不要重写、不要复述、不要合并前面任何一轮"
    "（它们已经定稿了，你的产物只会被**追加**在它们后面）。\n"
    "本轮**可能由多个 agent 协作完成**（交棒）：那你只写**自己这一棒**做了什么；"
    "别的参与者的部分由它们各自追加，你不要替它们写、也不必综述全局。\n"
)


def _call(llm: LLM, messages: list[dict]) -> dict:
    resp = llm.chat(messages, tools=[R._SCHEMA])
    usage = resp.usage
    cached = int(getattr(getattr(usage, "prompt_tokens_details", None),
                         "cached_tokens", 0) or 0)
    reasoning = int(getattr(getattr(usage, "completion_tokens_details", None),
                            "reasoning_tokens", 0) or 0)
    print(f"  usage: prompt={usage.prompt_tokens:,} cached={cached:,} "
          f"miss={usage.prompt_tokens - cached:,} completion={usage.completion_tokens:,}"
          f"（含推理 {reasoning:,}）")
    calls = resp.choices[0].message.tool_calls or []
    if not calls:
        print("  ⚠ 没有走出口，正文：", str(resp.choices[0].message.content)[:300])
        return {}
    try:
        return json.loads(calls[0].function.arguments)
    except json.JSONDecodeError as error:
        print(f"  ⚠ 产物 JSON 解析失败：{error}")
        return {}


def main() -> int:
    ap = argparse.ArgumentParser(description="子 agent 追加式压缩演练")
    ap.add_argument("session_id")
    ap.add_argument("--round", type=int, default=0, help="追加哪一轮（默认最后一个有工具动作的轮）")
    ap.add_argument("--domain", default="", help="展示该域自己的文件清单（子 agent 视角）")
    ap.add_argument("--dry", action="store_true", help="只打输入，不调 LLM")
    args = ap.parse_args()

    src = task_module.TASKS_ROOT / args.session_id
    if not (src / "task.json").is_file():
        raise SystemExit(f"会话不存在：{src}")
    REHEARSAL_ROOT.mkdir(parents=True, exist_ok=True)
    dst = REHEARSAL_ROOT / args.session_id
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)
    task_module.TASKS_ROOT = REHEARSAL_ROOT
    task = task_module.Task.load(args.session_id)

    rounds = [r for r in task.rounds if str(r.get("end_state")) == "completed"]
    target = next((r for r in rounds if int(r["seq"]) == args.round), None)
    if target is None:
        target = next(
            (r for r in reversed(rounds) if R._round_actions(r)["tools"]), rounds[-1]
        )
    seq = int(target["seq"])
    prior = [r for r in rounds if int(r["seq"]) < seq]

    for r in task.rounds:                       # 副本上重做：全部按未整理渲染
        r["org_state"] = ""
        r.pop("pending_org", None)
    agent = _build_agent(task, mode=task.mode or MODE_MANAGED, async_organization=False)
    agent.rounds = task.rounds

    domain_files: list[str] = []
    if args.domain:
        entry = next((e for e in (task.registry or [])
                      if str(e.get("name")) == args.domain
                      or str(e.get("id")) == args.domain), None)
        domain_files = [str(x) for x in ((entry or {}).get("files") or [])]

    print(f"副本：{dst}")
    print(f"追加轮：R{seq}　前面已定稿：R{prior[0]['seq']}–R{prior[-1]['seq']}（{len(prior)} 轮）"
          if prior else f"追加轮：R{seq}（没有更早的轮）")
    if args.domain:
        print(f"子 agent：{args.domain}　本域文件 {len(domain_files)} 个"
              + (f"：{ '、'.join(domain_files[:6]) }" if domain_files else ""))

    # ---------- 第一阶段：批次档，产出"已累积的共享叙事" ----------
    narrative: dict[int, dict] = {}
    if prior:
        print("\n=== 第一阶段：批次档（R1…R{0} 各一句话）===".format(prior[-1]["seq"]))
        msgs = agent._assemble_full_messages()
        msgs.append({"role": "user", "content": R._instruction(prior, task)})
        if args.dry:
            print("（--dry：跳过第一阶段调用）")
        else:
            product = _call(LLM(), msgs)
            narrative = {int(n.get("seq", 0)): n for n in (product.get("notes") or [])}
            for r in prior:
                n = narrative.get(int(r["seq"])) or {}
                print(f"  R{r['seq']} ▸ {str(n.get('sentence') or '（缺产物）')[:100]}")

    # ---------- 第二阶段：追加档，只写本轮 ----------
    story: list[str] = ["[共享叙事]（每轮一句话，追加式；这些轮的原文已折叠，盘上可取回）"]
    for r in prior:
        n = narrative.get(int(r["seq"])) or {}
        if n.get("sentence"):
            story.append(f"R{r['seq']} ▸ {n['sentence']}")
    if len(story) == 1:
        story.append("（尚无已折叠轮）")

    block: list[str] = ["", "[本轮原文]", f"R{seq}"]
    for u in R._user_turns(target):
        block.append(f"  用户：{u}")
    a = R._round_actions(target)
    if a["tools"]:
        block.append("  工具：" + "、".join(f"{k}×{v}" for k, v in sorted(a["tools"].items())))
    if a["files"]:
        block.append("  文件：" + "；".join(f"{v} {k}" for k, v in list(a["files"].items())[:12]))
    for hint_eid, hint in R._failure_hints(target):
        block.append(f"  失败候选〔{hint_eid}〕：{hint}")
    draft = R._conclusion_draft(target)
    if draft:
        block.append(f"  结论草稿（改写，别照抄）：{draft}")
    if domain_files:
        block.append("  本域自己的文件（可改；别人的只读）：" + "、".join(domain_files[:8]))

    ledger = R._ledger_lines(task)
    instruction = (_APPEND_HEAD.format(seq=seq) + "\n".join(story) + "\n"
                   + "\n".join(block) + "\n"
                   + ("\n[账本已有条目]（**不要重复写这些**；只写新增）\n"
                      + "\n".join(ledger) + "\n" if ledger else "")
                   + R._CONTRACT
                   + f"\n追加档只需 submit 一条：notes 里**只有 seq={seq}**。")
    messages = [{"role": "system", "content": agent.system_prompt or ""}]
    messages += [{"role": "user", "content": "\n".join(story)}]
    messages += [e["message"] for e in target["events"]]
    messages.append({"role": "user", "content": instruction})

    print(f"\n=== 第二阶段：追加档（只写 R{seq}）===")
    print(f"输入构成：共享叙事 {len(story) - 1} 行 + 本轮原文 {len(target['events'])} 条事件"
          f"（约 {agent._estimate_messages(messages):,} tok）")
    if args.dry:
        print("\n--- 指令（逐字）---\n" + instruction)
        return 0

    product = _call(LLM(), messages)
    notes = product.get("notes") or []
    # 执行者由**代码**盖章（不让模型自报身份——本会话的病根就是身份错位）
    executor = args.domain or str(target.get("active_view") or "") or "Main"
    print(f"\n--- 追加产物（应该只有 R{seq} 一条；执行者〔{executor}〕由代码盖章）---")
    extra = [int(n.get("seq", 0)) for n in notes if int(n.get("seq", 0)) != seq]
    multi = len(notes) > 1        # 一轮多棒：每棒各一条
    for n in notes:
        tag = f"〔{executor}〕" if (multi or executor != "Main") else ""
        print(f"[R{n.get('seq')}]{tag} ▸ {n.get('sentence')}")
        for f in n.get("failures") or []:
            ev = str((f or {}).get("evidence") or "")
            print(f"    ⚠ {R._fail_text(f)}" + (f"　〔{ev}〕" if ev else ""))
    print(f"\n机械校验：产物 {len(notes)} 条　越界（写了别的轮）：{extra or '无'}"
          f"　→ {'✓ 追加合规' if not extra and notes else '✗ 不合格'}")
    ledger = product.get("ledger_append") or {}
    for key, label in (("constraints", "约束"), ("decisions", "决策"),
                       ("known_issues", "已知问题"), ("open_questions", "待决")):
        for item in ledger.get(key) or []:
            print(f"  账本·{label}：{item}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
