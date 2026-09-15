"""渐近归属测试（2026-09-12 用户口径）。

背景：整理/分裂在后台跑的那段时间里，用户可能又发了几轮输入；它们到达时
活跃域树还是空的（或旧的），路由只能判给主 agent。产物一生效，就必须按
同一套路由逻辑**补判**归位——只把上下文归过去，不重做活，归完再处理新一轮。

钉住六件事：
1. **补判零 LLM**——替身模型响应池为空，任何模型调用都会抛错；补判必须
   一次都不调用（纯函数、只写标记）；
2. **维护窗口轮归位**——产物生效前判主 agent 的轮，生效后按文件命中补判；
3. **顺序**——先补判、再判本轮：新一轮的粘滞必须接上补判结果；
4. **保底块跟轮走**——归域轮的纯聊天块随轮进该域视图（只归位）；
5. **环境块恒归主 agent**（R68 口径不变）；
6. **不丢块**——归属完整性仍成立（每块至少一个归宿）。
"""
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


def _chat_round(seq: int, user_text: str) -> dict:
    """纯聊天轮（无文件、无环境交互）→ v3 产出单个保底块。"""
    return {
        "seq": seq,
        "user_input": {"original": user_text, "normalized": ""},
        "events": [
            {"id": f"R{seq}-E01", "type": "user",
             "message": {"role": "user", "content": user_text}},
            {"id": f"R{seq}-E02", "type": "final_answer",
             "message": {"role": "assistant", "content": "好的"}},
        ],
        "end_state": "completed",
    }


def test_ownership_rules_for_block_kinds():
    """归属四条判据：环境块恒主 agent / 文件块按域 / 保底块跟轮 / 其余主 agent。"""
    domains = _domains()
    index = {
        "R1-B1": {"seq": 1, "round": {"seq": 1},
                  "block": {"kind": "environment", "file": ""}, "summary": ""},
        "R1-B2": {"seq": 1, "round": {"seq": 1},
                  "block": {"kind": "file", "file": "src/wovra/tools/safety.py"},
                  "summary": ""},
        "R1-B3": {"seq": 1, "round": {"seq": 1, "active_view": "工具层"},
                  "block": {"kind": "fallback", "file": ""}, "summary": ""},
        "R1-B4": {"seq": 1, "round": {"seq": 1},
                  "block": {"kind": "user", "file": ""}, "summary": ""},
        "R1-B5": {"seq": 1, "round": {"seq": 1},
                  "block": {"kind": "file", "file": "notes.md"}, "summary": ""},
    }
    owners = views_module.ownership(domains, index)
    assert owners["R1-B1"] == registry_module.MAIN_AGENT_ID   # 环境块恒归主 agent
    assert owners["R1-B2"] == "工具层"        # 文件命中
    assert owners["R1-B3"] == "工具层"        # 保底块跟本轮 active_view 走
    assert owners["R1-B4"] == registry_module.MAIN_AGENT_ID   # 用户块：随命中轮渲染（归属仍主 agent）
    assert owners["R1-B5"] == registry_module.MAIN_AGENT_ID   # 不属任何域的文件 → 残留桶


