"""回合内转交测试（route_to，2026-09-12 用户口径）。

用户口径：主 agent 的本职就是「收到消息 → 对职责表 → 把用户原话转给对应
agent」，由目标**在同一个用户回合内**接手干完；主 agent 基本说不了一句话。
误路由不是问题——接手方发现不是自己的活，自己转出去即可（便宜）。

钉住五件事：
1. **同回合生效**——主 agent 一转，目标立刻在同一回合的下一步接手干活，
   用户不需要等第二个回合；最终回答由目标产出。
2. **原话到场**——目标接手时看得到用户原话（不经过主 agent 转述）。
3. **批次后才换**——工具方法体只登记意图，`active_view` 在工具批次跑完
   才变（批次中途换装配会让同批后续工具看到错乱上下文）。
4. **跳数上限**——两个域互相踢皮球必须收口（烧完一轮步数还是没人干活）。
5. **自己/不存在的目标**都拒绝（不制造空转）。
6. **接手方上下文干净**——当前轮里主 agent 那次 `route_to` 调用**连它的回执**
   都不进接手方的上下文（R10 实测缺陷：接手方把回执当成本轮答复，只回一句
   "已转交 ▨▨"就闭合轮次，用户的问题要等下一轮才被回答）；代之以一条运行时
   转交说明（谁转来的、为什么）。
7. **链式转交**（主 agent → A → B，R10 现场的形状）：每一跳都重新装配、
   都重新调模型，最终回答必须来自**最后一手**——中间任何一手都不得把
   "我转出去了"当成本轮答复。
"""
import json

import pytest

from wovra import registry as registry_module
from wovra import routing as routing_module
from wovra import views as views_module
from wovra.agent import Agent
from wovra.agent.support import _MAX_ROUTE_HOPS
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
    task = Task.create(goal="g")
    rounds = [dict(r) for r in rounds]
    rounds[0]["domains"] = _domains()
    task.rounds = rounds
    registry_module.merge_into(task.registry, _domains())
    return task


def _route_call(agent: str, reason: str = "改了 tools 层", call_id: str = "c1"):
    return _chunk(_delta(tool_calls=[_fragment(
        0, id=call_id, name="route_to",
        arguments=json.dumps({"agent": agent, "reason": reason},
                             ensure_ascii=False),
    )]))


def test_route_to_switches_view_within_same_turn(monkeypatch):
    """主 agent 转交后，目标在**同一回合**的下一步接手并产出最终回答。"""
    monkeypatch.delenv(routing_module.ACTIVE_VIEW_ENV, raising=False)
    task = _task_with_domains([_mk_file_round(1, "写工具层", ["src/wovra/tools/safety.py"])])
    stub = _StubLLM([
        [_route_call("工具层")],            # 主 agent：转交
        [_chunk(_delta(content="工具层干完了"))],  # 目标：接手干活
    ])
    agent = Agent(llm=stub, tools=[], task=task)

    # 用户原话刻意不提文件名 → 文件命中判不出来，落到主 agent，由它路由
    answer = agent.run("把那块安全逻辑收一收")

    assert answer == "工具层干完了"
    last = agent.rounds[-1]
    assert last["active_view"] == "工具层"     # 本回合内换了视图
    assert last["route_hops"] == 1
    # 目标接手时看到的是**用户原话**（不经过主 agent 转述）
    second = stub.calls[-1]["messages"]
    body = "\n".join(str(m.get("content") or "") for m in second)
    assert "把那块安全逻辑收一收" in body
    # 且装配已切到工具层身份（不是主 agent）
    assert "[当前身份] A（工具层）" in body


