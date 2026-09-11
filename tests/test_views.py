"""域视图派生测试（Level 1 第二步：装配按域分化的材料层）。

钉住三件事：
1. **块归属完整性**（不丢块）——每个块恰有一个归宿、且真的落进某个视图；
2. **归属优先级**（域显式声明 → 文件域命中（最长前缀）→ 兜底主 agent）；
3. **账本分片**（全局节永不分片、分片节按文件域机械匹配）。

纯函数、零 LLM：这些用例不需要替身模型，直接喂 rounds/state/domains。
"""

from wovra import views as views_module
from wovra.task import TaskState


def _block_round(seq: int, files: list[str], summaries: dict | None = None) -> dict:
    """造一个含文件块的轮（v3 分块靠 tool_call 参数，故直接构造事件）。"""
    import json as _json

    events = [{"id": f"R{seq}-E01", "type": "user",
               "message": {"role": "user", "content": "干活"}}]
    for i, path in enumerate(files, start=1):
        cid = f"c{seq}_{i}"
        events.append({
            "id": f"R{seq}-E{i * 2:02d}", "type": "tool_call",
            "message": {"role": "assistant", "content": "", "tool_calls": [{
                "id": cid, "type": "function",
                "function": {"name": "write_file",
                             "arguments": _json.dumps({"path": path, "content": "x"})},
            }]},
        })
        events.append({
            "id": f"R{seq}-E{i * 2 + 1:02d}", "type": "tool_result",
            "message": {"role": "tool", "tool_call_id": cid, "content": "已写入"},
        })
    events.append({"id": f"R{seq}-E99", "type": "final_answer",
                   "message": {"role": "assistant", "content": "完成"}})
    r = {
        "seq": seq,
        "user_input": {"original": "干活", "normalized": "干活"},
        "events": events,
        "end_state": "completed",
    }
    if summaries:
        r["block_summaries"] = summaries
    return r


def test_ownership_priority_and_no_lost_blocks():
    """归属三级优先：显式声明 > 文件域命中 > 兜底主 agent；一个都不丢。"""
    rounds = [
        _block_round(1, ["src/wovra/tools/safety.py"]),
        _block_round(2, ["index.html"]),
        _block_round(3, ["notes.md"]),
    ]
    domains = [
        {"name": "工具层", "file_domains": ["src/wovra/tools/"]},
        {"name": "前端", "file_domains": ["index.html"]},
    ]
    index = views_module.block_index(rounds)
    owners = views_module.ownership(domains, index)

    assert len(index) >= 3
    # 每个块都有归宿（不丢块）
    assert set(owners) == set(index)
    # 文件域命中
    for bid, item in index.items():
        f = item["block"].get("file")
        if f == "src/wovra/tools/safety.py":
            assert owners[bid] == "工具层"
        elif f == "index.html":
            assert owners[bid] == "前端"
        elif f == "notes.md":
            assert owners[bid] == views_module.MAIN_AGENT_ID   # 无匹配 → 兜底


def test_explicit_block_ids_override_file_match():
    """域显式声明优先于文件域机械命中（尊重模型的跨域/例外判断）。"""
    rounds = [_block_round(1, ["shared.py"])]
    index = views_module.block_index(rounds)
    bid = next(iter(index))
    domains = [
        {"name": "甲", "file_domains": ["shared.py"]},
        {"name": "乙", "file_domains": [], "block_ids": [bid]},
    ]
    owners = views_module.ownership(domains, index)
    assert owners[bid] == "乙"


def test_longest_file_prefix_wins():
    """同一文件命中多个域时归**最长前缀**（职责粒度更细的那个说了算）。"""
    rounds = [_block_round(1, ["src/a/b.py"])]
    index = views_module.block_index(rounds)
    bid = next(iter(index))
    domains = [
        {"name": "粗", "file_domains": ["src/"]},
        {"name": "细", "file_domains": ["src/a/"]},
    ]
    assert views_module.ownership(domains, index)[bid] == "细"


def test_build_views_covers_every_block_exactly_once():
    """每个块恰在一个视图的「本域块」里出现一次（完整性对账）。"""
    rounds = [
        _block_round(1, ["src/wovra/tools/safety.py", "docs/a.md"]),
        _block_round(2, ["index.html"]),
    ]
    domains = [
        {"name": "工具层", "description": "边界", "file_domains": ["src/wovra/tools/"],
         "goal": "加固"},
        {"name": "前端", "description": "演示", "file_domains": ["index.html"]},
    ]
    built = views_module.build_views(
        rounds, TaskState(goal="g"), domains=domains
    )
    comp = built["completeness"]
    assert comp["ok"] is True
    assert comp["missing"] == [] and comp["dropped_from_views"] == []

    seen = []
    for v in built["views"].values():
        for line in v["own"]:
            seen.append(line.split("（R", 1)[0].replace("▸ ", "").strip())
    assert sorted(seen) == sorted(built["index"])       # 恰好一次，不重不漏


