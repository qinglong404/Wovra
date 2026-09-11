"""Agent 运行时测试（自 test_agent.py 拆分，2026-09-11）。

本模块：test_assembly。"""

import json
from wovra import task as task_module
from wovra.agent import Agent
from wovra.task import Task

# 共享夹具/工具（_helpers.py 是原文件的公共头部）
from ._helpers import *  # noqa: F401,F403

def test_managed_assembly_condenses_organized_rounds():
    """加载视图：已整理轮次 = 用户原文 + 意图 + 精修索引。

    长回答的尾部细节不进上下文，头部与精修索引进入；
    整理是轮次从全量变紧凑的唯一途径。
    """
    long_answer = "很长的回答开头。" + "细节" * 100 + "很长的回答结尾。"
    task = Task.create(goal="x")
    task.rounds = [
        _round(1, "第一轮原始提问", long_answer),
        _round(2, "第二轮 UI 修改", "按钮改好了"),
        _round(3, "第三轮闲聊", "哈哈"),
        _round(4, "第四轮 ICP 调试", "误差降低了"),
    ]
    agent = Agent(llm=_StubLLM(), tools=[], task=task)
    _make_open_round(agent, 5, "继续")

    msgs = agent._assemble_messages()
    bodies = [m.get("content", "") for m in msgs]

    assert any("误差降低了" in b for b in bodies)            # 未整理轮全量
    assert any("第一轮原始提问" in b for b in bodies)         # 用户原文全量保留
    assert any("澄清：第一轮原始提问" in b for b in bodies)   # Normalized 意图
    assert any("R1-E02" in b for b in bodies)                 # 精修事件索引
    assert any("很长的回答开头" in b for b in bodies)         # 截断头部可见
    assert not any("很长的回答结尾" in b for b in bodies)     # 头部之后的细节不进上下文


def test_expand_history_reads_full_content():
    long_answer = "很长的回答开头。" + "细节" * 100 + "很长的回答结尾。"
    task = Task.create(goal="x")
    task.rounds = [_round(1, "第一轮原始提问", long_answer)]
    agent = Agent(llm=_StubLLM(), tools=[], task=task)

    full = agent.expand_history(["R1-E02"], level="full")
    assert "很长的回答结尾" in full  # 展开能取回头部之外的原文

    summary = agent.expand_history(["R1"], level="summary")
    assert "澄清：第一轮原始提问" in summary and "[R1-E01]" in summary


def test_managed_mode_uses_full_for_current_round():
    """当前 Round 全量保留：自己的完整回答不出现在截断索引里。"""
    task = Task.create(goal="x")
    task.rounds = [_round(1, "第一轮原始提问", "第一轮完整回答内容")]
    agent = Agent(llm=_StubLLM(), tools=[], task=task)
    _make_open_round(agent, 2, "继续")
    agent._record_event("final_answer", {"role": "assistant", "content": "本轮完整的最终回答"})

    msgs = agent._assemble_messages()

    assert any("本轮完整的最终回答" in (m.get("content") or "") for m in msgs)


def test_expand_history_tolerates_string_ids_and_case(monkeypatch):
    """模型偶尔传逗号字符串 ids 和大写 level，必须容错。"""
    task = Task.create(goal="x")
    task.rounds = [_round(1, "第一轮原始提问", "第一轮完整回答内容")]
    agent = Agent(llm=_StubLLM(), tools=[], task=task)

    result = agent.expand_history("R1-E01,R1-E02", level="Full")
    assert "第一轮原始提问" in result
    assert "第一轮完整回答内容" in result


def test_unorganized_rounds_stay_full_until_organized(monkeypatch):
    """前缀纪律：未整理轮次永远全量在上下文，无论轮数多少、体量多大——
    分辨率损失只允许来自整理，不来自装配（三档滑窗已废除）。"""
    task = Task.create(goal="分层回归")
    task.rounds = [
        _round(1, "处理 ICP 配准误差问题", "ICP 误差分析完成"),
        _round(2, "修改界面按钮颜色", "按钮改好了"),
        _round(3, "调整界面布局间距", "布局调整完毕"),
        _round(4, "继续处理 ICP 配准", "ICP 参数已更新"),
    ]
    for r in task.rounds:
        r["org_state"] = ""  # 全部未整理
    agent = Agent(llm=_StubLLM(), tools=[], task=task)
    _make_open_round(agent, 5, "ICP 误差为什么还是这么大")

    msgs = agent._assemble_messages()
    bodies = "\n".join(m.get("content", "") for m in msgs)

    # 每一轮的完整原文都在——没有一个轮被静默降档
    for answer in ("ICP 误差分析完成", "按钮改好了", "布局调整完毕", "ICP 参数已更新"):
        assert answer in bodies


