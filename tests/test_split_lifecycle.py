"""分裂生命周期与经济判据测试（步 3）。

钉住三件事（用户口径 / plan §13.2/§13.3）：
1. **经济判据是机械算式** `(B − B′) × N_future − C_split`，两侧同单位
   （token 等效输入）；`≤ 0 → 不拆`；
2. **发现职责 ≠ 创建 Agent**：注册表里没有这个域 = `split`（本批发现），
   已有 = `no split`（延续，重放幂等）；视图本就不由这里创建；
3. **status 是材料事实**：该域名下有命中轮 → `active`，否则 `dormant`——
   不是模型自述、也不是"拆了就 active"。
"""

import json

from wovra import economics as economics_module
from wovra import split_lifecycle as split_lifecycle_module


def test_split_benefit_formula_both_directions():
    """算式两侧同单位：省下的每轮量 × 未来轮数 − 一次性成本。"""
    worth = economics_module.split_benefit(100_000, 20_000, 5)
    # (100000 − 20000) × 5 − 18830 = 381170
    assert worth["saved_per_call"] == 80_000
    assert worth["benefit"] == 80_000 * 5 - economics_module.PREFIX_BREAK_TOKENS
    assert worth["split"] is True and worth["verdict"] == "值得拆"
    assert "18830" in worth["formula"].replace(",", "")

    cheap = economics_module.split_benefit(100_000, 99_000, 1)
    # (100000 − 99000) × 1 − 18830 < 0 → 不值得拆
    assert cheap["benefit"] < 0
    assert cheap["split"] is False
    assert cheap["verdict"].startswith("不拆")


def test_split_benefit_clamps_negative_inputs():
    """脏输入（B′ > B、负轮数）不产生负收益的假象——省下量恒 ≥0。"""
    out = economics_module.split_benefit(10, 100_000, -3)
    assert out["saved_per_call"] == 0
    assert out["n_future"] == 0
    assert out["benefit"] == -economics_module.PREFIX_BREAK_TOKENS


def test_assess_uses_max_view_as_conservative_lower_bound():
    """整体判定用**最大视图体量**当 B′（保守下界：连它都赚就一定赚）。"""
    views = [
        {"name": "大域", "tokens": 60_000, "rounds": 8},
        {"name": "小域", "tokens": 5_000, "rounds": 2},
    ]
    assessed = economics_module.assess(100_000, views)
    overall = assessed["overall"]
    assert overall["b_after"] == 60_000            # 不是最小的那个
    assert overall["n_future"] == 8                # 取最活跃域的轮数
    assert len(assessed["per_domain"]) == 2
    lines = economics_module.format_lines(assessed)
    assert lines and "分裂经济判据" in lines[0]
    # 明细按收益降序（先看最值得拆的）：大域省得多、轮次多 → 排前
    assert "大域" in lines[1]
    assert "小域" in lines[2]


def test_assess_from_watermarks_skips_main_agent_bucket():
    """主 agent 是兜底桶，不参与"要不要把它拆出来"。"""
    marks = {
        "Main": {"tokens": 90_000, "rounds": 30},
        "工具层": {"tokens": 12_000, "rounds": 6},
    }
    assessed = economics_module.assess_from_watermarks(100_000, marks)
    names = [d["name"] for d in assessed["per_domain"]]
    assert names == ["工具层"]
    assert assessed["overall"]["b_after"] == 12_000


def test_plan_separates_discovery_from_creation():
    """发现职责与创建 Agent 是两个动作：新域 = split，已有域 = no split。"""
    registry = [{"id": "A", "name": "主agent", "status": "active"},
                {"id": "A-1", "name": "工具层", "status": "dormant"}]
    marks = {"工具层": {"tokens": 12_000, "rounds": 6},
             "新前线": {"tokens": 30_000, "rounds": 4}}
    actions = split_lifecycle_module.plan(
        [{"name": "工具层"}, {"name": "新前线"}], registry, marks, b_before=100_000
    )
    by_name = {a["name"]: a for a in actions}
    assert by_name["工具层"]["action"] == split_lifecycle_module.NO_SPLIT
    assert by_name["工具层"]["id"] == "A-1"
    assert by_name["新前线"]["action"] == split_lifecycle_module.SPLIT
    assert by_name["新前线"]["id"] == ""          # 还没进注册表，没有 ID
    # 两域都有命中轮 → active（材料事实）
    assert by_name["工具层"]["status"] == split_lifecycle_module.ACTIVE


def test_plan_status_follows_material_fact_not_declaration():
    """没命中轮的域仍是 dormant——status 由材料事实决定，与拆不拆无关。"""
    actions = split_lifecycle_module.plan(
        [{"name": "刚发现域"}], [], {"刚发现域": {"tokens": 3_000, "rounds": 0}},
        b_before=100_000,
    )
    assert actions[0]["action"] == split_lifecycle_module.SPLIT
    assert actions[0]["status"] == split_lifecycle_module.DORMANT
    assert actions[0]["benefit"] < 0              # 没活跃轮 → 收益必为负


def test_plan_empty_domains_is_noop():
    """「无域」≠「不可分」：空产物不产生动作、不产生留痕。"""
    assert split_lifecycle_module.plan([], []) == []
    assert split_lifecycle_module.plan(None, None) == []
    assert split_lifecycle_module.summary_lines([]) == []


def test_apply_status_only_touches_status():
    """落 status 只动这一个字段（职责描述归 registry.merge_into）。"""
    registry = [{"id": "A-1", "name": "工具层", "status": "dormant",
                 "description": "别动我", "file_domains": ["tools/"]}]
    changed = split_lifecycle_module.apply_status(
        registry, [{"id": "A-1", "status": "active"}]
    )
    assert changed == ["A-1"]
    assert registry[0]["status"] == "active"
    assert registry[0]["description"] == "别动我"
    # 无变化不产生改动记录（反复 promote 不抖动）
    assert split_lifecycle_module.apply_status(
        registry, [{"id": "A-1", "status": "active"}]
    ) == []


def test_summary_lines_reports_counts_and_verdicts():
    actions = split_lifecycle_module.plan(
        [{"name": "甲"}, {"name": "乙"}], [{"id": "A-1", "name": "甲"}],
        {"甲": {"tokens": 60_000, "rounds": 10}, "乙": {"tokens": 5_000, "rounds": 1}},
        b_before=100_000,
    )
    lines = split_lifecycle_module.summary_lines(actions)
    assert "生命周期动作" in lines[0]
    assert "1 个 split" in lines[0] and "1 个 no split" in lines[0]
    joined = "\n".join(lines)
    assert "甲：no split／active" in joined
    assert "值得拆" in joined                      # 甲省得多、轮次多


def test_split_lifecycle_is_json_serializable():
    """落账本的动作包必须是可 JSON 序列化的纯数据（history 要落盘）。"""
    actions = split_lifecycle_module.plan(
        [{"name": "甲"}], [], {"甲": {"tokens": 10_000, "rounds": 3}},
        b_before=50_000,
    )
    json.dumps(actions, ensure_ascii=False)
