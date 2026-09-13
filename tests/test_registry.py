"""注册表构建测试（Level 1 视图分化第一步）。

领域树 → 注册表条目是机械翻译：语义由分裂分析（LLM）产出，编号与
层级由 Runtime 机械派生（语义归模型、体量归机制）。这些用例钉住
三件事：路径 ID 的层级生成、幂等、脏数据兜底。
"""

import json

from wovra import registry as registry_module


def test_build_entries_generates_path_ids_from_tree():
    """顶层域 → A/B（字母）；子域 → A-1（路径 ID 是职责路径，非管理树）。

    2026-09-12 改版：主 agent 不再占 `A`（它是 `Main`），于是第一次分裂的
    顶层域拿到 A、B、C…——与 `docs/思考内容与AI对话.md` §五（A=大类、
    A-1=子类）一致。
    """
    domains = [
        {"name": "工具层", "description": "路径与命令边界",
         "files": ["src/wovra/tools/safety.py"], "goal": "加固"},
        {"name": "探针", "description": "安全仪器",
         "parent": "工具层", "files": ["experiments/probe.py"]},
        {"name": "前端", "description": "演示页",
         "files": ["webui/index.html"]},
    ]
    entries = registry_module.build_entries(domains)

    ids = [e["id"] for e in entries]
    assert ids == ["A", "B"]                   # 只登记**分裂层**（顶层 2 个节点）
    top = entries[0]
    assert top["name"] == "工具层"
    assert top["goal"] == "加固"
    # 条目的 files = **子树**里的文件名（父域含子域的文件）
    assert set(top["files"]) == {"src/wovra/tools/safety.py", "experiments/probe.py"}
    assert top["status"] == "dormant"          # 休眠是默认态（零成本）
    assert top["inbox"] == []
    # 只留观测字段（2026-09-12 用户拍板：账本派生、不落盘）——
    # 轮次/步数/承载由 views.agent_ledger 现场算，条目上不再有这三个键
    assert (top["ctx_cur"], top["ctx_peak"], top["window"]) == (0, 0, 0)
    assert not {"rounds", "steps", "handoffs"} & set(top)

    # 子 agent 分裂（两级替换）：顶层单节点 → **下钻**一层，产物平级为 A-1
    sub = registry_module.build_entries(domains[:2], parent_id="A")
    assert [e["id"] for e in sub] == ["A-1"]
    assert sub[0]["name"] == "探针"


def test_merge_into_retires_the_split_domain():
    """两级替换：A 分裂后 **A 消失**，产物以 A-1、A-2…平级登记（§63）。"""
    registry = [{"id": registry_module.MAIN_AGENT_ID, "name": "主agent"},
                {"id": "A", "name": "工具层", "files": ["src/a.py"]}]
    added, _updated = registry_module.merge_into(
        registry,
        [{"name": "小工具", "files": ["src/a.py"], "parent": "工具层"},
         {"name": "检测", "files": ["src/detect.py"], "parent": "工具层"}],
        parent_id="A", retire_id="A",
    )
    assert "工具层" not in [e["name"] for e in registry]     # A 已消失
    assert {e["id"] for e in registry} == {registry_module.MAIN_AGENT_ID,
                                           "A-1", "A-2"}
    assert set(added) >= {"A-1", "A-2"}