def test_organized_rounds_render_compact_views(monkeypatch):
    """整理生效后：已整理轮次 = 原文+意图+精修索引的紧凑视图，
    未整理轮次仍全量——同一装配里两种形态按轮共存。"""
    task = Task.create(goal="分层回归")
    task.rounds = [
        _round(1, "处理 ICP 配准误差问题", "ICP 误差分析完成"),
        _round(2, "修改界面按钮颜色", "按钮改好了"),
    ]
    task.rounds[0]["org_state"] = "done"    # R1 已整理
    task.rounds[0]["refined_index"] = {
        "R1-E01": "提出误差问题",
        "R1-E02": "ICP 误差已收敛到 0.5px",
    }
    task.rounds[1]["org_state"] = ""        # R2 未整理
    agent = Agent(llm=_StubLLM(), tools=[], task=task)
    _make_open_round(agent, 3, "继续")

    msgs = agent._assemble_messages()
    bodies = "\n".join(m.get("content", "") for m in msgs)

    # R1 已整理：紧凑视图（原文+意图+精修索引），事件原文不再全量
    assert "处理 ICP 配准误差问题" in bodies
    assert "澄清：处理 ICP 配准误差问题" in bodies
    assert "R1-E02" in bodies and "0.5px" in bodies   # 精修索引在
    assert "ICP 误差分析完成" not in bodies           # 回答原文不进上下文
    # R2 未整理：全量原文仍在
    assert "按钮改好了" in bodies


def test_organized_rounds_render_block_details_view(monkeypatch):
    """2026-09-08 新契约：已整理轮 = 👤用户/🎯意图/📌关键约束 + 逐块细节描述。

    块描述承载完整细节，事件索引退场；旧整理轮（无块描述）回退精修
    事件索引（见 test_organized_rounds_render_compact_views）。
    """
    task = Task.create(goal="分层回归")
    task.rounds = [{
        "seq": 1,
        "user_input": {
            "original": "不要用git,先把0和初期扩展做了.",
            "normalized": "拒绝 git，落库测试脚本并实施全部短期扩展",
            "key_constraints": "全程禁止任何 git 命令",
        },
        "events": [
            {"id": "R1-E01", "type": "user", "status": "",
             "truncated": "不要用git", "message": {"role": "user", "content": "不要用git"}},
            {"id": "R1-E02", "type": "final_answer", "status": "",
             "truncated": "交付：测试落库 + 五项短期扩展" + "细节" * 200,
             "message": {"role": "assistant",
                         "content": "交付：测试落库 + 五项短期扩展" + "细节" * 200}},
        ],
        "blocks": [{
            "id": "R1-B1", "kind": "work", "start": 0, "end": 1,
            "start_event": "R1-E01", "end_event": "R1-E02",
            "touched_files": ["tests/run.js"], "wrote_files": ["tests/run.js"],
            "command_types": ["test"],
        }],
        "block_summaries": {
            "R1-B1": "创建 tests/run.js 一键回归入口（1467 字符，顺序执行 5 个套件"
                     "并汇总），把散落在 /tmp 的测试脚本正式落库；全程未碰 git",
        },
        "refined_index": {}, "end_state": "completed", "org_state": "done",
    }]
    agent = Agent(llm=_StubLLM(), tools=[], task=task)
    _make_open_round(agent, 2, "继续")

    msgs = agent._assemble_messages()
    body = "\n".join(m.get("content", "") for m in msgs)

    assert '👤 用户: "不要用git,先把0和初期扩展做了."' in body
    assert "🎯 意图: 拒绝 git，落库测试脚本并实施全部短期扩展" in body
    assert "📌 关键约束: 全程禁止任何 git 命令" in body
    assert "▸ R1-B1: 创建 tests/run.js 一键回归入口" in body
    assert "事件索引" not in body                       # 事件行退场
    assert "细节" * 200 not in body                     # 回答原文不进上下文


def test_organization_missing_blocks_get_fallback_route_lines(monkeypatch, tmp_path):
    """完整性兜底：LLM 漏标的块用确定性路由行补齐——视图里不允许出现
    没有描述的块（用户拍板：保证完整的细节描述）。"""
    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path)
    org_json = json.dumps({
        "rounds": [{"seq": 1, "normalized_user_input": "意图"}],  # 未给 block_summaries
        "state_patch": {},
    }, ensure_ascii=False)
    responses = [
        [_chunk(_delta(content="干完了"))],
        [_chunk(_delta(content=org_json))],
    ]
    task = Task.create(goal="目标")
    agent = Agent(llm=_StubLLM(responses), tools=[], task=task, org_watermark=0, org_grace_rounds=0, org_cooldown_rounds=0)

    agent.run("问")

    blocks = task.rounds[-1]["blocks"]
    summaries = task.rounds[-1]["pending_org"]["block_summaries"]
    assert set(summaries) == {b["id"] for b in blocks}   # 覆盖完整
    assert all("仅路由" in s for s in summaries.values())


