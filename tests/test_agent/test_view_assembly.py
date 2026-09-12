"""视图装配测试（Level 1 第二/三步：装配读轮上的 active_view）。

钉住四件事（本大步的三条验收标准的机器形态）：
1. **等价性**——开关 off（默认）时装配走旧路径，字节与今天一致：
   不出现职责表/身份段，视图标记不参与；
2. **隔离**——开关 on 且本轮归某域时，非本域轮的块与轮号**一次都不出现**
   （否定断言），本域轮保留用户原文，本域块 ID 保留（expand_history 的锚）；
3. **冻结性**——别的域整理生效，本视图历史段字节不变；同一视图派生两次
   结果相同（纯函数，视图是可重建的派生物）；
4. **路由落点**——active_view 写在轮开启时刻（唯一切换点），持久化随轮。

纯函数 + 替身模型；不开网络。
"""
import json

import pytest

from wovra import registry as registry_module
from wovra import routing as routing_module
from wovra import views as views_module
from wovra.agent import Agent
from wovra.task import Task

from ._helpers import *  # noqa: F401,F403


def _domains() -> list[dict]:
    return [
        {"name": "工具层", "description": "安全层与工具实现",
         "file_domains": ["src/wovra/tools/"], "block_ids": []},
        {"name": "前端", "description": "纯 HTML 演示页",
         "file_domains": ["index.html"], "block_ids": []},
    ]


def _task_with_domains(rounds: list[dict]) -> Task:
    """带分裂产物的任务：R1 挂 domains（latest_domains 取最后一次生效的）。"""
    task = Task.create(goal="g")
    rounds = [dict(r) for r in rounds]
    rounds[0]["domains"] = _domains()
    task.rounds = rounds
    registry_module.merge_into(task.registry, _domains())
    return task


def test_route_hint_is_injected_into_main_agent_context(monkeypatch):
    """规则建议进主 agent 的装配（2026-09-12 用户口径：每轮主 agent 先触发）。

    路由不再直接落子域——它变成主 agent 上下文里的一条 [路由建议]，由主
    agent 用 route_to 把原话转出去（判断权在它，规则只给起点）。
    """
    monkeypatch.setenv(routing_module.ACTIVE_VIEW_ENV, "1")
    task = _task_with_domains([_mk_file_round(1, "写工具", ["src/wovra/tools/safety.py"])])
    agent = _agent(task)
    agent._open_or_reuse_round("改下 src/wovra/tools/safety.py 的判定")
    body = "\n".join(str(m.get("content") or "") for m in agent._assemble_messages())

    assert "[路由建议]" in body
    assert "工具层" in body and "文件命中" in body
    assert "判断权在你" in body            # 建议不是命令
    # 关掉开关：视图分化整条路径退场，建议也不该出现
    monkeypatch.setenv(routing_module.ACTIVE_VIEW_ENV, "0")
    off = "\n".join(str(m.get("content") or "") for m in agent._assemble_messages())
    assert "[路由建议]" not in off


def test_per_agent_runtime_accounting(monkeypatch):
    """3a：每个 agent 自己的轮次/步数/上下文体量与窗口（2026-09-12 用户口径）。

    口径：一轮算一个轮次、该轮全部步数，都记给轮闭合时 `active_view` 的那个
    agent（真正答话的那个）；转出记给转出方（主 agent 的参与度账）。
    """
    monkeypatch.setenv(routing_module.ACTIVE_VIEW_ENV, "1")
    task = _task_with_domains([_mk_file_round(1, "写工具", ["src/wovra/tools/safety.py"])])
    agent = _agent(task)
    agent._open_or_reuse_round("改下 src/wovra/tools/safety.py")
    agent.current_round["steps_used"] = 4
    agent._assemble_messages()                       # 记一次主 agent 的上下文
    main_entry = agent._registry_entry_for(routing_module.MAIN_AGENT_ID)
    assert main_entry["ctx_cur"] > 0 and main_entry["window"] == agent._org_watermark

    # 主 agent 照建议转出 → 转出记账给主 agent，轮次/步数记给接手方
    agent._pending_route = "工具层"
    agent._apply_pending_route()
    assert main_entry["handoffs"] == 1
    agent.close_round()

    tools_entry = agent._registry_entry_for("工具层")
    assert tools_entry["rounds"] == 1
    assert tools_entry["steps"] == 4
    assert main_entry["rounds"] == 0                 # 这一轮不是它答的
    stats = registry_module.runtime_stats(task.registry)
    assert stats["工具层"]["rounds"] == 1 and stats["工具层"]["window"] > 0
    assert abs(stats["Main"]["share"] - main_entry["ctx_cur"] / main_entry["window"]) < 1e-9