def test_route_to_only_registers_intent_until_batch_finishes(monkeypatch):
    """工具方法体只登记意图：`active_view` 在批次跑完后才改。"""
    monkeypatch.delenv(routing_module.ACTIVE_VIEW_ENV, raising=False)
    task = _task_with_domains([_mk_file_round(1, "写工具层", ["src/wovra/tools/safety.py"])])
    agent = Agent(llm=_StubLLM(), tools=[], task=task)
    agent._open_or_reuse_round("随便说句")
    before = agent.current_round["active_view"]

    out = agent.route_to(agent="工具层", reason="这活是工具层的")

    assert "已转交" in out
    assert agent.current_round["active_view"] == before   # 还没换
    assert agent._pending_route == "工具层"                # 只登记了意图
    assert agent._apply_pending_route() is True            # 批次后才落地
    assert agent.current_round["active_view"] == "工具层"
    assert agent.current_round["route_hops"] == 1
    # 幂等：同一目标再落一次不重复计数
    agent._pending_route = "工具层"
    assert agent._apply_pending_route() is False
    assert agent.current_round["route_hops"] == 1


def test_route_to_refuses_past_hop_limit(monkeypatch):
    """跳数上限：踢皮球链必须收口（宁可交回用户，也不烧光一轮步数）。"""
    monkeypatch.delenv(routing_module.ACTIVE_VIEW_ENV, raising=False)
    task = _task_with_domains([_mk_file_round(1, "写工具层", ["src/wovra/tools/safety.py"])])
    agent = Agent(llm=_StubLLM(), tools=[], task=task)
    agent._open_or_reuse_round("这活归谁")
    agent.current_round["active_view"] = "前端"
    agent.current_round["route_hops"] = _MAX_ROUTE_HOPS

    out = agent.route_to(agent="工具层", reason="还是工具层吧")

    assert "不再转交" in out
    assert agent._pending_route == ""      # 拒绝登记
    assert agent.current_round["active_view"] == "前端"


def test_route_to_rejects_self_and_unknown_target(monkeypatch):
    """转给自己（主 agent）或职责表里没有的名字都不接受。"""
    monkeypatch.delenv(routing_module.ACTIVE_VIEW_ENV, raising=False)
    task = _task_with_domains([_mk_file_round(1, "写工具层", ["src/wovra/tools/safety.py"])])
    agent = Agent(llm=_StubLLM(), tools=[], task=task)
    agent._open_or_reuse_round("随便说句")

    assert "没有可转的对象" in agent.route_to(agent="主agent", reason="x")
    assert "没有可转的对象" in agent.route_to(agent="Main", reason="x")
    unknown = agent.route_to(agent="不存在的域", reason="x")
    assert "未找到 agent" in unknown and "工具层" in unknown
    assert agent._pending_route == ""


def test_handoff_target_does_not_see_router_steps(monkeypatch):
    """接手方看不到"路由器那一步"（R10 实测：复述回执就收工，问题没人答）。"""
    monkeypatch.delenv(routing_module.ACTIVE_VIEW_ENV, raising=False)
    task = _task_with_domains([_mk_file_round(1, "写工具层", ["src/wovra/tools/safety.py"])])
    stub = _StubLLM([
        [_chunk(_delta(content="我先转给工具层。", tool_calls=[_fragment(
            0, id="c1", name="route_to",
            arguments=json.dumps({"agent": "工具层", "reason": "安全层归它"},
                                 ensure_ascii=False),
        )]))],
        [_chunk(_delta(content="工具层干完了"))],   # 接手方真的干活
    ])
    agent = Agent(llm=stub, tools=[], task=task)

    assert agent.run("把那块安全逻辑收一收") == "工具层干完了"
    body = "\n".join(
        str(m.get("content") or "") for m in stub.calls[-1]["messages"]
    )
    # 该在的：用户原话、接手方身份、一条明确的转交说明
    assert "把那块安全逻辑收一收" in body
    assert "[当前身份] A（工具层）" in body
    assert "[回合内转交]" in body and "安全层归它" in body
    # 不该在的：路由调用、它的回执、以及主 agent 转交前的过渡话
    assert "就此停手" not in body
    assert "我先转给工具层" not in body
    assert not any(
        (c.get("function") or {}).get("name") == "route_to"
        for m in stub.calls[-1]["messages"]
        for c in (m.get("tool_calls") or [])
    )