def test_split_defects_catches_overlap_and_uncovered():
    """**入口校验**（F2 互斥 + F3 完备）：重叠 / 未覆盖 / 空域都要抓出来。

    用户口径（2026-09-12，worklog §62）：一个文件不可能两个子 agent 共同维护；
    被分裂者名下的文件必须 100% 分完——有缺陷就**拒收**（"根基错了，测试无意义"）。
    """
    files = ["src/a.py", "src/wovra/tools/b.py", "webui/x.js"]
    # 前缀重叠：`src/` 与 `src/wovra/tools/` 会同时命中 b.py
    defects = registry_module.split_defects(
        [{"name": "工具层", "file_domains": ["src/"]},
         {"name": "深工具", "file_domains": ["src/wovra/tools/"]},
         {"name": "前端", "file_domains": ["webui/"]}],
        files,
    )
    assert any("重叠" in d and "src/wovra/tools/b.py" in d for d in defects)

    # 未覆盖：webui/x.js 没人认领
    defects = registry_module.split_defects(
        [{"name": "工具层", "files": ["src/a.py", "src/wovra/tools/b.py"]}],
        files,
    )
    assert any("未覆盖" in d and "webui/x.js" in d for d in defects)

    # 空域：给了个域但没有任何文件
    defects = registry_module.split_defects(
        [{"name": "空壳", "files": []}], ["src/a.py"]
    )
    assert any("空域" in d for d in defects)

    # 通过：精确文件清单、恰好覆盖、互不重叠
    assert registry_module.split_defects(
        [{"name": "工具层", "files": ["src/a.py", "src/wovra/tools/b.py"]},
         {"name": "前端", "files": ["webui/x.js"]}],
        files,
    ) == []

    # **非 LIVE（历史）也要有落点**（只读/被取代/被删都挂在最相关 LIVE 块下）
    hist = ["docs/old-notes.md", "src/legacy.py"]
    defects = registry_module.split_defects(
        [{"name": "工具层", "files": ["src/a.py"],
          "history_files": ["src/legacy.py"]},
         {"name": "前端", "files": ["webui/x.js"]}],
        ["src/a.py", "webui/x.js"], hist,
    )
    assert any("历史未落点" in d and "docs/old-notes.md" in d for d in defects)
    assert not any("src/legacy.py" in d for d in defects)   # 已挂在工具层下

    # 历史文件也不能两个域都认领
    defects = registry_module.split_defects(
        [{"name": "甲", "files": ["src/a.py"], "history_files": ["src/legacy.py"]},
         {"name": "乙", "files": ["webui/x.js"], "history_files": ["src/legacy.py"]}],
        ["src/a.py", "webui/x.js"], ["src/legacy.py"],
    )
    assert any("历史重叠" in d for d in defects)


def test_build_entries_empty_and_dirty_parent():
    """空产物 → 无条目；parent 指向不存在的域 → 按顶层处理（不吞子树）。"""
    assert registry_module.build_entries([]) == []
    assert registry_module.build_entries(None) == []
    entries = registry_module.build_entries([
        {"name": "孤儿", "parent": "不存在的父"},
        {"name": "正常", "description": "d"},
    ])
    assert [e["id"] for e in entries] == ["A", "B"]   # 两个顶层，不丢


def test_build_entries_breaks_cycles():
    """脏数据成环（甲的父是乙、乙的父是甲）不递归到死，条目照长。"""
    entries = registry_module.build_entries([
        {"name": "甲", "parent": "乙"},
        {"name": "乙", "parent": "甲"},
    ])
    assert entries, "环里也要长出条目（信息不切开的兜底）"
    assert all(e["name"] in ("甲", "乙") for e in entries)


def test_merge_into_is_idempotent_and_preserves_runtime_state():
    """重复 promote 只更新描述，不重复追加；status/inbox 不被抹掉。"""
    registry = [{"id": registry_module.MAIN_AGENT_ID, "name": "主agent",
                 "description": "全局协调",
                 "file_domains": [], "status": "active", "inbox": []}]
    domains = [{"name": "工具层", "description": "旧描述",
                "file_domains": ["tools/"]}]

    added, updated = registry_module.merge_into(registry, domains)
    assert added == ["A"] and updated == []
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
    assert added3 == [] and updated3 == ["A"]
    assert len(registry) == 2
    assert registry[1]["description"] == "新描述"
    assert registry[1]["file_domains"] == ["tools/", "blocks/"]


def test_merge_into_handles_missing_registry_and_empty_domains():
    """registry 为 None（旧任务）时能就地建表；空域不产生条目。"""
    registry: list = []
    added, updated = registry_module.merge_into(registry, [{"name": "单域"}])
    assert added == ["A"] and updated == []
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
    registry = [{"id": registry_module.MAIN_AGENT_ID, "name": "主agent",
                 "description": "全局协调",
                 "file_domains": [], "status": "active", "inbox": []}]

    added, updated = registry_module.backfill(registry, rounds)
    assert added == ["A"] and updated == []       # **只登记分裂层**（顶层 1 个）
    assert "旧域" not in [e["name"] for e in registry]   # 旧批次被取代
    assert registry[-1]["name"] == "工具层"

    # 幂等：再回填一次不动
    assert registry_module.backfill(registry, rounds) == ([], [])
    assert len(registry) == 2


