"""长会话免费验收（零 API）：真实会话数据 + 真实装配/维护链。

背景：tasks/ 里最大的真实会话 35 轮、代次最多 1 代——折叠窗口
（keep_min = 最新代 - 2）在真实数据上从未触发过。代码重构 + 换模型后，
整条 水位→整理→暂存→promote→代次→折叠 链路没有一次端到端证据。

本脚本用真实会话内容做底、用**录制的整理产物**当 org 输出，驱动真实的
_maintenance / _assembly 代码，零 LLM 费用：

  A. 真实会话载入：代次从磁盘恢复（含"修复前无字段"的旧数据）、
     真实 35 轮装配、崩溃遗留 pending 的 promote 时机；
  B. 在真实轮次上连续跑 4 批整理（真实 org→stage→promote→代次 链），
     制造 4 代，核对折叠窗口只折叠最老一代、重启后恢复到第 4 代。

纪律：会话数据只读——先复制到临时 TASKS_ROOT 再加载，绝不改写
tasks/ 下的历史记录。

用法：.venv/bin/python scripts/acceptance_long_session.py
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from wovra import blocks as blocks_module  # noqa: E402
from wovra import task as task_module  # noqa: E402
from wovra.agent import Agent  # noqa: E402
from wovra.task import Task  # noqa: E402

REAL_TASKS = REPO / "tasks"
# 同一会话的前后两份记录：orgtest 带 org_generation（修复后），
# 643355 无该字段（修复前落盘）——代次恢复的天然回归样本。
SESSION_NEW = "20260909-181052-orgtest"
SESSION_OLD = "20260909-181052-643355"

OK = "PASS"
NG = "FAIL"
_failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    mark = OK if cond else NG
    if not cond:
        _failures.append(name)
    line = f"  [{mark}] {name}"
    if detail:
        line += f"  —— {detail}"
    print(line)


# ---------------------------------------------------------------- 替身 LLM

def _chunk(delta):
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=delta, finish_reason=None)], usage=None
    )


def _fragment(arguments: str):
    return SimpleNamespace(
        index=0,
        id="replay-call",
        function=SimpleNamespace(name="submit_organization", arguments=arguments),
    )


class _ReplayLLM:
    """把录制好的 submit_organization 参数当作 LLM 输出回放（零 API）。

    每次 chat = 消耗一个预置 payload；池空说明真实链多跑了一次调用，
    直接报错而不是静默兜底。
    """

    model = "replay"

    def __init__(self, payloads: list[str] | None = None):
        self.payloads = list(payloads or [])
        self.calls: list[dict] = []

    def chat(self, messages, tools=None, stream=False, **kwargs):
        self.calls.append({"messages": messages, "tools": tools})
        if not self.payloads:
            raise AssertionError("回放池耗尽：真实维护链多发起了一次 LLM 调用")
        arguments = self.payloads.pop(0)
        delta = SimpleNamespace(
            content=None,
            tool_calls=[_fragment(arguments)],
            reasoning_content=None,
            model_extra=None,
        )
        yield _chunk(delta)


# ---------------------------------------------------------------- 数据装配

def load_session(task_id: str, tmp_root: Path) -> Task:
    """把真实会话复制到临时 TASKS_ROOT 后加载（只读纪律：不动原记录）。"""
    shutil.copytree(REAL_TASKS / task_id, tmp_root / task_id, dirs_exist_ok=True)
    task_module.TASKS_ROOT = tmp_root
    return Task.load(task_id)


def _live_blocks(r: dict) -> list[dict]:
    return blocks_module.segment_round_by_file(r)


def _summary_for(r: dict, block: dict, recorded: dict) -> str:
    """块描述：录制里有就逐字用，没有则用路由信息合成（唯一合成处）。"""
    rec = recorded.get(r["seq"]) or {}
    if block["id"] in rec:
        return rec[block["id"]]
    bits = [f"{block['start_event']}~{block['end_event']}"]
    if block.get("file"):
        bits.append(block["file"])
    elif block.get("command_types"):
        bits.append("命令[" + ", ".join(block["command_types"]) + "]")
    return "（回放合成，录制缺失）" + " · ".join(bits)


def build_payload(rounds: list[dict], recorded: dict, goal: str) -> str:
    """按真实产物格式构造一次批量整理输出。"""
    items = []
    for r in rounds:
        ui = r.get("user_input") or {}
        items.append(
            {
                "seq": r["seq"],
                "normalized_user_input": ui.get("normalized")
                or f"R{r['seq']} 意图（回放）",
                "key_constraints": ui.get("key_constraints") or "",
                "block_summaries": [
                    {"id": b["id"], "summary": _summary_for(r, b, recorded)}
                    for b in _live_blocks(r)
                ],
            }
        )
    return json.dumps(
        {"rounds": items, "state_patch": {"goal": goal}}, ensure_ascii=False
    )


class _RenderSpy:
    """包住 agent 的两种渲染，记录每轮走了紧凑还是折叠。"""

    def __init__(self, agent: Agent):
        self.collapsed: list[int] = []
        self.compact: list[int] = []
        orig_c, orig_f = agent._render_compact, agent._render_collapsed

        def compact(r):
            self.compact.append(r["seq"])
            return orig_c(r)

        def collapsed(r, ledger):
            self.collapsed.append(r["seq"])
            return orig_f(r, ledger)

        agent._render_compact = compact
        agent._render_collapsed = collapsed


# ---------------------------------------------------------------- 验收 A

def part_a(tmp: Path) -> None:
    print("\n== A. 真实会话：代次恢复 + 真实装配 + 崩溃遗留 promote ==")
    task = load_session(SESSION_NEW, tmp)
    agent = Agent(llm=_ReplayLLM(), tools=[], task=task)

    disk_gens = sorted(
        {r.get("org_generation") for r in task.rounds if r.get("org_generation")}
    )
    done_n = sum(1 for r in task.rounds if r.get("org_state") == "done")
    check(
        f"orgtest 代次从磁盘恢复（磁盘 max={max(disk_gens) if disk_gens else 0}）",
        agent._org_generation == (max(disk_gens) if disk_gens else 0),
        f"agent._org_generation={agent._org_generation}",
    )

    spy = _RenderSpy(agent)
    msgs = agent._assemble_messages()
    check(
        f"真实 35 轮装配成功（30 个已整理轮全部紧凑视图，单代不折叠）",
        len(spy.compact) == done_n and not spy.collapsed,
        f"compact={len(spy.compact)} collapsed={len(spy.collapsed)} 体量≈{agent.last_context_estimate} tok",
    )

    pending_before = sum(1 for r in agent.rounds if r.get("pending_org"))
    agent._promote_org_results()
    pending_after = sum(1 for r in agent.rounds if r.get("pending_org"))
    applied = sum(1 for r in agent.rounds if r.get("block_summaries"))
    check(
        f"崩溃遗留 pending 在下一轮开启时生效（{pending_before} 轮 → {pending_after}）",
        pending_after == 0 and applied > 0,
        f"产物落盘到 {applied} 轮",

    )

    old = load_session(SESSION_OLD, tmp)
    agent_old = Agent(llm=_ReplayLLM(), tools=[], task=old)
    check(
        "修复前旧数据（无 org_generation 字段）按 done→第 1 代恢复",
        agent_old._org_generation == 1,
        f"agent._org_generation={agent_old._org_generation}",
    )


# ---------------------------------------------------------------- 验收 B

def part_b(tmp: Path) -> None:
    print("\n== B. 真实轮次上连跑 4 批整理：代次 → 折叠窗口 → 重启恢复 ==")
    task = load_session(SESSION_NEW, tmp)
    agent = Agent(llm=_ReplayLLM(), tools=[], task=task)

    # 录制产物先留存，再把这些轮重置回"未整理"，保留真实内容/事件。
    recorded = {r["seq"]: dict(r.get("block_summaries") or {}) for r in agent.rounds}
    for r in agent.rounds:
        r["org_state"] = ""
        for field in ("org_generation", "pending_org", "block_summaries"):
            r.pop(field, None)
    agent._org_generation = 0

    organized = agent.rounds[:30]
    batches = [organized[0:10], organized[10:20], organized[20:25], organized[25:30]]
    print(f"  批次划分：{ [ (b[0]['seq'], b[-1]['seq']) for b in batches ] }")

    for generation, batch in enumerate(batches, 1):
        agent.llm.payloads.append(build_payload(batch, recorded, "长会话验收目标"))
        ok, _exchange = agent._organize_rounds(
            batch, base_messages=agent._assemble_messages()
        )
        check(f"第 {generation} 批：真实 _organize_rounds 成功（org→stage）", ok)
        check(
            f"第 {generation} 批：org_state=done 且代次={generation}",
            all(
                r["org_state"] == "done" and r.get("org_generation") == generation
                for r in batch
            ),
        )
        check(
            f"第 {generation} 批：promote 前产物只进暂存、正式字段不动",
            all(r.get("pending_org") for r in batch)
            and all(not r.get("block_summaries") for r in batch),
        )
        agent._promote_org_results()
        check(
            f"第 {generation} 批：promote 后暂存清空、产物落正式字段",
            all("pending_org" not in r and r.get("block_summaries") for r in batch),
        )

    check("4 批整理 = 4 代", agent._org_generation == 4, f"{agent._org_generation}")

    spy = _RenderSpy(agent)
    msgs = agent._assemble_messages()
    collapsed = sorted(set(spy.collapsed))
    compact = sorted(set(spy.compact))
    check(
        "折叠窗口 keep_min=2：仅最老第 1 代（R1-R10）被折叠",
        collapsed == [r["seq"] for r in batches[0]],
        f"collapsed={collapsed}",
    )
    check(
        "第 2/3/4 代（R11-R30）保持紧凑视图",
        compact == [r["seq"] for r in organized[10:]],
        f"compact 轮数={len(compact)}",
    )
    folded_msgs = [
        m for m in msgs if "（折叠）" in str(m.get("content") or "")
    ]
    gen1_visible = sum(1 for r in batches[0] if not r.get("merged_skip"))
    check(
        "折叠真实落进上下文：第 1 代非合并轮全部渲染为折叠视图",
        len(folded_msgs) == gen1_visible,
        f"折叠视图 {len(folded_msgs)} 条"
        f"（合并组跳过的 {len(batches[0]) - gen1_visible} 轮由组首显示）",
    )
    check(
        "折叠不是丢弃：第 2-4 代仍带块细节，可 expand_history 取回",
        any("块细节：" in str(m.get("content") or "") for m in msgs),
    )

    reloaded = Agent(llm=_ReplayLLM(), tools=[], task=Task.load(SESSION_NEW))
    check(
        "重启后代次从磁盘恢复到第 4 代（不会与旧批次撞号反转折叠）",
        reloaded._org_generation == 4,
        f"agent._org_generation={reloaded._org_generation}",
    )
    spy2 = _RenderSpy(reloaded)
    reloaded._assemble_messages()
    check(
        "重启后视图一致：仍是第 1 代折叠、2-4 代紧凑",
        sorted(set(spy2.collapsed)) == [r["seq"] for r in batches[0]]
        and sorted(set(spy2.compact)) == [r["seq"] for r in organized[10:]],
        f"collapsed={sorted(set(spy2.collapsed))}",
    )


def main() -> int:
    print("Wovra 长会话免费验收（真实数据，零 API）")
    with tempfile.TemporaryDirectory(prefix="wovra-accept-") as td:
        root = Path(td)
        part_a(root / "a")
        part_b(root / "b")
    print("\n" + "=" * 60)
    if _failures:
        print(f"结果：{len(_failures)} 项未通过")
        for name in _failures:
            print("  - " + name)
        return 1
    print("结果：全部通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
