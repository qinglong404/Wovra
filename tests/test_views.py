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


def _route_round(seq: int, target: str, files: list[str] | None = None) -> dict:
    """造一个**回合内转交**的轮：主 agent 转给 target，target 答话。

    事件序（步 = 一条 assistant 事件）：Main 写文件 → Main 转交 → target 答复。
    故答复轮归 target、转出记给 Main、步数按转交点切成两段。
    """
    import json as _json

    events = [{"id": f"R{seq}-E01", "type": "user",
               "message": {"role": "user", "content": "干活"}}]
    eid = 2
    for i, path in enumerate(files or [], start=1):
        cid = f"c{seq}_{i}"
        events.append({
            "id": f"R{seq}-E{eid:02d}", "type": "tool_call",
            "message": {"role": "assistant", "content": "", "tool_calls": [{
                "id": cid, "type": "function",
                "function": {"name": "write_file",
                             "arguments": _json.dumps({"path": path, "content": "x"})},
            }]},
        })
        eid += 1
        events.append({
            "id": f"R{seq}-E{eid:02d}", "type": "tool_result",
            "message": {"role": "tool", "tool_call_id": cid, "content": "已写入"},
        })
        eid += 1
    cid = f"c{seq}_route"
    events.append({
        "id": f"R{seq}-E{eid:02d}", "type": "tool_call",
        "message": {"role": "assistant", "content": "", "tool_calls": [{
            "id": cid, "type": "function",
            "function": {"name": "route_to",
                         "arguments": _json.dumps({"agent": target, "reason": "归它"})},
        }]},
    })
    eid += 1
    events.append({
        "id": f"R{seq}-E{eid:02d}", "type": "tool_result",
        "message": {"role": "tool", "tool_call_id": cid, "content": "已转交"},
    })
    events.append({"id": f"R{seq}-E99", "type": "final_answer",
                   "message": {"role": "assistant", "content": "完成"}})
    return {
        "seq": seq,
        "user_input": {"original": "干活", "normalized": "干活"},
        "events": events,
        "end_state": "completed",
    }


def test_agent_ledger_splits_carrier_answer_and_steps():
    """派生账：承载轮 / 答复轮 / 步数三问分答（2026-09-12 用户拍板）。

    为什么必须分：**承载**是"这份材料现在在谁手里"（随分裂变），**答复**是
    "谁真的答的话"（发生过的事实、不该被后来的分裂改写）。旧口径把两者塞进
    同一个存量字段，第一次分裂之后就再也说不清了。
    """
    rounds = [
        _block_round(1, ["src/wovra/tools/a.py"]),   # Main 起手，自己答完
        _route_round(2, "工具层", ["src/wovra/tools/b.py"]),  # Main 转出，工具层答
        _block_round(3, ["notes.md"]),               # 不属任何域的文件 → 归 Main
        _block_round(4, ["src/wovra/tools/c.py", "notes.md"]),  # 一轮里两个 agent 都有块
    ]
    domains = [{"name": "工具层", "file_domains": ["src/wovra/tools/"]}]
    ledger = views_module.agent_ledger(rounds, domains)

    main, tools = ledger["Main"], ledger["工具层"]
    # 答复：R1/R3/R4 是 Main 答的，R2 由工具层接手答完
    assert main["answer_rounds"] == 3 and tools["answer_rounds"] == 1
    # 答复轮相加 == 会话轮数（这是**唯一**可以相加的口径）
    assert main["answer_rounds"] + tools["answer_rounds"] == len(rounds)
    # 承载：R1/R2/R4 有域内文件块；R3/R4 有归主 agent 的块——R4 两边都承载
    assert tools["carrier_seqs"] == [1, 2, 4] and main["carrier_seqs"] == [3, 4]
    assert tools["carrier_blocks"] == 3 and main["carrier_blocks"] == 2
    # → **承载轮之和 5 > 会话 4 轮**：一轮的块可以分给两个 agent，故禁止求和
    assert tools["carrier_rounds"] + main["carrier_rounds"] > len(rounds)
    # 步数：按转交点分段——R2 里 Main 执行了写文件与转交两步，工具层执行答复一步
    assert main["steps"] == 9 and tools["steps"] == 1
    assert main["handoffs"] == 1 and tools["handoffs"] == 0
    # 观测字段（不落盘的那三样之外）也在：没给注册表就全是 0，不编数
    assert main["window"] == 0 and main["share"] == 0.0
    # 双键：名字与 ID 都查得到同一条
    assert ledger["A"] is tools


def test_agent_ledger_counts_only_answered_rounds_as_answers():
    """中断轮有步数、没有答复：不进答复轮，但也别把它的步丢了。"""
    r = _block_round(1, ["src/wovra/tools/a.py"])
    r["end_state"] = "open"
    r["events"] = r["events"][:-1]          # 剥掉 final_answer
    ledger = views_module.agent_ledger([r], [{"name": "工具层",
                                              "file_domains": ["src/wovra/tools/"]}])
    assert ledger["Main"]["answer_rounds"] == 0
    assert ledger["Main"]["steps"] == 1     # 那次写文件调用照记
    assert ledger["工具层"]["carrier_rounds"] == 1