def _agent(task: Task) -> Agent:
    agent = Agent(llm=_StubLLM(), tools=[], task=task)
    return agent


def test_active_view_off_uses_legacy_assembly(monkeypatch):
    """等价性：开关显式关掉（`WOVRA_ACTIVE_VIEW=0`）时不进视图分支——无职责表、
    无身份段，字节与视图分化上线前一致（退路可用）。

    默认（不设环境变量）是**开**（2026-09-12 用户拍板「先追求效果」），
    故主 agent 轮也走视图装配——它的桶是补集，不再是全量。
    """
    monkeypatch.setenv(routing_module.ACTIVE_VIEW_ENV, "0")
    task = _task_with_domains([_mk_file_round(1, "写工具", ["src/wovra/tools/safety.py"])])
    agent = _agent(task)
    _make_open_round(agent, 2, "继续")
    off = agent._assemble_messages()
    body = "\n".join(str(m.get("content") or "") for m in off)
    assert "[全局职责表]" not in body
    assert "[当前身份]" not in body
    assert agent._active_view() == views_module.MAIN_AGENT_ID

    # 默认（开）且本轮归主 agent：拿的是**自己那份料**，不是全量
    monkeypatch.delenv(routing_module.ACTIVE_VIEW_ENV, raising=False)
    agent.current_round["active_view"] = views_module.MAIN_AGENT_ID
    on_msgs = agent._assemble_messages()
    on_body = "\n".join(str(m.get("content") or "") for m in on_msgs)
    assert "[当前身份]" in on_body
    assert on_msgs != off


def test_view_assembly_isolates_other_domain_rounds(monkeypatch):
    """隔离：非本域轮整轮不出现（块、轮号、块 ID 都不留）。

    注意职责表是**公共段**——它按设计含各域文件域（`index.html` 出现在
    职责表里是对的，否则路由就没有输入）。隔离针对的是**材料**：非本域
    的块描述、块 ID、轮的用户原文一次都不出现。
    """
    monkeypatch.setenv(routing_module.ACTIVE_VIEW_ENV, "1")
    task = _task_with_domains([
        _mk_file_round(1, "写工具层", ["src/wovra/tools/safety.py"]),
        _mk_file_round(2, "写演示页", ["index.html"]),
    ])
    task.rounds[1]["block_summaries"] = {"R2-B1": "演示页那块活的描述"}
    agent = _agent(task)
    _make_open_round(agent, 3, "继续")
    agent.current_round["active_view"] = "工具层"
    body = "\n".join(str(m.get("content") or "") for m in agent._assemble_messages())

    # 本域在场：轮头用户原文 + 本域块 ID（可 expand 取回原文）
    assert "写工具层" in body
    assert "R1-B" in body
    # 非本域材料彻底不在场
    assert "写演示页" not in body
    assert "演示页那块活的描述" not in body
    assert "R2-B" not in body
    assert "[R2]" not in body
    # 公共段（职责表）与身份段在场
    assert "[全局职责表]" in body
    assert "[当前身份]" in body