def test_settle_views_reattributes_maintenance_window_rounds(monkeypatch):
    """维护窗口内到达的轮：产物生效后按文件命中补判归位，零 LLM 调用。

    场景（用户口径的场景）：后台跑整理/分裂期间用户又发了 R2、R3——那时还
    没有域树，一律判给了主 agent；产物一生效，按同一套逻辑补判归位。

    （夹具注意：产物必须在构造 Agent **之前**挂到轮上——`Agent.__init__`
    浅拷贝 task.rounds，构造后新增的键 agent 手上那份看不到。真实 promote
    写在同一批内层 dict 上，故无此问题。）
    """
    monkeypatch.setenv(routing_module.ACTIVE_VIEW_ENV, "1")
    stub = _StubLLM()  # 响应池为空：任何模型调用都会 IndexError
    task = Task.create(goal="g")
    task.rounds = [
        _mk_file_round(1, "写 src/wovra/tools/safety.py", ["src/wovra/tools/safety.py"]),
        _chat_round(2, "再改改 tools/shell.py 的拦截"),
        _chat_round(3, "顺手把 index.html 的字号调大"),
    ]
    task.rounds[0]["domains"] = _domains()      # 产物已生效（落到轮上）
    registry_module.merge_into(task.registry, _domains())
    for r in task.rounds:
        r["active_view"] = registry_module.MAIN_AGENT_ID   # 但当时（旧注册表下）都判主 agent
    agent = Agent(llm=stub, tools=[], task=task)
    agent.current_round = None

    settled = agent._settle_views()

    assert settled == 3                                  # R1、R2、R3 全部按同一逻辑补判
    assert task.rounds[0]["active_view"] == "工具层"      # 文件块命中
    assert task.rounds[1]["active_view"] == "工具层"      # 只提 tools/（目录尾段带斜杠）
    assert task.rounds[2]["active_view"] == "前端"
    assert stub.calls == []                              # 零 LLM：补判是纯函数
    assert any("渐近归属" in e["detail"] for e in task.history)


def test_settle_views_does_not_overturn_settled_rounds(monkeypatch):
    """已归域的轮不被反复推翻（归属只随新产物前进，避免抖动）。"""
    monkeypatch.setenv(routing_module.ACTIVE_VIEW_ENV, "1")
    task = Task.create(goal="g")
    task.rounds = [_chat_round(1, "index.html 用蓝色")]
    task.rounds[0]["active_view"] = "前端"
    task.rounds[0]["domains"] = _domains()
    registry_module.merge_into(task.registry, _domains())
    agent = Agent(llm=_StubLLM(), tools=[], task=task)
    agent.current_round = None
    assert agent._settle_views() == 0
    assert task.rounds[0]["active_view"] == "前端"


def test_settle_and_route_orders_settle_before_current_round(monkeypatch):
    """顺序：先补判、再判本轮——新一轮粘滞接上补判结果。

    R1 归工具层、R2（维护窗口内到达，原先判主 agent）补判归工具层；接着
    开 R3 说一句无文件命中、无显式转交的话——它必须粘到补判后的「工具层」，
    而不是粘到 R2 原来的「主 agent」。
    """
    monkeypatch.setenv(routing_module.ACTIVE_VIEW_ENV, "1")
    task = Task.create(goal="g")
    task.rounds = [
        _mk_file_round(1, "写 src/wovra/tools/safety.py", ["src/wovra/tools/safety.py"]),
        _chat_round(2, "继续弄 tools/shell.py"),
    ]
    task.rounds[0]["domains"] = _domains()
    registry_module.merge_into(task.registry, _domains())
    task.rounds[1]["active_view"] = registry_module.MAIN_AGENT_ID   # 产物生效前判的主 agent
    agent = Agent(llm=_StubLLM(), tools=[], task=task)

    agent._open_or_reuse_round("再收一收顺序")   # 新轮（暂判）
    agent._settle_and_route("再收一收顺序")      # 先补判 R2、再判本轮

    assert task.rounds[1]["active_view"] == "工具层"     # 补判（材料归位）
    # 2026-09-12 改版：**每轮恒由主 agent 起手**——规则结果只当建议给它，
    # 由主 agent 用 route_to 把原话转出去。
    assert agent.current_round["active_view"] == registry_module.MAIN_AGENT_ID
    assert agent.current_round["route_hint"]["view"] == "工具层"


def test_settle_is_noop_when_switch_off(monkeypatch):
    """开关**显式关掉**时补判不做任何事（退路可用：装配退回单体全量）。"""
    monkeypatch.setenv(routing_module.ACTIVE_VIEW_ENV, "0")
    task = Task.create(goal="g")
    task.rounds = [_chat_round(1, "index.html 改一下")]
    task.rounds[0]["domains"] = _domains()
    registry_module.merge_into(task.registry, _domains())
    agent = Agent(llm=_StubLLM(), tools=[], task=task)
    agent.current_round = None
    assert agent._settle_views() == 0
    assert not task.rounds[0].get("active_view")


