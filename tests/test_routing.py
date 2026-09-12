"""路由测试（Level 1 第三步：这一轮该由谁接）。

钉住五件事：
1. **判定顺序**——显式转交 > 文件命中（单域）> 粘滞 > 主 agent；
2. **多域命中交主 agent 转出**——一句话跨两摊活，由主 agent 挑关联最大的
   那个域（它有 route_to，比规则更懂"主要在谁那儿"；接手方还能再转）；
3. **开关默认开**——不设 WOVRA_ACTIVE_VIEW 时视图分化参与装配（显式给
   0/false/no/off 才退回单体全量装配，那是退路）；
4. **职责表是唯一公共信息面**——list_agents 渲染的职责表含 id/职责/文件域；
5. **误路由可纠正**——switch_view 写 pending_view，下一轮路由直接采信。

纯函数、零 LLM：不需要替身模型。
"""
import pytest

from wovra import routing as routing_module


def _registry() -> list[dict]:
    return [
        {"id": "A", "name": "主agent", "description": "全局协调", "file_domains": [],
         "status": "active", "inbox": []},
        {"id": "A-1", "name": "工具层", "description": "安全层与工具实现",
         "file_domains": ["src/wovra/tools/"], "status": "dormant", "inbox": []},
        {"id": "A-2", "name": "前端", "description": "演示页面",
         "file_domains": ["index.html", "js/"], "status": "dormant", "inbox": []},
    ]


def test_route_prefers_explicit_handoff():
    """显式转交是最高优先判据（人工/机制的直接意志）。"""
    out = routing_module.route(
        "随便一句话", _registry(), explicit="A-2",
    )
    assert out["view"] == "前端"
    assert "显式转交" in out["reason"]


def test_route_by_file_hit_single_domain():
    """单域文件命中：消息里出现该域文件路径/文件名即交给它。"""
    out = routing_module.route(
        "帮我看下 src/wovra/tools/safety.py 的越界判定", _registry()
    )
    assert out["view"] == "工具层"
    assert out["matched"] == ["工具层"]


def test_route_does_not_overmatch_extensionless_dir_names():
    """无扩展名的目录末段不参与命中——中文对话里太容易误伤。

    `src/wovra/tools/` 的末段是 `tools`，不取；故只说"工具这块"不会命中，
    交主 agent（有转交兜底）。
    """
    out = routing_module.route("工具这块要不要再收一收", _registry())
    assert out["view"] == routing_module.MAIN_AGENT_ID


def test_route_multi_hit_goes_to_main_agent():
    """一句话跨两摊活：交主 agent，由它挑**关联最大**的域用 route_to 转出。

    `file_hints` 是材料侧机械统计的"该域真实出现过的文件"——只提文件名
    的说法（safety.py）靠它命中，否则职责表里的目录写法定不了归属。
    """
    out = routing_module.route(
        "safety.py 的拦截要在 index.html 上给个提示",
        _registry(),
        file_hints={"工具层": ["src/wovra/tools/safety.py"]},
    )
    assert out["view"] == routing_module.MAIN_AGENT_ID
    assert set(out["matched"]) == {"工具层", "前端"}
    assert "多域命中" in out["reason"]
    assert "转出" in out["reason"]      # 交主 agent 是"让它转"，不是"让它做"


def test_route_by_bare_filename_with_hints():
    """只提文件名（不写路径）也能命中——职责表写目录时的必要补强。"""
    out = routing_module.route(
        "safety.py 加个白名单",
        _registry(),
        file_hints={"工具层": ["src/wovra/tools/safety.py"]},
    )
    assert out["view"] == "工具层"


def test_route_sticky_keeps_previous_view():
    """无命中时粘滞在上一轮视图（同一摊活连着干是常态）。"""
    out = routing_module.route("继续", _registry(), sticky="工具层")
    assert out["view"] == "工具层"
    assert "粘滞" in out["reason"]


def test_route_sticky_to_main_agent_is_not_reported_as_sticky():
    """上一轮是主 agent 时不算粘滞——兜底就是主 agent，不必美化口径。"""
    out = routing_module.route("继续", _registry(), sticky="Main")
    assert out["view"] == routing_module.MAIN_AGENT_ID
    assert "粘滞" not in out["reason"]


def test_active_view_switch_defaults_on(monkeypatch):
    """开关默认开（2026-09-12 用户拍板「先追求效果」）。

    只有显式给 0/false/no/off 才退回单体全量装配——那是退路，不是缺省。
    """
    monkeypatch.delenv(routing_module.ACTIVE_VIEW_ENV, raising=False)
    assert routing_module.active_view_enabled() is True
    for value in ("0", "false", "no", "off", "OFF", " false "):
        monkeypatch.setenv(routing_module.ACTIVE_VIEW_ENV, value)
        assert routing_module.active_view_enabled() is False, value
    monkeypatch.setenv(routing_module.ACTIVE_VIEW_ENV, "1")
    assert routing_module.active_view_enabled() is True


def test_route_without_registry_falls_back_to_main_agent():
    assert routing_module.route("干活", [])["view"] == routing_module.MAIN_AGENT_ID


def test_responsibility_lines_are_self_contained():
    """职责表必须自足（id、名字、职责、文件域、状态）——它是隔离后唯一的
    跨 agent 公共信息面，缺一项就等于路由缺输入。"""
    lines = routing_module.responsibility_lines(_registry())
    assert len(lines) == 3
    joined = "\n".join(lines)
    for token in ("A", "工具层", "安全层与工具实现", "src/wovra/tools/", "dormant"):
        assert token in joined


def test_identity_card_states_isolation_and_handoff():
    """身份段必须同时给出两件事：隔离是设计（不是缺失）+ 接错活怎么转。"""
    card = "\n".join(routing_module.identity_card("工具层", _registry()))
    assert "所有权文件域：src/wovra/tools/" in card
    assert "隔离纪律" in card
    assert "route_to" in card and "consult" in card
    assert "接活先验 ownership" in card


def test_identity_card_for_main_agent_is_router():
    """主 agent 的身份卡必须写清本职是路由（不是干活），且给出兜底判据。

    注册表里恒有主 agent 条目（v2 起 ID 是 `Main`）——身份卡必须先判主
    agent，否则它会拿到与子域同款的通用卡片，路由纪律永远下发不到它手上
    （机制在、纪律缺席）。
    """
    card = "\n".join(
        routing_module.identity_card(routing_module.MAIN_AGENT_ID, _registry())
    )
    assert "主agent" in card
    assert "路由器" in card and "route_to" in card
    assert "完全不需要读任何域的代码" in card
    assert "停手" in card


@pytest.mark.parametrize(
    "text,expect",
    [
        ("改 tools/safety.py", "工具层"),
        ("改 src\\wovra\\tools\\shell.py", "工具层"),   # 反斜杠路径也要认
        ("index.html 的样式", "前端"),
        ("js/app.js 报错", "前端"),
    ],
)
def test_match_domains_normalizes_backslashes(text, expect):
    assert routing_module.match_domains(text, _registry()) == [expect]