def test_view_assembly_history_is_frozen_when_other_domain_organized(monkeypatch):
    """冻结性：别的域整理生效，本视图消息序列字节不变。"""
    monkeypatch.setenv(routing_module.ACTIVE_VIEW_ENV, "1")
    rounds = [
        _mk_file_round(1, "写工具层", ["src/wovra/tools/safety.py"]),
        _mk_file_round(2, "写演示页", ["index.html"]),
    ]
    task = _task_with_domains(rounds)
    agent = _agent(task)
    _make_open_round(agent, 3, "继续")
    agent.current_round["active_view"] = "工具层"
    before = agent._assemble_messages()

    # 前端那批活被整理（别人的水位推进）：块描述与代次都变了。
    # 注意：Agent 构造时对 rounds 做**外层**浅拷贝——必须改 agent.rounds
    # 这一份，否则改的是 task 那份、视图读不到，冻结性会假通过。
    agent.rounds[1]["org_state"] = "done"
    agent.rounds[1]["org_generation"] = 9
    agent.rounds[1]["block_summaries"] = {"R2-B1": "index.html 的描述（不该进工具层视图）"}
    after = agent._assemble_messages()

    assert before == after
    assert "index.html 的描述" not in "\n".join(
        str(m.get("content") or "") for m in after
    )


def test_view_derivation_is_deterministic():
    """视图是可重建的派生物：同一份材料派生两次，字节完全相同。"""
    rounds = [_mk_file_round(1, "写工具", ["src/wovra/tools/safety.py"])]
    first = views_module.build_views(rounds, None, domains=_domains())
    second = views_module.build_views(rounds, None, domains=_domains())
    assert first["views"]["工具层"]["text"] == second["views"]["工具层"]["text"]
    assert first["views"]["工具层"]["own_ids"] == second["views"]["工具层"]["own_ids"]


def test_view_blocks_by_round_only_returns_hit_rounds():
    """按轮归堆只含命中轮；非命中轮不进返回（供装配"整轮不出现"）。"""
    rounds = [
        _mk_file_round(1, "写工具", ["src/wovra/tools/safety.py"]),
        _mk_file_round(2, "写演示页", ["index.html"]),
    ]
    hits = views_module.view_blocks_by_round(rounds, _domains(), "工具层")
    assert set(hits) == {1}
    assert hits[1]["own_ids"]


def test_view_history_is_append_only_across_rounds(monkeypatch):
    """前缀纯追加：同一视图连续两轮，前一轮的 [system+历史+身份] 段是
    后一轮同段的**逐字节前缀**——只有尾部（新命中轮 + 信封 + 当前轮）增长。

    这条是"冻结性"的时间维形态（另一条测的是"别人整理不动我"）：
    组合起来即"视图字节只在自身水位整理生效时才重写，其余纯追加"。
    """
    monkeypatch.setenv(routing_module.ACTIVE_VIEW_ENV, "1")
    task = _task_with_domains([_mk_file_round(1, "写工具层", ["src/wovra/tools/safety.py"])])
    agent = _agent(task)
    agent._open_or_reuse_round("继续改 src/wovra/tools/safety.py")
    agent.current_round["active_view"] = "工具层"
    first = agent._assemble_messages()[:3]  # system + 历史 + 身份（信封之前）

    agent.close_round()
    agent._open_or_reuse_round("再改 tools/shell.py")
    agent.current_round["active_view"] = "工具层"
    later = agent._assemble_messages()
    assert later[: len(first)] == first          # 纯追加：旧段逐字节不变
    assert len(later) > len(first)               # 新轮确实增加了内容


