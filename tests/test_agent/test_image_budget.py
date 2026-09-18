"""看图预算（2026-09-16，GAIA FINDINGS §2）：软线提示 + 硬线拒绝，防视觉题空转。

被测口径：`support.image_view_soft/hard` 两道线（调用期读环境），计数落在 round 的
`image_views` 上（与 route_hops 同款持久化）。测试改环境变量即可。
"""

from ._helpers import _agent_with


def _agent(*, soft, hard, monkeypatch):
    monkeypatch.setenv("WOVRA_IMAGE_VIEWS", str(soft))
    monkeypatch.setenv("WOVRA_IMAGE_VIEWS_MAX", str(hard))
    seen = {"views": 0, "shots": 0}

    def view_image(path: str, note: str = "") -> str:
        """看一张图（测试替身）。"""
        seen["views"] += 1
        return f"已注入图片: {path}"

    def screenshot(target: str, width: int = 1280, height: int = 800) -> str:
        """截图（测试替身）。"""
        seen["shots"] += 1
        return "已截图: shot.png"

    agent = _agent_with([view_image, screenshot])
    agent.current_round = {"image_views": 0}
    return agent, seen


def test_image_budget_soft_note_then_hard_refusal(monkeypatch):
    agent, seen = _agent(soft=2, hard=4, monkeypatch=monkeypatch)

    first = agent._invoke_tool("view_image", '{"path": "a.png"}')
    assert "已注入图片" in first and "[看图预算]" not in first
    # 恰好到软线：附一次收敛提示（结果本身照给）
    second = agent._invoke_tool("view_image", '{"path": "a.png"}')
    assert "[看图预算]" in second and "已注入图片" in second
    # 软线与硬线之间照常看图，但不重复提示（白烧 token）
    third = agent._invoke_tool("view_image", '{"path": "a.png"}')
    fourth = agent._invoke_tool("view_image", '{"path": "a.png"}')
    assert "[看图预算]" not in third and "[看图预算]" not in fourth
    assert seen["views"] == 4 and agent.current_round["image_views"] == 4

    # 到硬线：**不执行**——工具没被调用，也没有新图注入
    refused = agent._invoke_tool("view_image", '{"path": "a.png"}')
    assert "[看图预算用尽]" in refused and "未执行" in refused
    assert seen["views"] == 4
    assert agent.current_round["image_views"] == 4


def test_image_budget_counts_only_vision_tools(monkeypatch):
    """screenshot 只落盘、不注入图像，不进预算（挡它挡不住看图循环）。"""
    agent, seen = _agent(soft=1, hard=2, monkeypatch=monkeypatch)
    for _ in range(5):
        agent._invoke_tool("screenshot", '{"target": "https://example.com"}')
    assert agent.current_round["image_views"] == 0
    assert seen["shots"] == 5


def test_image_budget_without_round_does_not_crash(monkeypatch):
    """没有 round 时（脚本/测试直调）不崩、也不记账：计数只活在 round 上。

    没有 round 就没有累计，硬线自然不会触发——这是刻意的：预算是**回合**的
    概念，工作模式下 round 恒存在（脚本直调工具不该被一个看不见的计数器拒绝）。
    """
    agent, seen = _agent(soft=6, hard=12, monkeypatch=monkeypatch)
    agent.current_round = None
    for _ in range(20):
        out = agent._invoke_tool("view_image", '{"path": "a.png"}')
    assert "已注入图片" in out and "[看图预算用尽]" not in out
    assert seen["views"] == 20
    assert agent._image_view_count() == 0
    assert agent._count_image_view("x") == "x"        # 无 round 时也不写坏状态


def test_image_budget_resets_with_new_round(monkeypatch):
    """预算是**每回合**的（同 steps_used/route_hops）：新回合重新给额度。"""
    agent, _ = _agent(soft=1, hard=1, monkeypatch=monkeypatch)
    agent._invoke_tool("view_image", '{"path": "a.png"}')
    assert "[看图预算用尽]" in agent._invoke_tool("view_image", '{"path": "a.png"}')
    agent.current_round = {"image_views": 0}          # 新回合
    assert "已注入图片" in agent._invoke_tool("view_image", '{"path": "a.png"}')
