"""域视图派生测试（Level 1 第二步：装配按域分化的材料层）。

钉住四件事（2026-09-12 用户口径：块作筛子、轮作单位）：
1. **归属是纯集合判断**——块的 file 落在某域 file_domains 下即归它，
   最长前缀优先；无文件的块（环境块/保底块/用户块）恒定归主 agent；
   模型产物里的 block_ids **不参与判据**（v2 命名式指派的遗留）；
2. **逐轮筛**——只有命中本视图块的轮才成一段（带该轮用户原文）；
   一块都没命中的轮**整轮不出现**；
3. **隔离**——非本视图块（描述、块 ID）在本视图字节里一次都不出现；
4. **账本分片**（全局节永不分片；主 agent 用 exclude 拿补集）。

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
    """归属＝文件域命中（最长前缀）→ 兜底主 agent；一个都不丢。"""
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


def test_block_ids_no_longer_decide_ownership():
    """产物里的 block_ids 不参与归属（判据只有文件集合本身）。

    2026-09-12 用户口径：分裂写下的 file_domains 是筛子、只用一次；
    v2 的"每块恰一个属主域 / 外域块留指针"是命名式指派，已作废。
    """
    rounds = [_block_round(1, ["shared.py"])]
    index = views_module.block_index(rounds)
    bid = next(iter(index))
    domains = [
        {"name": "甲", "file_domains": ["shared.py"]},
        {"name": "乙", "file_domains": [], "block_ids": [bid]},
    ]
    owners = views_module.ownership(domains, index)
    assert owners[bid] == "甲"            # 文件在甲的域里，乙的声明无效


def test_blocks_without_file_always_go_to_main_agent():
    """无文件的块（环境块/保底块/用户块）恒定归主 agent（用户口径）。"""
    import json as _json

    events = [
        {"id": "R1-E01", "type": "user",
         "message": {"role": "user", "content": "装依赖"}},
        {"id": "R1-E02", "type": "tool_call",
         "message": {"role": "assistant", "content": "", "tool_calls": [{
             "id": "c1", "type": "function",
             "function": {"name": "run_command",
                          "arguments": _json.dumps({"command": "pip install x"})},
         }]}},
        {"id": "R1-E03", "type": "tool_result",
         "message": {"role": "tool", "tool_call_id": "c1", "content": "ok"}},
        {"id": "R1-E04", "type": "final_answer",
         "message": {"role": "assistant", "content": "装好了"}},
    ]
    rounds = [{"seq": 1, "user_input": {"original": "装依赖", "normalized": ""},
               "events": events, "end_state": "completed"}]
    index = views_module.block_index(rounds)
    domains = [{"name": "工具层", "file_domains": ["src/wovra/tools/"]}]
    owners = views_module.ownership(domains, index)
    assert index, "环境块应当存在（run_command 的 pip 属环境类）"
    assert all(o == views_module.MAIN_AGENT_ID for o in owners.values())


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

def test_build_views_covers_every_block_at_least_once():
    """每个块至少落进一个视图（完整性对账；用户块可跨视图重复）。"""
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

    seen: set[str] = set()
    for v in built["views"].values():
        seen.update(v["own_ids"])
    assert seen == set(built["index"])


def test_view_isolates_other_domains_rounds():
    """整轮隔离：一块都没命中的轮，整轮不出现在本视图里。

    判据（2026-09-12 用户口径）：非本视图的轮不留轮号、不留涉及文件清单、
    不留块 ID——本视图字节里连它涉及的路径都不出现。
    """
    rounds = [
        _block_round(1, ["src/wovra/tools/a.py"]),
        _block_round(2, ["index.html"]),
    ]
    domains = [
        {"name": "工具层", "file_domains": ["src/wovra/tools/"]},
        {"name": "前端", "file_domains": ["index.html"]},
    ]
    built = views_module.build_views(rounds, TaskState(goal="g"), domains=domains)
    tool_text = built["views"]["工具层"]["text"]
    assert "R1" in tool_text
    assert "index.html" not in tool_text          # 非本域的文件名不出现
    assert "R2" not in tool_text                  # 连轮号都不留
    for bid, owner in built["owners"].items():
        if owner != "工具层":
            assert bid not in tool_text           # 非本域块 ID 不出现


def test_user_block_and_round_header_follow_hit_round():
    """命中轮里：轮头（用户原文）与用户块随本域块一起进本视图。"""
    import json as _json

    events = [
        {"id": "R1-E01", "type": "user",
         "message": {"role": "user", "content": "改工具层"}},
        {"id": "R1-E02", "type": "tool_call",
         "message": {"role": "assistant", "content": "", "tool_calls": [{
             "id": "c1", "type": "function",
             "function": {"name": "write_file",
                          "arguments": _json.dumps({"path": "src/wovra/tools/a.py",
                                                    "content": "x"})},
         }]}},
        {"id": "R1-E03", "type": "tool_result",
         "message": {"role": "tool", "tool_call_id": "c1", "content": "已写入"}},
        {"id": "R1-E04", "type": "user",
         "message": {"role": "user", "content": "顺手加个测试"}},
        {"id": "R1-E05", "type": "final_answer",
         "message": {"role": "assistant", "content": "完成"}},
    ]
    rounds = [{"seq": 1,
               "user_input": {"original": "改工具层", "normalized": "加固边界",
                              "key_constraints": "[R1] 只动 tools/"},
               "events": events, "end_state": "completed"}]
    index = views_module.block_index(rounds)
    kinds = {str(i["block"]["kind"]) for i in index.values()}
    assert "user" in kinds, "轮内第二条用户输入应当成用户块"
    domains = [{"name": "工具层", "file_domains": ["src/wovra/tools/"]}]
    built = views_module.build_views(rounds, TaskState(goal="g"), domains=domains)
    text = built["views"]["工具层"]["text"]
    assert '👤 用户: "改工具层"' in text            # 轮头随命中轮进
    assert "🎯 意图: 加固边界" in text
    assert "[R1] 只动 tools/" in text
    user_bids = [b for b, i in index.items() if i["block"]["kind"] == "user"]
    for bid in user_bids:                          # 用户块随命中轮一起进
        assert bid in text


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


def test_main_agent_takes_no_same_round_as_domain():
    """主 agent 的筛子 = 不属于任何域的文件（同轮不同文件各归其位）。"""
    rounds = [_block_round(1, ["index.html", "notes.md"])]
    domains = [{"name": "前端", "file_domains": ["index.html"]}]
    built = views_module.build_views(rounds, TaskState(goal="g"), domains=domains)
    main = built["views"][views_module.MAIN_AGENT_ID]
    front = built["views"]["前端"]
    main_ids = set(main["own_ids"])
    front_ids = set(front["own_ids"])
    assert not (main_ids & front_ids), "同一块不应同时归两个视图"
    owners = built["owners"]
    assert all(owners[b] == "前端" for b in front_ids)
    assert all(owners[b] == views_module.MAIN_AGENT_ID for b in main_ids)


def test_slice_state_exclude_keeps_complement():
    """主 agent 账本用 exclude：只留**没提到**任何子域文件的条目。"""
    state = TaskState(
        goal="总目标",
        decisions=["改了 src/wovra/tools/safety.py 的判定", "定了整体方向"],
    )
    sliced = views_module.slice_state(state, ["src/wovra/tools/"], exclude=True)
    assert sliced["decisions"] == ["定了整体方向"]
    assert sliced["goal"] == ["总目标"]           # 全局节两个方向都保留


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


def test_view_text_contains_card_history_and_no_pointers():
    """视图文本含域卡 + 本视图历史；不再有「外域块」指针段。"""
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
    assert "[本视图历史]" in text
    assert "[本域块]（全分辨率）" not in text     # 旧口径已作废
    assert "[外域块]" not in text                 # 指针清单已作废（隔离第一）


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