def test_route_writes_active_view_on_round_open(monkeypatch):
    """轮开启时**恒由主 agent 起手**，规则结果落成 `route_hint` 建议
    （2026-09-12 用户口径：每轮都是"主 agent 先触发 → 路由原话 → 子 agent 回"）。"""
    monkeypatch.setenv(routing_module.ACTIVE_VIEW_ENV, "1")
    task = _task_with_domains([_mk_file_round(1, "写工具", ["src/wovra/tools/safety.py"])])
    agent = _agent(task)
    agent._open_or_reuse_round("改下 src/wovra/tools/safety.py 的判定")
    assert agent.current_round["active_view"] == routing_module.MAIN_AGENT_ID
    assert agent.current_round["route_hint"]["view"] == "工具层"   # 建议（不是决定）
    # 主 agent 照建议转出去（真实流程）→ 本回合归工具层，下一轮才有粘滞可谈
    agent._pending_route = "工具层"
    agent._apply_pending_route()
    assert agent.current_round["active_view"] == "工具层"
    # 下一轮无命中 → 规则建议粘滞在本域，但起手仍是主 agent
    agent.close_round()
    agent._open_or_reuse_round("继续")
    assert agent.current_round["active_view"] == routing_module.MAIN_AGENT_ID
    assert agent.current_round["route_hint"]["view"] == "工具层"


def test_switch_view_sets_pending_and_next_round_takes_it(monkeypatch):
    """显式转交：switch_view 写 pending_view，下一轮路由最高优先采信。"""
    monkeypatch.setenv(routing_module.ACTIVE_VIEW_ENV, "1")
    task = _task_with_domains([_mk_file_round(1, "写工具", ["src/wovra/tools/safety.py"])])
    agent = _agent(task)
    out = agent.switch_view(agent="前端", reason="这活是页面的")
    assert "已转交" in out
    agent._open_or_reuse_round("随便一句话")
    assert agent.current_round["active_view"] == "前端"
    assert task.pending_view == ""  # 消费即清


def test_list_agents_renders_responsibility_table():
    """list_agents 是隔离后唯一的跨 agent 公共信息面。"""
    task = _task_with_domains([_mk_file_round(1, "写工具", ["src/wovra/tools/safety.py"])])
    agent = _agent(task)
    out = agent.list_agents()
    assert "[agent 职责表]" in out
    assert "工具层" in out and "src/wovra/tools/" in out
    assert "前端" in out


def test_tool_batch_helpers_are_json_serializable():
    """新工具的 schema 是纯 dict（注册路径不引入序列化差异）。"""
    from wovra.agent import _LIST_AGENTS_SCHEMA, _SWITCH_VIEW_SCHEMA

    for schema in (_LIST_AGENTS_SCHEMA, _SWITCH_VIEW_SCHEMA):
        assert json.dumps(schema, ensure_ascii=False)
        assert schema["function"]["name"] in ("list_agents", "switch_view")


