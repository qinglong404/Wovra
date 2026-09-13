"""usage 落账行的解析与"账本按轮不按段"（worklog §91）。

用户报："我想要让有多个agent参与的一轮上面，显示每个agent的命中率，和tok花费，
而不是只显示一个汇总。" —— 查下去发现**两个账目缺陷**（都不是显示问题）：

1. `parse_usage_row` 的 `by=` 段正则用 `[^\\]\\s]+` 取执行方名字，**不许有空格**，
   而域名是模型起的中文短语（实测「web 网络工具（web_fetch / web_search 实现与回归）」
   「LLM 网络检索方案调研（外部开源方案选型）」都带空格）→ 这些段**整条匹配不上、
   静默丢掉**：会话 20260913-151842-2628dd 的 R8 记录里明明有两段，投影出来只剩 Main；
2. 同一条正则的另一半问题：`_KV` 全行扫描，而 `by=[… prompt=167709 …]` 里也有
   同名键 → **轮级数字被最后一段分账顶掉**（R8 轮总 404,157 显示成 236,448，会话级
   Σprompt 跟着少算）。

外加 `core._work_loop` 每段开头清零账本 → 同一轮的后续段把前一段的**分账连同 token
一起丢**（R7：轮总 43 步、分账只有 25 步，且那行 prompt 恰好等于单段分账额）。
"""
import json

from wovra import serve
from wovra import task as task_module

# 会话 20260913-151842-2628dd 的**原文**（R8：两个 agent，域名带空格）
R8_ROW = (
    "[managed] round=8 steps=10 context=33,696 working=416,313 org=0 compaction=0 "
    "prompt=404,157 completion=12,156 total=416,313（思考 8,791） "
    "缓存命中 297,856 tok（73.7%） 未命中 106,301 tok（26.3%） "
    "等效输入 116,230 tok ttft=41.4s（峰值 8.6s） "
    "by=[Main steps=2 prompt=167709 cached=94208 miss=73501 completion=1651] "
    "[LLM 网络检索方案调研（外部开源方案选型） steps=8 prompt=236448 "
    "cached=203648 miss=32800 completion=10505]"
)


def test_parse_usage_row_keeps_agent_names_with_spaces():
    """执行方名字**允许含空格**——不许再整段丢掉（用户看到的"只有汇总"就是这个）。"""
    row = serve.parse_usage_row(R8_ROW)
    assert row is not None
    by = row["by_agent"]
    assert set(by) == {"Main", "LLM 网络检索方案调研（外部开源方案选型）"}, by
    assert by["Main"]["prompt"] == 167709
    assert by["LLM 网络检索方案调研（外部开源方案选型）"]["prompt"] == 236448
    # 每个 agent 的命中率算得出来（前端就是拿这两个数显示"命中 x%"）
    assert by["Main"]["cached"] / by["Main"]["prompt"] < 0.6      # 换视图首调：56.2%
    assert by["LLM 网络检索方案调研（外部开源方案选型）"]["cached"] / \
        by["LLM 网络检索方案调研（外部开源方案选型）"]["prompt"] > 0.8


def test_parse_usage_row_round_totals_not_clobbered_by_segments():
    """轮级数字取**分账段之前**那段——否则被最后一段的 `prompt=` 顶掉。"""
    row = serve.parse_usage_row(R8_ROW)
    assert row["prompt"] == 404157, "轮总被 by= 段里的同名键覆盖了"
    assert row["completion"] == 12156
    assert row["cached"] == 297856 and row["miss"] == 106301
    assert row["steps"] == 10
    # 不变式：轮总 == 各段之和（这正是"多 agent 各自花费"要能对上的前提）
    assert sum(b["prompt"] for b in row["by_agent"].values()) == row["prompt"]
    assert sum(b["steps"] for b in row["by_agent"].values()) == row["steps"]


def test_round_usage_map_projects_per_agent_breakdown(tmp_path, monkeypatch):
    """投影把分账带给前端（页面上每个 agent 一行 tok · 命中）。"""
    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path)
    data = {"history": [{"kind": "usage", "time": "2026-09-13T17:00:14",
                         "detail": R8_ROW}]}
    usage = serve.round_usage_map(data)
    agg = usage.get(8)
    assert agg is not None
    assert len(agg["by_agent"]) == 2
    assert agg["prompt"] == 404157