def test_agent_ledger_ignores_settled_active_view_for_answers():
    """**回归**：渐近归属补判改写 `active_view` 之后，答复归属不受影响。

    这是本次改动的要害：旧账本存的是"闭合那一刻的 active_view"，而补判会在
    之后把它改掉（材料归位），于是存账描述的归属**已经不存在了**（实测 §55：
    主 agent 记 13 轮却只承载 2 块）。派生口径下答复走事件流——`active_view`
    怎么被补判改，答复数都不动。
    """
    r = _block_round(1, ["src/wovra/tools/a.py"])
    domains = [{"name": "工具层", "file_domains": ["src/wovra/tools/"]}]
    before = views_module.agent_ledger([r], domains)["Main"]["answer_rounds"]
    r["active_view"] = "工具层"              # 模拟补判
    after = views_module.agent_ledger([r], domains)["Main"]["answer_rounds"]
    assert before == after == 1
    assert views_module.agent_ledger([r], domains)["工具层"]["answer_rounds"] == 0


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
    assert "A（工具层）" in joined


def test_view_watermarks_reports_per_view_tokens_rounds_and_over_flag():
    """逐层分裂的机械判据（plan §13.1）：各视图自身体量 + 活跃轮数 + 是否到水位。

    水位基准是**该视图自己**的量（分裂后水位按视图各自计量）——故判据只需要
    体量事实，语义（这一摊活是否真分成两条线）仍归模型。
    """
    rounds = [
        _block_round(1, ["src/wovra/tools/safety.py"]),
        _block_round(2, ["index.html"]),
        _block_round(3, ["src/wovra/tools/shell.py"]),
    ]
    domains = [
        {"name": "工具层", "file_domains": ["src/wovra/tools/"]},
        {"name": "前端", "file_domains": ["index.html"]},
    ]
    marks = views_module.view_watermarks(rounds, TaskState(), domains=domains)
    assert set(marks) == {"工具层", "前端", views_module.MAIN_AGENT_ID}
    tools = marks["工具层"]
    assert tools["blocks"] >= 2                       # R1/R3 两个文件块
    assert tools["rounds"] == 2                       # 命中两轮
    assert tools["tokens"] > 0
    assert marks["前端"]["rounds"] == 1
    # 未给水位 → 不判"到线"（阈值口径随调用方，不在模块里硬编码）
    assert tools["over"] is False
    low = views_module.view_watermarks(
        rounds, TaskState(), domains=domains, watermark=1
    )
    assert low["工具层"]["over"] is True


def test_coverage_gap_reports_uncovered_files_and_overlapped_rounds():
    """覆盖缺口 + 多域共命轮是**机械事实**（2026-09-12，worklog §40.3/§44.3-5）。

    动机：主 agent 桶里"未覆盖文件"占大头时，涨的不是主 agent 的定位，而是
    分裂覆盖不全；多个域共命同一轮则是父子域语义重叠的信号（直接抬高视图
    切换频率、压低粘滞率）。两者都由 Runtime 现算成硬数据喂回分裂分析，
    判定仍归模型。
    """
    rounds = [
        # R1 被两个域同时命中（工具层 + 前端）→ 共命轮
        _block_round(1, ["src/wovra/tools/safety.py", "index.html"]),
        # R2 只有没人认领的文件 → 覆盖缺口
        _block_round(2, ["scripts/maint_health.py"]),
    ]
    domains = [
        {"name": "工具层", "file_domains": ["src/wovra/tools/"]},
        {"name": "前端", "file_domains": ["index.html"]},
    ]
    gap = views_module.coverage_gap(domains, rounds)
    assert gap["uncovered"] == ["scripts/maint_health.py"]
    assert gap["overlapped_rounds"] == [1]
    assert gap["round_domain_counts"][1] == 2

    # 全覆盖且无共命时不误报
    clean = views_module.coverage_gap(
        domains, [_block_round(3, ["src/wovra/tools/shell.py"])]
    )
    assert clean["uncovered"] == [] and clean["overlapped_rounds"] == []


def test_view_watermarks_survives_no_domains():
    """无分裂产物时不炸：只有主 agent 一份，且不被当成"要拆的主 agent"。"""
    rounds = [_block_round(1, ["a.py"])]
    marks = views_module.view_watermarks(rounds, TaskState(), domains=[])
    assert list(marks) == [views_module.MAIN_AGENT_ID]