def test_expand_history_supports_block_ids():
    """紧凑视图以块 ID 为定位锚：expand_history("R1-B1") 取回整块原文。"""
    task = Task.create(goal="x")
    task.rounds = [{
        "seq": 1,
        "user_input": {"original": "改按钮", "normalized": "改按钮颜色"},
        "events": [
            {"id": "R1-E01", "type": "tool_call", "status": "", "truncated": "调用 edit_file",
             "message": {"role": "assistant", "tool_calls": [
                 {"id": "c1", "type": "function",
                  "function": {"name": "edit_file",
                               "arguments": "{\"path\": \"app.py\"}"}}]}},
            {"id": "R1-E02", "type": "final_answer", "status": "", "truncated": "改好了",
             "message": {"role": "assistant", "content": "按钮已改成蓝色，刷新即可看到"}},
        ],
        "blocks": [{
            "id": "R1-B1", "kind": "work", "start": 0, "end": 1,
            "start_event": "R1-E01", "end_event": "R1-E02",
            "touched_files": ["app.py"], "wrote_files": ["app.py"],
            "command_types": [],
        }],
        "refined_index": {}, "end_state": "completed", "org_state": "done",
    }]
    agent = Agent(llm=_StubLLM(), tools=[], task=task)

    out = agent.expand_history("R1-B1")
    assert "R1-E01" in out and "edit_file" in out and "app.py" in out
    assert "按钮已改成蓝色" in out
    assert "未找到块" not in out


def test_current_round_events_not_folded():
    """轮内赦免：开放轮事件全量进上下文（旧折叠机制曾诱发重读死循环）。"""
    from wovra import truncate

    agent = _agent_with([])
    agent.rounds = []
    agent.current_round = {
        "seq": 1, "user_input": {"original": "q", "normalized": ""},
        "events": [], "refined_index": {}, "end_state": "open", "org_state": "",
    }
    agent.messages = []
    for i in range(60):
        event = truncate.make_event(
            f"R1-E{i:02d}", "tool_result",
            {"role": "tool", "tool_call_id": f"c{i}", "content": f"内容标记{i}" * 30},
            tool_name="read_file",
        )
        agent.current_round["events"].append(event)
        agent.messages.append(event["message"])

    msgs = agent._assemble_messages()
    joined = json.dumps(msgs, ensure_ascii=False)
    assert "内容标记0" in joined and "内容标记59" in joined  # 首尾都在
    assert "已折叠" not in joined


def test_file_map_lists_files_from_organized_rounds():
    """文件地图：已整理轮次里写/读过的文件以清单形式注入装配。"""
    from wovra import truncate

    agent = Agent(
        llm=_StubLLM([[_chunk(_delta(content="ok"))]]),
        tools=[],
    )
    write_call = {
        "role": "assistant", "content": "",
        "tool_calls": [{
            "id": "c1", "type": "function",
            "function": {
                "name": "write_file",
                "arguments": json.dumps({"path": "docs/a.md", "content": "x"}),
            },
        }],
    }
    agent.rounds = [
        {
            "seq": 1, "user_input": {"original": "写文档", "normalized": ""},
            "events": [truncate.make_event("R1-E01", "tool_call", write_call)],
            "refined_index": {}, "end_state": "completed", "org_state": "done",
        },
        {
            "seq": 2, "user_input": {"original": "继续", "normalized": ""},
            "events": [truncate.make_event(
                "R2-E01", "final_answer", {"role": "assistant", "content": "好"}
            )],
            "refined_index": {}, "end_state": "completed", "org_state": "done",
        },
    ]
    agent.current_round = None
    agent.messages = []

    msgs = agent._assemble_messages()
    joined = json.dumps(msgs, ensure_ascii=False)
    assert "docs/a.md" in joined
    assert "写于 R1" in joined


def test_current_round_folds_only_when_over_model_window():
    """唯一的天花板是模型窗口：估算超限才紧急折叠最老事件，正常任务碰不到。"""
    from wovra import truncate

    agent = _agent_with([])
    agent.rounds = []
    agent.current_round = {
        "seq": 1, "user_input": {"original": "q", "normalized": ""},
        "events": [], "refined_index": {}, "end_state": "open", "org_state": "",
    }
    agent.messages = []
    for i in range(6):
        # 每条 ~300 个 CJK 字 ≈ 300+ tok，6 条远超下面设置的窗口
        content = "数据" * 100 + f"标记{i}"
        event = truncate.make_event(
            f"R1-E{i:02d}", "tool_result",
            {"role": "tool", "tool_call_id": f"c{i}", "content": content},
            tool_name="read_file",
        )
        agent.current_round["events"].append(event)
        agent.messages.append(event["message"])
    agent.context_limit = 300  # 预算 = 90% = 270

    msgs = agent._assemble_messages()
    joined = json.dumps(msgs, ensure_ascii=False)
    assert "紧急折叠" in joined
    assert "标记5" in joined  # 最近的事件保留全量
    assert "标记0" not in joined  # 最老的折叠为索引行（索引行只有前 120 字）