def test_settle_without_domains_does_nothing(monkeypatch):
    """还没有分裂产物时补判不做（无机可用，不允许瞎猜）。"""
    monkeypatch.setenv(routing_module.ACTIVE_VIEW_ENV, "1")
    task = Task.create(goal="g")
    task.rounds = [_chat_round(1, "随便说句")]
    agent = Agent(llm=_StubLLM(), tools=[], task=task)
    agent.current_round = None
    assert agent._settle_views() == 0


def test_settled_round_chat_block_follows_view_and_nothing_lost(monkeypatch):
    """归域轮的保底块随轮进该域视图；完整性仍成立（不丢块）。"""
    monkeypatch.setenv(routing_module.ACTIVE_VIEW_ENV, "1")
    rounds = [
        _mk_file_round(1, "写 src/wovra/tools/safety.py", ["src/wovra/tools/safety.py"]),
        _chat_round(2, "顺手聊两句 tools 的事"),
    ]
    rounds[1]["active_view"] = "工具层"
    built = views_module.build_views(rounds, None, domains=_domains())
    chat_ids = [
        bid for bid, item in built["index"].items()
        if item["block"].get("kind") == "fallback"
    ]
    assert chat_ids, "纯聊天轮应产出保底块"
    for bid in chat_ids:
        assert built["owners"][bid] == "工具层"
        assert bid in built["views"]["工具层"]["own_ids"]
    assert built["completeness"]["ok"] is True


def _async_product_fixture(monkeypatch):
    """一次异步维护：产物已就绪（pending_org，已落盘），尚未落地。

    用真实水位路径（`_maybe_organize_batch` 同步执行 org+split）产出暂存物，
    故下游 promote 面对的是真实形状的产物。
    """
    task = Task.create(goal="g")
    task.rounds = [_mk_file_round(1, "看下 index.html 演示页", ["index.html"])]
    task.rounds[0]["org_state"] = ""
    agent = Agent(
        llm=_StubLLM([[_chunk(_delta(content=_org_json()))]],
                     split_responses=[[_domains_chunk()]]),
        tools=[], task=task, org_watermark=0, org_grace_rounds=0,
        org_cooldown_rounds=0,
    )
    agent.last_context_estimate = 5000
    agent._maybe_organize_batch()
    return agent, task


def test_product_never_lands_into_another_instance_running_round(monkeypatch):
    """§50 跨实例补丁（serve 每轮新建 agent）：轮边界纪律按**盘上**判。

    维护线程（旧实例）看不见下一轮（新实例）——它的 `current_round` 恒为
    None。若此时照旧 promote，运行中的那一轮下一次落盘（工具卡）会 rebase
    合并产物，`latest_domains` 从空变非空 → 装配在**轮中间**从全量切到视图
    路径：前缀断裂、前后步上下文不一致。故盘上有开放轮 → 不落 live，
    留痕说明推迟，等轮闭合的边界再生效。

    同时钉住"空闲直接做"（2026-09-14 用户口径）：判定不白等——维护线程
    当场把已闭合轮的归属判好、记进预备区（`pending_view`），边界只做生效。
    """
    agent, task = _async_product_fixture(monkeypatch)
    assert task.rounds[0]["pending_org"]["domains"]      # 产物暂存、已落盘

    # 另一个实例在跑下一轮（serve）
    other = Task.load(task.id)
    runner = Agent(llm=_StubLLM(), tools=[], task=other)
    runner._open_or_reuse_round("接着聊")
    runner._persist_rounds()
    assert other.rounds[-1]["end_state"] == "open"

    # 维护线程跑完的结算时刻：盘上有开放轮 → 不落 live，但归属判定已预备
    agent._settle_after_maintenance()
    assert [str(e["id"]) for e in task.registry] == ["Main"]
    assert any("归属预判" in str(e.get("detail")) for e in task.history)
    fresh = Task.load(task.id)
    assert not any(r.get("domains") for r in fresh.rounds)
    assert fresh.rounds[0].get("pending_org")            # 产物还在暂存区
    assert fresh.rounds[0]["pending_view"]["view"] == "web 演示"   # 判定已就绪
    assert fresh.rounds[0].get("active_view") in (None, "", "Main")  # 但未生效

    # 运行中的轮中途落盘：产物/预备值不得影响它的装配路径
    runner._persist_rounds()
    assert not any(r.get("domains") for r in runner.rounds)
    assert runner._assemble_view_messages("Main", runner.rounds) is None

    # 轮闭合 → 边界生效：预备值落地 + 域树落地 + 注册表长出子 agent
    runner.close_round()
    fresh = Task.load(task.id)
    assert [str(e["id"]) for e in fresh.registry] == ["Main", "A"]
    assert fresh.rounds[0]["domains"]
    assert fresh.rounds[0]["active_view"] == "web 演示"   # 预备归属生效
    assert "pending_view" not in fresh.rounds[0]
    assert "pending_org" not in fresh.rounds[0]


