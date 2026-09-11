"""注册表构建测试（Level 1 视图分化第一步）。

领域树 → 注册表条目是机械翻译：语义由分裂分析（LLM）产出，编号与
层级由 Runtime 机械派生（语义归模型、体量归机制）。这些用例钉住
三件事：路径 ID 的层级生成、幂等、脏数据兜底。
"""

import json

from wovra import registry as registry_module


def test_build_entries_generates_path_ids_from_tree():
    """顶层域 → A-1/A-2；子域 → A-1-1（路径 ID 是职责路径，非管理树）。"""
    domains = [
        {"name": "工具层", "description": "路径与命令边界",
         "file_domains": ["src/wovra/tools/"], "goal": "加固"},
        {"name": "探针", "description": "安全仪器",
         "parent": "工具层", "file_domains": ["experiments/"]},
        {"name": "前端", "description": "演示页",
         "file_domains": ["index.html"]},
    ]
    entries = registry_module.build_entries(domains)

    ids = [e["id"] for e in entries]
    assert ids == ["A-1", "A-1-1", "A-2"]
    top = entries[0]
    assert top["name"] == "工具层"
    assert top["file_domains"] == ["src/wovra/tools/"]
    assert top["goal"] == "加固"
    assert top["status"] == "dormant"          # 休眠是默认态（零成本）
    assert top["inbox"] == []
    child = entries[1]
    assert child["name"] == "探针"
    assert child["file_domains"] == ["experiments/"]


def test_build_entries_empty_and_dirty_parent():
    """空产物 → 无条目；parent 指向不存在的域 → 按顶层处理（不吞子树）。"""
    assert registry_module.build_entries([]) == []
    assert registry_module.build_entries(None) == []
    entries = registry_module.build_entries([
        {"name": "孤儿", "parent": "不存在的父"},
        {"name": "正常", "description": "d"},
    ])
    assert [e["id"] for e in entries] == ["A-1", "A-2"]   # 两个顶层，不丢


def test_build_entries_breaks_cycles():
    """脏数据成环（A 的父是 B、B 的父是 A）不递归到死：visited 截断。"""
    entries = registry_module.build_entries([
        {"name": "甲", "parent": "乙"},
        {"name": "乙", "parent": "甲"},
    ])
    assert len(entries) == 2                      # 各出现一次，不无限展开
    assert {e["name"] for e in entries} == {"甲", "乙"}


def test_merge_into_is_idempotent_and_preserves_runtime_state():
    """重复 promote 只更新描述，不重复追加；status/inbox 不被抹掉。"""
    registry = [{"id": "A", "name": "主agent", "description": "全局协调",
                 "file_domains": [], "status": "active", "inbox": []}]
    domains = [{"name": "工具层", "description": "旧描述",
                "file_domains": ["tools/"]}]

    added, updated = registry_module.merge_into(registry, domains)
    assert added == ["A-1"] and updated == []
    assert len(registry) == 2

    # 运行时状态（路由激活后的 status 与收件箱）必须活过重放
    registry[1]["status"] = "active"
    registry[1]["inbox"].append({"from": "主agent", "message": "接口定了"})

    added2, updated2 = registry_module.merge_into(registry, domains)
    assert added2 == [] and updated2 == []        # 一字未变即无更新
    assert len(registry) == 2                     # 不重复追加
    assert registry[1]["status"] == "active"      # 状态保留
    assert registry[1]["inbox"][0]["message"] == "接口定了"

    # 现状变了（描述/文件域）→ 更新既有条目，仍是两条
    domains[0]["description"] = "新描述"
    domains[0]["file_domains"] = ["tools/", "blocks/"]
    added3, updated3 = registry_module.merge_into(registry, domains)
    assert added3 == [] and updated3 == ["A-1"]
    assert len(registry) == 2
    assert registry[1]["description"] == "新描述"
    assert registry[1]["file_domains"] == ["tools/", "blocks/"]


def test_merge_into_handles_missing_registry_and_empty_domains():
    """registry 为 None（旧任务）时能就地建表；空域不产生条目。"""
    registry: list = []
    added, updated = registry_module.merge_into(registry, [{"name": "单域"}])
    assert added == ["A-1"] and updated == []
    assert registry_module.merge_into(registry, []) == ([], [])


def test_latest_domains_takes_newest_not_union():
    """取最近一次已生效产物，不做并集——并集会把已消失的域复活成僵尸。"""
    rounds = [
        {"seq": 1, "domains": [{"name": "旧域"}]},
        {"seq": 2, "domains": []},                      # 空产物不覆盖
        {"seq": 3, "domains": [{"name": "新域"}]},
        {"seq": 4},                                     # 无产物不覆盖
    ]
    assert registry_module.latest_domains(rounds) == [{"name": "新域"}]
    assert registry_module.latest_domains([]) == []
    assert registry_module.latest_domains(None) == []


def test_backfill_materializes_history_once():
    """加载期回填：历史已生效的分裂产物补进注册表（幂等）。

    机制落地前 promote 过的产物不会再触发 promote——不补则历史会话
    的注册表永远只有主 agent（同 v1→v3 块迁移的"历史数据没被新机制
    覆盖"）。
    """
    rounds = [
        {"seq": 1, "domains": [{"name": "旧域", "description": "已过时"}]},
        {"seq": 2, "domains": [
            {"name": "工具层", "description": "路径与命令边界",
             "file_domains": ["src/wovra/tools/"]},
            {"name": "探针", "parent": "工具层"},
        ]},
    ]
    registry = [{"id": "A", "name": "主agent", "description": "全局协调",
                 "file_domains": [], "status": "active", "inbox": []}]

    added, updated = registry_module.backfill(registry, rounds)
    assert added == ["A-1", "A-1-1"] and updated == []
    assert "旧域" not in [e["name"] for e in registry]   # 旧批次被取代
    assert registry[-1]["name"] == "探针"

    # 幂等：再回填一次不动
    assert registry_module.backfill(registry, rounds) == ([], [])
    assert len(registry) == 3