def test_top_id_and_legacy_migration():
    """ID 体系 v1 → v2 的机械迁移（2026-09-12 用户拍板）。

    v1：主 agent 占 `A`，第一次分裂产出 `A-1`、`A-2`…（看起来像主 agent 的
    子目录）；v2：主 agent = `Main`，顶层域取 `A`、`B`、`C`…，`A` 满了才在
    A 内裂 `A-1`。
    """
    assert [registry_module.top_id(i) for i in (1, 2, 26, 27, 28)] == [
        "A", "B", "Z", "AA", "AB",
    ]
    assert registry_module.migrate_legacy_id("A") == "Main"
    assert registry_module.migrate_legacy_id("A-1") == "A"
    assert registry_module.migrate_legacy_id("A-2") == "B"
    assert registry_module.migrate_legacy_id("A-1-2") == "A-2"
    # 已经是新体系/自由文本 → 原样（迁移靠 scheme 标记把住，不看形状猜）
    assert registry_module.migrate_legacy_id("Main") == "Main"
    assert registry_module.migrate_legacy_id("工具层") == "工具层"

    registry = [
        {"id": "A", "name": "主agent", "inbox": []},
        {"id": "A-1", "name": "工具层", "inbox": [{"from": "A-2", "message": "x"}]},
        {"id": "A-1-1", "name": "装配与视图分化", "inbox": []},
    ]
    rounds = [
        {"seq": 1, "active_view": "A", "route_handoff": {"from": "A", "to": "工具层"}},
        {"seq": 2, "active_view": "工具层", "route_explicit": "A"},
    ]
    moved, _samples = registry_module.migrate_agent_ids(registry, rounds)
    assert [e["id"] for e in registry] == ["Main", "A", "A-1"]
    assert registry[1]["inbox"][0]["from"] == "B"      # 收件箱里的旧 ID 形态也迁
    assert rounds[0]["active_view"] == "Main"          # 哨兵换名
    assert rounds[0]["route_handoff"]["from"] == "Main"
    assert rounds[1]["active_view"] == "工具层"         # 域名不动
    assert rounds[1]["route_explicit"] == "Main"
    assert moved >= 5


# ---------------------------------------------------------------------------
# 归属结算 + 跨条目互斥体检（2026-09-13，worklog §76）
#
# 实测缺陷（会话 20260913-125849-2963df）：`merge_into` 原先只写新条目、
# 只更新同 id 条目，**分裂方（主 agent）自己的清单谁也不碰**。主 agent 从
# 创建时累积的清单（F5 谁创建谁拥有）于是与子域清单重叠——Main.files(17)
# ∩ D.files(17) = 16 个文件，另有一个已删文件一边在 Main.files 算 LIVE、
# 一边在 D.history_files 算已删。后果不是"页面难看"：`_file_permission`
# 先看 mine，两家 file_owned_by 都 True → **两个 agent 都能写同一批文件**。
# ---------------------------------------------------------------------------


def _splitter_with_files():
    """分裂前的主 agent + 三个探针文件（其中一个已删）。"""
    return [{
        "id": registry_module.MAIN_AGENT_ID, "name": "主agent",
        "description": "全局协调", "status": "active", "inbox": [],
        "files": ["output/_p1.py", "output/_p2.py", "output/_dead.py"],
        "history_files": [],
        "file_notes": {"output/_p1.py": "探针一", "output/_p2.py": "探针二",
                       "output/_dead.py": "已删的探针"},
    }]


def test_settle_ownership_subtracts_claimed_files_from_the_splitter():
    """产物认领的文件从主 agent 清单里减掉——"全部文件活搬进子 agent"。"""
    registry = _splitter_with_files()
    entries = registry_module.build_entries([{
        "name": "诊断探针与常驻仪器", "description": "取证脚本",
        "files": ["output/_p1.py", "output/_p2.py"],
        "history_files": ["output/_dead.py"],
    }])

    moved = registry_module.settle_ownership(registry, entries)
    main = registry[0]
    assert registry_module.entry_files(main) == []
    assert registry_module.entry_history(main) == []
    # 清单里没了的文件，描述一并摘掉（不给主 agent 留空抽屉）
    assert main["file_notes"] == {}
    assert len(moved) == 3
    assert all("交出" in m for m in moved)