def test_usage_rows_partition_the_round_without_double_counting(tmp_path, monkeypatch):
    """一轮落多次账时，行与行**不重叠**（写增量），且 `last_stats` 仍保留轮内累计。

    同一轮会落多次账：中断/超限落一次（`closed=False`），续跑收尾再落一次。
    `round_usage_map` 按轮把多行相加，所以每行必须是**增量**；同时
    `last_stats` 不能清零——`cli/render.py` 在轮收尾后读它显示本轮花费
    （把清零写进去时，`test_rounds`/`test_maintenance` 当场红了两条）。
    """
    from wovra.agent import Agent
    from wovra.task import Task

    from ._helpers import _StubLLM

    (tmp_path / "tasks").mkdir(exist_ok=True)
    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path / "tasks")
    task = Task.create(goal="分段落账")
    agent = Agent(llm=_StubLLM(), tools=[], task=task)
    agent.current_round = {"seq": 3, "active_view": "Main", "events": [],
                           "end_state": "open"}
    agent.rounds.append(agent.current_round)
    agent.last_stats = agent._fresh_stats()
    agent._stats_round = 3

    def add(name, steps, prompt):
        b = agent.last_stats["by_agent"].setdefault(
            name, {"steps": 0, "calls": 0, "prompt": 0, "cached": 0,
                   "miss": 0, "completion": 0})
        b["steps"] += steps
        b["prompt"] += prompt
        b["cached"] += prompt - 10
        b["miss"] += 10
        agent.last_stats["llm_calls"] += steps
        agent.last_stats["prompt_tokens"] += prompt
        agent.last_stats["cached_tokens"] += prompt - 10
        agent.last_stats["cache_miss_tokens"] += 10

    # 第一段：主 agent 1 步 / 100 tok → 中断落账
    add("Main", 1, 100)
    agent._usage_record_and_drain(closed=False)
    # 第二段：接手方 2 步 / 200 tok（累计口径）→ 收尾落账
    add("域甲", 2, 200)
    agent._usage_record_and_drain(closed=True)

    rows = [serve.parse_usage_row(h["detail"])
            for h in task.history if h.get("kind") == "usage"]
    rows = [r for r in rows if r]
    assert len(rows) == 2
    # 行与行不重叠：两行相加 == 轮内累计（不是把第一段再算一遍）
    assert sum(r["prompt"] for r in rows) == 300
    assert sum(r["steps"] for r in rows) == 3
    assert sum(r["by_agent"]["Main"]["prompt"] for r in rows if "Main" in r["by_agent"]) == 100
    assert sum(r["by_agent"]["域甲"]["steps"] for r in rows if "域甲" in r["by_agent"]) == 2
    # 第一行只覆盖第一段；第二行只有接手方那一段
    assert rows[0]["prompt"] == 100 and list(rows[0]["by_agent"]) == ["Main"]
    assert rows[1]["prompt"] == 200 and list(rows[1]["by_agent"]) == ["域甲"]
    # 显示口径：`last_stats` 仍是轮内累计（CLI 收尾后读它）
    assert agent.last_stats["prompt_tokens"] == 300
    assert agent.last_stats["llm_calls"] == 3


def test_usage_window_carries_across_segments_of_a_round(tmp_path, monkeypatch):
    """**账本按"轮"不按"段"**：同一轮再次进入 `_work_loop` 不许清零（实测 R7 缺 18 步）。

    `_work_loop` 原先每次进入就 `last_stats = self._fresh_stats()`，于是同一轮的
    后续段（步数超限后的 `\\c` 续跑、会合交棒后的第二段）把前一段的按调用方分账
    连同 token 一起丢掉——而轮总步数是从轮上续过来的，所以**汇总看着完整、分账缺段**。
    这里用 `max_turns=0` 直接走那两行账本初始化（循环一次都不进，末尾照旧抛步数超限）。
    """
    from wovra.agent import Agent
    from wovra.task import Task

    from ._helpers import _StubLLM

    (tmp_path / "tasks").mkdir(exist_ok=True)
    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path / "tasks")
    task = Task.create(goal="段账本")
    agent = Agent(llm=_StubLLM(), tools=[], task=task)
    # 构造参数里 `max_turns=0` 会被 `or _DEFAULT_MAX_TURNS` 吞掉，故构造后直接改；
    # 0 步上限 = 循环一次都不进，只走开头那两行账本初始化（末尾照旧抛步数超限）
    agent.max_turns = 0
    agent.current_round = {"seq": 7, "active_view": "A", "events": [],
                           "end_state": "open"}
    agent.rounds.append(agent.current_round)

    # 第一段：主 agent 走了 18 步（分账已记）
    agent.last_stats = agent._fresh_stats()
    agent._stats_round = 7
    agent.last_stats["by_agent"]["Main"] = {
        "steps": 18, "calls": 18, "prompt": 1000, "cached": 900,
        "miss": 100, "completion": 50}

    # 第二段进入同一轮：**接着累**，不清零
    try:
        agent._work_loop()
    except RuntimeError:
        pass
    assert agent.last_stats["by_agent"]["Main"]["steps"] == 18, \
        "同一轮的后续段把前一段的分账清零了（R7 那个缺 18 步的缺陷）"

    # 换到新的一轮：必须开新账本
    agent.current_round = {"seq": 8, "active_view": "B", "events": [],
                           "end_state": "open"}
    agent.rounds.append(agent.current_round)
    try:
        agent._work_loop()
    except RuntimeError:
        pass
    assert agent.last_stats["by_agent"] == {}, "新轮应开新账本"