def test_view_tiering_rewrites_only_on_own_watermark_advance(monkeypatch):
    """分档的冻结性（plan §5.1）：只有**本视图自己**水位推进才重写字节。

    ① 别的域整理（全局代次前进、别人材料变化）→ 本视图历史段逐字节不变；
    ② 本域自己再整理一代 → 折叠线前进，字节随之变化（这是该变的）。

    注意口径：分档基准是"本域命中轮的最大代次 − 2"，故**单轮域永不折叠**
    （它自己就是最高代次）——要观察折叠必须给本域两个不同代次的轮。
    """
    monkeypatch.setenv(routing_module.ACTIVE_VIEW_ENV, "1")
    rounds = [
        _mk_file_round(1, "写工具层", ["src/wovra/tools/safety.py"]),
        _mk_file_round(2, "再写工具层", ["src/wovra/tools/shell.py"]),
        _mk_file_round(3, "写演示页", ["index.html"]),
    ]
    task = _task_with_domains(rounds)
    # 工具层：R1 第 3 代、R2 第 5 代（基准 5 − 2 = 3 → R1 尚在全分辨率）；
    # 前端：R3 第 1 代（它的代次高低与工具层无关）
    task.rounds[0]["org_state"], task.rounds[0]["org_generation"] = "done", 3
    task.rounds[0]["block_summaries"] = {"R1-B1": "第 3 代描述"}
    task.rounds[1]["org_state"], task.rounds[1]["org_generation"] = "done", 5
    task.rounds[1]["block_summaries"] = {"R2-B1": "第 5 代描述"}
    task.rounds[2]["org_state"], task.rounds[2]["org_generation"] = "done", 1
    task.rounds[2]["block_summaries"] = {"R3-B1": "演示页第 1 代描述"}

    agent = _agent(task)
    _make_open_round(agent, 90, "继续")
    agent.current_round["active_view"] = "工具层"
    before = agent._assemble_messages()
    body_before = "\n".join(str(m.get("content") or "") for m in before)
    assert "第 3 代描述" in body_before            # 基准 3 → 未折叠

    # ① 别的域整理推进（全局代次前进、前端材料变化）——本域材料一字未动
    agent.rounds[2]["org_generation"] = 9
    agent.rounds[2]["block_summaries"] = {"R3-B1": "演示页第 9 代描述（不该进工具层）"}
    after_other = agent._assemble_messages()
    assert after_other == before
    assert "演示页第 9 代描述" not in "\n".join(
        str(m.get("content") or "") for m in after_other
    )

    # ② 本域自己再整理一代（R2 → 第 6 代）→ 基准前进（3 → 4），R1 落入折叠档
    # 注意：Agent 构造时对 rounds 做**外层**浅拷贝（内层 dict/列表共享），
    # 故改材料必须改 agent.rounds 这一份，改 task.rounds 视图看不到。
    agent.rounds[1]["org_generation"] = 6
    after_own = agent._assemble_messages()
    body_after = "\n".join(str(m.get("content") or "") for m in after_own)
    assert after_own != before
    assert "第 3 代描述" not in body_after         # 第 3 代已折叠
    assert "块细节已折叠" in body_after
    assert "第 5 代描述" in body_after             # 第 6 代那轮仍在全分辨率
    # 折叠不改归属：折叠轮的块 ID 依然保留（expand_history 的锚）
    assert "R1-B" in body_after


def test_view_assembly_repairs_checkpoint_split_tool_message(monkeypatch):
    """视图装配的协议补缝：上一轮以 tool_call 结尾（检查点轮边界）、
    当前轮以 tool 结果开头时，补上收尾 tool_call——tool 消息不得悬空
    （视图历史全是 user/assistant 文本对，悬空即被严格端点 400）。"""
    monkeypatch.setenv(routing_module.ACTIVE_VIEW_ENV, "1")
    call_msg = {"role": "assistant", "content": "",
                "tool_calls": [{"id": "c9", "type": "function",
                                "function": {"name": "todo", "arguments": "{}"}}]}
    r1 = {
        "seq": 1,
        "user_input": {"original": "写工具", "normalized": ""},
        "events": [
            {"id": "R1-E01", "type": "user", "status": "", "truncated": "写工具",
             "message": {"role": "user", "content": "写工具"}},
            {"id": "R1-E02", "type": "tool_call", "status": "",
             "truncated": "todo(verify)", "message": call_msg},
        ],
        "end_state": "completed", "org_state": "done",
    }
    task = _task_with_domains([r1])
    agent = _agent(task)
    agent.current_round = {
        "seq": 2, "user_input": {"original": "[运行时] 大步验收通过",
                                 "normalized": ""},
        "events": [], "refined_index": {}, "end_state": "open",
        "org_state": "", "active_view": "工具层",
    }
    agent.rounds.append(agent.current_round)
    agent.messages = []
    agent._record_event("tool_result", {"role": "tool", "tool_call_id": "c9",
                                        "content": "大步已验收"})

    msgs = agent._assemble_messages()

    for i, m in enumerate(msgs):
        if m.get("role") == "tool":
            prev = msgs[i - 1] if i else None
            assert prev is not None and prev.get("role") == "assistant" \
                and prev.get("tool_calls"), (
                f"位置 {i} 的 tool 消息悬空——严格端点会 400"
            )