def test_settle_ownership_is_end_to_end_via_merge_into():
    """`merge_into` 落地后全注册表无重叠——这正是实测缺的那一步。"""
    registry = _splitter_with_files()
    added, _updated = registry_module.merge_into(registry, [{
        "name": "诊断探针与常驻仪器", "description": "取证脚本",
        "files": ["output/_p1.py", "output/_p2.py"],
        "history_files": ["output/_dead.py"],
    }])
    assert added == ["A"]
    assert registry_module.entry_files(registry[0]) == []
    assert registry_module.registry_defects(registry) == []
    # 归属收敛到子域：一个文件只有一个主人
    assert registry_module.owner_of_file(registry, "output/_p1.py").startswith("A")


def test_settle_ownership_noop_without_claims_and_keeps_legacy_prefixes():
    """产物没给文件清单 → 不动别人的账；老前缀口径（file_domains）不参与结算。

    用户口径（2026-09-13）：「旧会话原样保留，只修机制」——前缀归属是历史
    兼容读取（`entry_prefixes`），不在结算范围内。
    """
    registry = _splitter_with_files()
    registry[0]["file_domains"] = ["output/"]
    # ① 无文件清单的产物：一字不动
    registry_module.settle_ownership(
        registry, registry_module.build_entries([{"name": "空域"}])
    )
    assert len(registry_module.entry_files(registry[0])) == 3
    # ② 前缀仍在 → 仍算它维护（兼容读取），但清单不被前缀牵连
    registry_module.settle_ownership(
        registry, registry_module.build_entries(
            [{"name": "别处", "files": ["src/other.py"]}]
        )
    )
    assert len(registry_module.entry_files(registry[0])) == 3
    assert registry[0]["file_domains"] == ["output/"]      # 历史字段原样


def test_project_merge_is_pure():
    """预演不改传入的注册表（门与落地同一套代码，但门不能有副作用）。"""
    registry = _splitter_with_files()
    before = json.dumps(registry, ensure_ascii=False, sort_keys=True)
    merged, added, _updated, settle = registry_module.project_merge(registry, [{
        "name": "探针", "description": "取证",
        "files": ["output/_p1.py", "output/_p2.py", "output/_dead.py"],
    }])
    assert json.dumps(registry, ensure_ascii=False, sort_keys=True) == before
    assert added == ["A"] and len(settle) == 3
    assert registry_module.entry_files(merged[0]) == []
    assert [e["id"] for e in merged] == ["Main", "A"]


def test_registry_defects_reports_all_three_overlap_shapes():
    """互斥体检覆盖 files∩files、files∩history、history∩history 三种形态。"""
    assert registry_module.registry_defects([]) == []
    assert registry_module.registry_defects([
        {"id": "Main", "name": "主", "files": ["a.py"]},
        {"id": "A", "name": "甲", "files": ["b.py"]},
    ]) == []

    bad = [
        {"id": "Main", "name": "主", "files": ["a.py", "x.py"],
         "history_files": ["h.py"]},
        {"id": "A", "name": "甲", "files": ["a.py"]},
        {"id": "B", "name": "乙", "history_files": ["x.py", "h.py"]},
    ]
    defects = registry_module.registry_defects(bad)
    kinds = {" ".join(d.split("共认")[0].split()) for d in defects}
    assert "F2 违反：Main.files 与 A.files" in kinds
    assert "F2 违反：Main.files 与 B.history_files" in kinds
    assert "F2 违反：Main.history_files 与 B.history_files" in kinds
    assert all("一个文件只能一个域" in d for d in defects)
    # 无 name 的脏条目跳过（注册表里可能有半截条目）
    assert registry_module.registry_defects([{"id": "X"}]) == []


def test_registry_defects_ignores_legacy_prefixes():
    """前缀是兼容读取口径，不让它把旧会话天天报成冲突。"""
    assert registry_module.registry_defects([
        {"id": "Main", "name": "主", "file_domains": ["output/"]},
        {"id": "A", "name": "甲", "files": ["output/_p1.py"]},
    ]) == []