def test_next_round_applies_pending_stash_before_working(monkeypatch):
    """R6 开场：先把已判好的归属落地（预备区生效），再判本轮。

    用户口径（2026-09-14）：「如果 R6 开场，R5～R6 还没判断好归属，就先等其
    判断完，再进行 R6 的流程」。构造"产物已落地 + 预备值留在轮上"的形态
    （维护线程判完先记、另一实例 promote 过）：本轮开场时 `pending_org` 已
    清空（promote 无事可做），但预备值必须在开轮时被应用——本轮的路由粘滞
    才能接上它。
    """
    monkeypatch.setenv(routing_module.ACTIVE_VIEW_ENV, "1")
    task = Task.create(goal="g")
    task.rounds = [_chat_round(1, "看下 index.html 的配色")]
    domains = _domains()
    task.rounds[0]["domains"] = domains
    registry_module.merge_into(task.registry, domains)
    task.rounds[0]["pending_view"] = {
        "view": "前端", "reason": "文件命中：index.html",
        "domains": registry_module.domains_digest(domains),
    }
    agent = Agent(llm=_StubLLM(), tools=[], task=task)

    agent._open_or_reuse_round("接着聊")          # R2 开场
    agent._promote_org_results()                  # 无暂存产物，但预备值要落地
    assert task.rounds[0]["active_view"] == "前端"
    assert "pending_view" not in task.rounds[0]

    agent._settle_and_route("接着聊")             # 先补判、再判本轮
    assert agent.current_round["active_view"] == registry_module.MAIN_AGENT_ID
    assert agent.current_round["route_hint"]["view"] == "前端"   # 粘滞接上已落地归属


def test_staged_view_dropped_when_product_does_not_match(monkeypatch):
    """预备值只在"所依据的产物 == 现在落地的产物"时採用。

    产物被拒收/换了批次时，旧判定不许写进 `active_view`——丢弃后由
    `_settle_views` 按现行材料重算（预备是加速器，不是真相来源）。
    """
    monkeypatch.setenv(routing_module.ACTIVE_VIEW_ENV, "1")
    task = Task.create(goal="g")
    task.rounds = [_chat_round(1, "看下 index.html")]
    task.rounds[0]["domains"] = _domains()
    task.rounds[0]["pending_view"] = {
        "view": "前端", "reason": "预判", "domains": "deadbeef",
    }
    agent = Agent(llm=_StubLLM(), tools=[], task=task)
    agent.current_round = None

    assert agent._apply_staged_views() == 0
    # 断言看 agent 手上那份：`Agent.__init__` 对轮做浅拷贝（外层 dict 是副本）
    assert agent.rounds[0].get("active_view") in (None, "", "Main")
    assert "pending_view" not in agent.rounds[0]

