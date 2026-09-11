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


def _agent(task: Task) -> Agent:
    agent = Agent(llm=_StubLLM(), tools=[], task=task)
    return agent


def test_active_view_off_uses_legacy_assembly(monkeypatch):
    """等价性：开关 off（默认）时不进视图分支——无职责表、无身份段。

    且开关 on、但本轮 active_view 为主 agent 时，产物也与 off 时**逐字节
    相同**（"active_view 缺省 = 主 agent = 今天的装配"）。
    """
    monkeypatch.delenv(routing_module.ACTIVE_VIEW_ENV, raising=False)
    task = _task_with_domains([_mk_file_round(1, "写工具", ["src/wovra/tools/safety.py"])])
    agent = _agent(task)
    _make_open_round(agent, 2, "继续")
    off = agent._assemble_messages()
    body = "\n".join(str(m.get("content") or "") for m in off)
    assert "[全局职责表]" not in body
    assert "[当前身份]" not in body
    assert agent._active_view() == views_module.MAIN_AGENT_ID

    monkeypatch.setenv(routing_module.ACTIVE_VIEW_ENV, "1")
    agent.current_round["active_view"] = views_module.MAIN_AGENT_ID
    assert agent._assemble_messages() == off


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
    # agent.rounds 是 task.rounds 的浅拷贝，内层 dict 共享——改材料即改视图输入。
    task.rounds[1]["org_state"] = "done"
    task.rounds[1]["org_generation"] = 9
    task.rounds[1]["block_summaries"] = {"R2-B1": "index.html 的描述（不该进工具层视图）"}
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
    """路由在轮开启时刻落一次（唯一切换点），随轮持久化。"""
    monkeypatch.setenv(routing_module.ACTIVE_VIEW_ENV, "1")
    task = _task_with_domains([_mk_file_round(1, "写工具", ["src/wovra/tools/safety.py"])])
    agent = _agent(task)
    agent._open_or_reuse_round("改下 src/wovra/tools/safety.py 的判定")
    assert agent.current_round["active_view"] == "工具层"
    # 下一轮无命中 → 粘滞在本域
    agent.close_round()
    agent._open_or_reuse_round("继续")
    assert agent.current_round["active_view"] == "工具层"


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