def test_main_agent_view_always_exists_and_holds_orphans():
    """主 agent 视图恒定存在，且拿走无归宿块（不丢块的兜底落点）。"""
    rounds = [_block_round(1, ["orphan.md"])]
    built = views_module.build_views(
        rounds, TaskState(goal="g"), domains=[{"name": "别处", "file_domains": ["x/"]}]
    )
    assert views_module.MAIN_AGENT_ID in built["views"]
    main = built["views"][views_module.MAIN_AGENT_ID]
    assert main["counts"]["own"] == len(built["index"])
    assert built["completeness"]["ok"] is True


def test_pointers_are_aggregated_per_owner():
    """外域块按属主域聚合为一行（块 ID 清单）——不是一块一行。

    实测依据（2026-09-11，269 块真实会话）：一块一行约 250 行 ≈ 9K tok，
    指针的用途只是"这个事实在别处"，块 ID 清单同等可达。
    """
    rounds = [
        _block_round(1, ["src/wovra/tools/a.py", "src/wovra/tools/b.py"]),
        _block_round(2, ["index.html"]),
    ]
    domains = [
        {"name": "工具层", "file_domains": ["src/wovra/tools/"]},
        {"name": "前端", "file_domains": ["index.html"]},
    ]
    built = views_module.build_views(rounds, TaskState(goal="g"), domains=domains)
    front = built["views"]["前端"]["pointers"]
    # 前端视图里，所有工具层的块聚合在若干行内（每个属主一行）
    tool_lines = [ln for ln in front if ln.startswith("· 工具层：")]
    assert len(tool_lines) == 1
    assert "块——" in tool_lines[0]
    # 保底：每个外域块 ID 都出现在某行清单里
    tool_bids = [b for b, o in built["owners"].items() if o == "工具层"]
    joined = "".join(front)
    for bid in tool_bids:
        assert bid in joined


def test_slice_state_keeps_global_and_shards_by_file():
    """账本分片：全局节原样保留；分片节只留提到本域文件的条目。"""
    state = TaskState(
        goal="总目标",
        current_status="正在做",
        escalations=["需要拍板 A"],
        experiments=["验证 X"],
        decisions=["改了 src/wovra/tools/safety.py 的判定", "改了 index.html 的布局"],
        known_issues=["index.html 在窄屏错位"],
    )
    sliced = views_module.slice_state(state, ["src/wovra/tools/"])
    # 全局节永不分片（人机协同回路不能丢）
    assert sliced["goal"] == ["总目标"]
    assert sliced["current_status"] == ["正在做"]
    assert sliced["escalations"] == ["需要拍板 A"]
    assert sliced["experiments"] == ["验证 X"]
    # 分片节按文件域机械命中（文件名形态也算命中）
    assert sliced["decisions"] == ["改了 src/wovra/tools/safety.py 的判定"]
    assert "known_issues" not in sliced          # 与本域无关，切掉


def test_slice_state_empty_match_is_not_error():
    """命中 0 条不是错误：该域还没产生相关决策而已（全局节仍兜住回路）。"""
    state = TaskState(goal="g", decisions=["改了别的东西"])
    sliced = views_module.slice_state(state, ["nothing/here.md"])
    assert "decisions" not in sliced
    assert sliced["goal"] == ["g"]


def test_view_text_contains_card_own_and_pointers():
    """视图文本含域卡（职责/目标/文件域）、本域块、外域指针三段。"""
    rounds = [_block_round(1, ["src/wovra/tools/a.py", "index.html"],
                           summaries=None)]
    domains = [
        {"name": "工具层", "description": "边界守卫", "goal": "加固",
         "file_domains": ["src/wovra/tools/"]},
        {"name": "前端", "file_domains": ["index.html"]},
    ]
    built = views_module.build_views(rounds, TaskState(goal="g"), domains=domains)
    text = built["views"]["工具层"]["text"]
    assert "[职责域] 工具层" in text
    assert "职责：边界守卫" in text and "目标：加固" in text
    assert "所有权文件域：src/wovra/tools/" in text
    assert "[本域块]（全分辨率）" in text
    assert "[外域块]" in text


def test_human_report_reports_completeness():
    """人视图摘要：块总数 + 归属完整性 + 每域体量。"""
    rounds = [_block_round(1, ["src/wovra/tools/a.py"])]
    built = views_module.build_views(
        rounds, TaskState(goal="g"),
        domains=[{"name": "工具层", "file_domains": ["src/wovra/tools/"]}],
    )
    lines = views_module.human_report(built)
    joined = "\n".join(lines)
    assert "域视图" in joined
    assert "归属完整：是" in joined
    assert "A-1（工具层）" in joined