def test_main_agent_still_sees_its_own_route_step(monkeypatch):
    """反向保险：剥离只对**接手方**生效，主 agent/其它路径的消息不被误删。"""
    monkeypatch.delenv(routing_module.ACTIVE_VIEW_ENV, raising=False)
    task = _task_with_domains([_mk_file_round(1, "写工具层", ["src/wovra/tools/safety.py"])])
    agent = Agent(llm=_StubLLM(), tools=[], task=task)
    agent._open_or_reuse_round("这活归谁")
    agent.route_to(agent="工具层", reason="安全层归它")
    assert agent.current_round["route_handoff"]["reason"] == "安全层归它"

    msgs = [{"role": "assistant", "content": "我先转一下。",
             "tool_calls": [{"id": "c1", "type": "function",
                             "function": {"name": "route_to", "arguments": "{}"}}]},
            {"role": "tool", "content": "已转交 工具层：就此停手。"},
            {"role": "user", "content": "用户原话"}]
    kept, handoff = agent._strip_router_steps(msgs)
    assert kept == [{"role": "user", "content": "用户原话"}]
    assert handoff["reason"] == "安全层归它" and handoff["to"] == "工具层"
    # 没有路由步骤时不动手（返回原样、无说明）
    plain = [{"role": "user", "content": "用户原话"},
             {"role": "tool", "content": "别的工具结果"}]
    kept2, handoff2 = agent._strip_router_steps(plain)
    assert kept2 == plain and handoff2 is None


def test_two_hop_handoff_answers_in_same_turn(monkeypatch):
    """链式转交（主 agent → 工具层 → 前端）：最后一手作答，中间手不闭合轮次。

    R10 现场就是这条形状：接手方判定"这不是我的活"，再转出去。链式转交
    必须每跳都换视图、重新调模型；跳数如实累加到 2；最终回答只认最后一手。
    """
    monkeypatch.delenv(routing_module.ACTIVE_VIEW_ENV, raising=False)
    task = _task_with_domains([_mk_file_round(1, "写工具层", ["src/wovra/tools/safety.py"])])
    stub = _StubLLM([
        [_route_call("工具层", "安全层归它")],                    # 主 agent：第一跳
        [_route_call("前端", "其实要动页面", call_id="c2")],      # 工具层：转出去
        [_chunk(_delta(content="前端干完了"))],                  # 前端：作答
    ])
    agent = Agent(llm=stub, tools=[], task=task)

    assert agent.run("把那块逻辑收一收") == "前端干完了"
    last = agent.rounds[-1]
    assert last["active_view"] == "前端"
    assert last["route_hops"] == 2
    # 最后一手的转交说明记的是**上一手**（工具层），不是最初的主 agent
    assert last["route_handoff"]["from"] == "工具层"
    body = "\n".join(
        str(m.get("content") or "") for m in stub.calls[-1]["messages"]
    )
    assert "（前端）" in body                     # 装配已到最后一手
    assert "[回合内转交]" in body and "其实要动页面" in body
    # 两跳的路由步骤（调用 + 回执）都不进最后一手的上下文
    assert "就此停手" not in body
    assert not any(
        (c.get("function") or {}).get("name") == "route_to"
        for m in stub.calls[-1]["messages"]
        for c in (m.get("tool_calls") or [])
    )


def test_route_to_schema_is_registered_in_managed_mode():
    """常驻工具（tools 数组恒定纪律）：managed 下 route_to 与 schema 必在。"""
    from wovra.agent import _ROUTE_TO_SCHEMA

    agent = Agent(llm=_StubLLM(), tools=[])
    assert "route_to" in agent.tools
    names = [
        (s.get("function") or {}).get("name")
        for s in agent._schemas
    ]
    assert "route_to" in names
    assert _ROUTE_TO_SCHEMA["function"]["name"] == "route_to"
    assert _ROUTE_TO_SCHEMA["function"]["parameters"]["required"] == ["agent", "reason"]
