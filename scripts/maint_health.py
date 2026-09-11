"""维护健康体检（常驻仪器，零 LLM、只读）。

为什么常驻而不是临时脚本（2026-09-11 用户拍板，worklog §25）：同一批数字
（装配地板、折叠档成本、触发判定、缓存命中、账本体量）在会话里被反复
手写临时脚本量了十几遍，每次都重写、还常因输出过长被截断而重跑。仪器
写一次，之后一条命令出结论——这是省步数的正道：把取证固化成工具，而不是
每次重新发明。

用法：

    uv run --no-sync python scripts/maint_health.py            # 最近更新的会话
    uv run --no-sync python scripts/maint_health.py <task_id>

纪律：真实会话**只读**——先复制到临时 TASKS_ROOT 再加载，绝不改动 tasks/
（§13.2 教训：活跃会话的进程内存握着旧 rounds，手动改盘会被覆盖）。
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from wovra import economics as economics_module  # noqa: E402
from wovra import split_lifecycle as split_lifecycle_module  # noqa: E402
from wovra import task as task_module  # noqa: E402
from wovra import tokens as tokens_module  # noqa: E402
from wovra import views as views_module  # noqa: E402
from wovra.agent import Agent  # noqa: E402
from wovra.agent.support import _STATE_RENDER_BUDGET  # noqa: E402
from wovra.task import Task, _MODEL_SIDE_SECTIONS  # noqa: E402

REAL_TASKS = REPO / "tasks"


def _latest_task_id() -> str:
    cands = [
        p for p in REAL_TASKS.glob("*/task.json")
        if p.parent.name not in ("", ".")
    ]
    if not cands:
        raise SystemExit("tasks/ 下没有会话")
    return max(cands, key=lambda p: p.stat().st_mtime).parent.name


def _load_copy(task_id: str, tmp: Path) -> Task:
    src = REAL_TASKS / task_id
    if not src.exists():
        raise SystemExit(f"会话不存在: {task_id}")
    shutil.copytree(src, tmp / task_id, dirs_exist_ok=True)
    task_module.TASKS_ROOT = tmp
    return Task.load(task_id)


def _parse_llm_calls(task: Task) -> dict[str, list[int]]:
    """从 history 的 llm_call 记录里取 (prompt, cached)，按用途分组。"""
    out: dict[str, list[int]] = {}
    for h in task.history or []:
        if h.get("kind") != "llm_call":
            continue
        detail = str(h.get("detail") or "")
        purpose = detail[1:].split("]")[0] if detail.startswith("[") else "?"

        def _num(key: str) -> int:
            idx = detail.find(f"{key}=")
            if idx == -1:
                return 0
            raw = detail[idx + len(key) + 1:].split()[0].replace(",", "")
            try:
                return int(raw)
            except ValueError:
                return 0

        out.setdefault(purpose, []).append((_num("prompt"), _num("cached")))  # type: ignore[arg-type]
    return out  # type: ignore[return-value]


def report(task_id: str) -> None:
    with tempfile.TemporaryDirectory(prefix="wovra-health-") as td:
        tmp = Path(td)
        task = _load_copy(task_id, tmp)
        agent = Agent(llm=SimpleNamespace(model="stub"), tools=[], task=task)

        # 包住两种渲染，分别记账——比事后反推可靠
        sizes = {"compact": 0, "collapsed": 0, "originals": 0}
        rounds = {"compact": 0, "collapsed": 0}
        orig_c, orig_f = agent._render_compact, agent._render_collapsed

        def _count_original(r):
            # 装配对每个已整理轮**另外**追加一条 user 消息（👤 原文）——
            # 第一版仪器漏计这一块，导致分项之和比总估算少 84K（踩过）。
            sizes["originals"] += tokens_module.estimate(
                str((r.get("user_input") or {}).get("original") or "")
            )

        def compact(r):
            text = orig_c(r)
            if text:
                sizes["compact"] += tokens_module.estimate(text)
                rounds["compact"] += 1
                _count_original(r)
            return text

        def collapsed(r, ledger):
            text = orig_f(r, ledger)
            if text:
                sizes["collapsed"] += tokens_module.estimate(text)
                rounds["collapsed"] += 1
                _count_original(r)
            return text

        agent._render_compact, agent._render_collapsed = compact, collapsed
        # 系统人设（真实口径：cli.prompt 的 managed 提示词 + 工作区 AGENTS.md）
        try:
            from wovra.cli.prompt import _system_prompt
            agent.system_prompt = _system_prompt("managed")
        except Exception:  # noqa: BLE001
            pass
        msgs = agent._assemble_messages()
        # 分桶直接量装配输出本身（不靠估计反推）——把每条消息按角色/来源归类
        import os as _os
        if _os.environ.get("WOVRA_HEALTH_DEBUG"):
            ranked = sorted(
                ((Agent._estimate_messages([m]), m) for m in msgs),
                key=lambda x: -x[0],
            )
            print(f"[debug] 消息数 {len(msgs)}")
            for est, m in ranked[:8]:
                head = str(m.get("content") or "")[:70].replace("\n", " ")
                print(f"[debug]   {est:>7,} tok  {m.get('role')}  {head}")

        total = agent.last_context_estimate
        system_tok = tokens_module.estimate(agent.system_prompt or "")
        # 未整理轮原文：装配的分支条件是 `org_state != "done"`（全部历史轮），
        # **不是** `end_state == completed`——未闭合的轮同样全量进上下文。
        # 第一版按 completed 过滤，自检报出 57% 偏差（这就是它该干的活）。
        past = [r for r in task.rounds if r is not agent.current_round]
        raw_rounds = [r for r in past if r.get("org_state") != "done"]
        raw_msgs = [e["message"] for r in raw_rounds for e in r["events"]]
        raw_tok = Agent._estimate_messages(raw_msgs) if raw_msgs else 0
        # 当前开放轮（轮内赦免的追加消息）
        cur_tok = Agent._estimate_messages(agent._current_round_messages()) if agent.current_round else 0

        # 信封三块直接量（不靠减法反推——反推会把未计入的部分算进信封）
        state_tok = tokens_module.estimate(
            task.get_state().render(_STATE_RENDER_BUDGET, sections=_MODEL_SIDE_SECTIONS)
        )
        organized = [r for r in task.rounds
                     if r is not agent.current_round and r.get("org_state") == "done"]
        file_map = agent._file_map_lines(organized)
        map_tok = tokens_module.estimate("\n".join(file_map))
        todo_tok = tokens_module.estimate("\n".join(agent._todo_tail_lines()))
        envelope = state_tok + map_tok + todo_tok
        floor = total - raw_tok

        watermark, grace, cooldown = (
            agent._org_watermark, agent._org_grace, agent._org_cooldown
        )
        seq = task.rounds[-1]["seq"] if task.rounds else 0
        last_maint = max(
            (r["seq"] for r in task.rounds
             if r.get("org_state") == "done"
             or (r.get("org_state") == "pending" and r["seq"] in agent._org_inflight)),
            default=0,
        )
        below = total < watermark
        in_grace = seq <= grace
        in_cooldown = bool(last_maint) and seq - last_maint <= cooldown
        if below:
            verdict = f"不触发（未达线，还差 {watermark - total:,}）"
        elif in_grace:
            verdict = f"不触发（宽限期内，R{grace} 前全豁免）"
        elif in_cooldown:
            verdict = (f"不触发（最小间隔：上次维护 R{last_maint}，"
                       f"R{last_maint + cooldown + 1} 才放行）")
        else:
            verdict = "**到线且未被挡 → 下一轮闭合即触发**"

        state = task.get_state()
        model_side = state.render(_STATE_RENDER_BUDGET, sections=_MODEL_SIDE_SECTIONS)
        counts = {f: len(getattr(state, f) or []) for f in _MODEL_SIDE_SECTIONS}

        print(f"会话 {task_id}　轮 {seq}（未整理 {len(raw_rounds)}）　"
              f"org 代次 {agent._org_generation}")
        print("── 触发判定 ──")
        print(f"装机体量 {total:,} / 触发线 {watermark:,}　→ {verdict}")
        print(f"宽限 {grace} 轮　最小间隔 {cooldown} 轮　上次维护 R{last_maint or '-'}")
        print("── 地板构成（装配总估算拆分）──")
        print(f"合计 {total:,}　system {system_tok:,}　"
              f"用户原文 {sizes['originals']:,}　"
              f"紧凑 {sizes['compact']:,}（{rounds['compact']} 轮）　"
              f"折叠 {sizes['collapsed']:,}（{rounds['collapsed']} 轮）　"
              f"未整理原文 {raw_tok:,}（{len(raw_rounds)} 轮）")
        print(f"信封 {envelope:,}（账本 {state_tok:,} / 文件地图 {map_tok:,} / "
              f"todo {todo_tok:,}）")
        print(f"当前开放轮 {cur_tok:,}（{'进行中' if agent.current_round else '无'}）")
        print(f"未整理轮 {len(raw_rounds)} 轮")
        print(f"地板（合计 − 未整理原文）= {floor:,}"
              f"　占触发线 {floor / watermark:.0%}")
        # 自洽性校验：分项之和必须回到总估算（容差 5%）——仪器给错数比没数更糟
        parts = (system_tok + sizes["originals"] + sizes["compact"]
                 + sizes["collapsed"] + raw_tok + cur_tok + envelope)
        drift = abs(parts - total) / max(total, 1)
        flag = "OK" if drift <= 0.05 else f"**偏差 {drift:.0%}——口径有漏项**"
        print(f"分项核对：{parts:,} vs 合计 {total:,}　{flag}")
        print("── 缓存命中（全历史 llm_call）──")
        calls = _parse_llm_calls(task)
        if not calls:
            print("（无 llm_call 记账）")
        for purpose, pairs in sorted(calls.items()):
            p = sum(x[0] for x in pairs)
            c = sum(x[1] for x in pairs)
            rate = f"{c / p:.1%}" if p else "—"
            print(f"{purpose}: {len(pairs)} 次　prompt {p:,}　cached {c:,}　命中 {rate}")
        print("── 模型侧账本 ──")
        print(f"render {len(model_side):,} 字符（预算 {_STATE_RENDER_BUDGET:,}）"
              f"　分片保留 {counts}")
        all_counts = {
            f: len(getattr(state, f) or [])
            for f in ("goal", "current_status", "completed", "decisions", "known_issues",
                      "open_questions", "escalations", "experiments", "constraints")
        }
        print(f"全量条目 {all_counts}")
        print("── 域视图 ──")
        try:
            built = views_module.build_views(
                task.rounds, task.get_state(), registry=task.registry
            )
            verify = built.get("completeness") or {}
            views = built.get("views") or {}
            sizes_tok = [v.get("est_tokens", 0) for v in views.values()]
            span = f"{min(sizes_tok):,}–{max(sizes_tok):,}" if sizes_tok else "—"
            print(f"域 {len(views)}　块 {len(built.get('index') or {})}　"
                  f"归属完整 {verify.get('ok')}　缺归属 {len(verify.get('missing') or [])}　"
                  f"视图体量 {span} tok")
            # 逐层分裂的机械判据（各视图自身体量 + 是否到自己的水位）
            marks = views_module.view_watermarks(
                task.rounds, task.get_state(), registry=task.registry,
                watermark=watermark,
            )
            over = [n for n, m in marks.items()
                    if m.get("over") and n != views_module.MAIN_AGENT_ID]
            print(f"视图水位（阈值 {watermark:,}）："
                  f"{len(marks)} 个视图，到自身水位 {len(over)} 个"
                  + (f"——{'、'.join(over)}（考虑在内部再裂一层）" if over else ""))
            # 经济判据（零 LLM 机械算式，plan §13.3）
            assessed = economics_module.assess_from_watermarks(total, marks)
            for line in economics_module.format_lines(assessed):
                print(line)
            actions = split_lifecycle_module.plan(
                views_module.latest_domains(task.rounds), task.registry, marks,
                b_before=total,
            )
            for line in split_lifecycle_module.summary_lines(actions):
                print(line)
            # 路由粘滞率（active_view 落盘序列；开关关闭时全为 A，等于零信息）
            seq_views = [
                str(r.get("active_view") or views_module.MAIN_AGENT_ID)
                for r in task.rounds
            ]
            switches = sum(
                1 for a, b in zip(seq_views, seq_views[1:]) if a != b
            )
            sticky = (len(seq_views) - switches) / len(seq_views) if seq_views else 0.0
            print(f"active_view 序列 {len(seq_views)} 轮　切换 {switches} 次　"
                  f"粘滞率 {sticky:.1%}（目标 >80%）")
        except Exception as error:  # noqa: BLE001——体检不该因某节不可用而失败
            print(f"（域视图不可用：{error!r}）")


def main() -> int:
    args = sys.argv[1:]
    task_id = args[0] if args else _latest_task_id()
    report(task_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