def test_subdomain_gets_its_own_view_and_watermark():
    """逐层分裂（plan §13.1）：子域（parent）有**自己**的视图与水位。

    A-1 到自身水位后在其内部再裂一层 → A-1-1；路径 ID 由 Runtime 机械生成
    （注册表 build_entries），水位按各视图自己的量算——故子域的体量只含它
    自己名下的块，不含父域其余部分。
    """
    rounds = [
        _block_round(1, ["src/wovra/tools/safety.py"]),
        _block_round(2, ["src/wovra/tools/detect/canary.py"]),
    ]
    domains = [
        {"name": "工具层", "file_domains": ["src/wovra/tools/"]},
        {"name": "检测加固", "parent": "工具层",
         "file_domains": ["src/wovra/tools/detect/"]},
    ]
    marks = views_module.view_watermarks(rounds, TaskState(), domains=domains)
    assert set(marks) >= {"工具层", "检测加固"}
    # 归属是**最长前缀优先**：子域声明的 detect/ 更具体，那个块归子域；
    # 父域只拿剩下的（R1 的 safety.py）。故父子各一轮，互不重叠。
    assert marks["检测加固"]["rounds"] == 1
    assert marks["工具层"]["rounds"] == 1
    assert marks["检测加固"]["blocks"] == 1
    assert marks["工具层"]["blocks"] == 1

    # 路径 ID 是职责路径（A → A-1），由注册表机械生成
    from wovra import registry as registry_module
    ids = {e["name"]: e["id"] for e in registry_module.build_entries(domains)}
    assert ids == {"工具层": "A", "检测加固": "A-1"}
    built = views_module.build_views(rounds, TaskState(), domains=domains)
    assert built["views"]["检测加固"]["path_id"] == "A-1"
    # 隔离不因层级而破例：子域视图里不含父域那一轮的内容
    child_text = built["views"]["检测加固"]["text"]
    assert "canary.py" in child_text
    assert "safety.py" not in child_text


def _gen_round(seq: int, files: list[str], gen: int, summaries: dict | None = None) -> dict:
    """造一个「已整理、第 gen 代」的轮（分档判据的输入）。"""
    r = _block_round(seq, files, summaries)
    r["org_state"] = "done"
    r["org_generation"] = gen
    return r


def test_view_tiering_folds_older_generations_by_own_watermark():
    """分档（plan §2）：最近 3 批整理全分辨率，更早代折叠为文件名清单。

    基准是本视图**自己的**代次水位（本域命中轮的最大代次 − 2）：本域连续 5 代，
    则第 1–2 代折叠、第 3–5 代全分辨率。
    """
    rounds = [
        _gen_round(i, ["src/wovra/tools/f%d.py" % i], i) for i in range(1, 6)
    ]
    domains = [{"name": "工具层", "file_domains": ["src/wovra/tools/"]}]
    built = views_module.build_views(rounds, TaskState(), domains=domains)
    text = built["views"]["工具层"]["text"]

    # 更早代：只留文件名清单 + 折叠计数（不留块描述）
    assert "涉及文件：src/wovra/tools/f1.py" in text
    assert "块细节已折叠" in text
    # 最近代：块 ID 与全分辨率描述在场（R5 是最近一批）
    assert "R5-B1" in text
    # 轮头（用户原文）在两种档位下**都保留**——用户输入是输入，不是细节
    assert "👤 用户:" in text

    # 归属不因折叠而丢：折叠轮的块 ID 仍在 own_ids（完整性对账的前提）
    own = set(built["views"]["工具层"]["own_ids"])
    assert any(b.startswith("R1-B") for b in own)
    assert built["completeness"]["ok"] is True


def test_view_tiering_never_folds_organized_or_unorganized_gaps():
    """未整理的轮永不折叠；本域无已整理轮时分档基准为 None（一律全分辨率）。"""
    rounds = [_block_round(1, ["src/wovra/tools/a.py"])]      # 未整理
    domains = [{"name": "工具层", "file_domains": ["src/wovra/tools/"]}]
    assert views_module.view_keep_min(rounds) is None
    built = views_module.build_views(rounds, TaskState(), domains=domains)
    text = built["views"]["工具层"]["text"]
    assert "块细节已折叠" not in text
    assert "R1-B" in text


def test_view_keep_min_is_per_view_not_global():
    """分档基准按视图各自计量：别的域代次高，不拉高本视图的折叠线。

    本域只有第 1 代 → 基准 = 1 − 2 = −1（低于任何代次，故一律不折叠）；
    若错用全局最新代次（9），基准会是 7，本域那一轮就会被错误折叠。
    """
    rounds = [
        _gen_round(1, ["src/wovra/tools/a.py"], 1),
        _gen_round(2, ["index.html"], 9),          # 别的域整理了很多批
    ]
    domains = [
        {"name": "工具层", "file_domains": ["src/wovra/tools/"]},
        {"name": "前端", "file_domains": ["index.html"]},
    ]
    assert views_module.view_keep_min([rounds[0]]) == -1   # 本域单代：基准 −1
    assert views_module.view_keep_min(rounds) == 7         # 全局口径会算出 7（不许用）
    built = views_module.build_views(rounds, TaskState(), domains=domains)
    tools_text = built["views"]["工具层"]["text"]
    assert "R1-B" in tools_text
    assert "块细节已折叠" not in tools_text