def test_expand_history_full_returns_complete_content():
    """expand_history 不再截断："完整原文"的承诺必须兑现。"""
    from wovra.truncate import make_event

    big = "结果" * 5000  # 10000 字符
    agent = _agent_with([])
    agent.rounds = [{
        "seq": 1, "user_input": {"original": "q", "normalized": ""},
        "events": [make_event(
            "R1-E02", "tool_result",
            {"role": "tool", "tool_call_id": "c1", "content": big},
            tool_name="read_file",
        )],
        "refined_index": {}, "end_state": "completed", "org_state": "done",
    }]

    out = agent._read_full_event("R1-E02")

    assert big in out  # 全文返回，无 4KB 上限


def test_render_collapsed_keeps_live_blocks_only():
    """折叠视图：👤+🎯 + 当前仍 LIVE 的块摘要；其余块折叠并注明。"""
    r1 = _mk_file_round(1, "写两个文件", ["a.txt", "b.txt"])
    r2 = _mk_file_round(2, "删 b", ["b.txt"])
    agent = Agent(llm=_StubLLM(), tools=[])
    agent.rounds = [r1, r2]
    # 现场重算 v3 块 + ledger 推演（b.txt 被 R2 重写仍 live——改为删）
    from wovra import blocks as blocks_module
    from wovra import lifecycle as lifecycle_module
    ledger = lifecycle_module.FileLedger()
    # 让 b.txt 变 dead：直接构造 delete 事件太啰嗦，用 ledger 状态注入
    ledger.update(r1, blocks=blocks_module.segment_round_by_file(r1))
    # 模拟 b.txt 已死：手工把 ledger 条目改 dead
    b_entry = ledger.entries().get("b.txt")
    b_entry["state"] = "dead"
    r1["block_summaries"] = {
        "R1-B1": "a.txt 描述",
        "R1-B2": "b.txt 描述",
    }
    r1["user_input"]["normalized"] = "写两个文件"
    out = agent._render_collapsed(r1, ledger)
    assert out is not None
    assert "（折叠）" in out
    assert "👤 用户:" in out
    assert "🎯 意图: 写两个文件" in out
    assert "a.txt 描述" in out          # LIVE 块保留
    assert "b.txt 描述" not in out      # dead 块折叠
    assert "其余 1 块已折叠" in out


def test_assemble_collapses_oldest_generation():
    """装配：最近 3 代全量视图，第 1 代折叠（org_generation 判定）。"""
    rounds = []
    for seq in range(1, 9):
        rounds.append(_mk_file_round(seq, f"轮{seq}", [f"f{seq}.txt"]))
    for i, r in enumerate(rounds):
        r["org_state"] = "done"
        r["org_generation"] = i // 2 + 1   # 1,1,2,2,3,3,4,4 → 4 代
        r["block_summaries"] = {
            f"R{r['seq']}-B1": f"{r['seq']} 的块描述",
        }
    task = Task.create(goal="g")
    agent = Agent(llm=_StubLLM(), tools=[], task=task)
    agent.rounds = rounds
    msgs = agent._assemble_messages_impl()
    texts = [str(m.get("content") or "") for m in msgs]
    joined = "\n".join(texts)
    # 第 1 代（R1-R2）折叠
    assert "R1]（折叠）" in joined
    assert "R2]（折叠）" in joined
    # 第 2-4 代（R3-R8）全量视图（无折叠标记）
    assert "R3]（折叠）" not in joined
    assert "R4]（折叠）" not in joined
    assert "R8]（折叠）" not in joined
    # 折叠轮的块描述不出现（除了保留的 LIVE 块）；R1 的 f1.txt 全轮 live → 保留
    assert "1 的块描述" in joined


def test_expand_round_summary_shows_block_view():
    """expand_history summary 档优先显示块视图（折叠行的第一级展开）。"""
    r1 = _mk_file_round(1, "写文件", ["a.txt"])
    r1["org_state"] = "done"
    r1["block_summaries"] = {"R1-B1": "创建 a.txt：测试写入"}
    r1["user_input"]["normalized"] = "写一个文件"
    agent = Agent(llm=_StubLLM(), tools=[])
    agent.rounds = [r1]
    out = agent.expand_history("R1", level="summary")
    assert "块视图：" in out
    assert "R1-B1: 创建 a.txt：测试写入" in out
    # 第二级：full 档取回原文
    full = agent.expand_history("R1", level="full")
    assert "写文件" in full
