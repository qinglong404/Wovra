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


def test_compact_view_summaries_resolved_by_v3_blocks():
    """回归（2026-09-11 实测）：视图块结构必须现场重算 v3，与整理产物同源。

    真凶：`_render_compact` 曾遍历 `r["blocks"]`（close_round 落的 v1 粗分块：
    以写/改为截止，无写轮整轮一块）去 summaries 里查描述，而 summaries 的键
    是 v3 块号（按文件聚合）。两套分块器编号空间相同（R{n}-B{k}）但切法不同
    ——编号对不上的描述被**静默丢弃**，不报错。

    本用例复刻最吃亏的形状：**纯读轮**（无写操作）。v1 只切出 1 块，v3 按文件
    切出 3 块。实测本会话 R1 正是此类（24 个文件块、0 次写）：v1=1，24 条描述
    只有 1 条进上下文，另外 23 条永不显示——而"读源码调研"恰恰是最需要沉淀的
    轮次类型，这个偏差是系统性的、不是偶发。
    """
    task = Task.create(goal="x")
    events = [{"id": "R1-E01", "type": "user", "status": "", "truncated": "读三个文件",
               "message": {"role": "user", "content": "读三个文件"}}]
    for i, path in enumerate(("a.py", "b.py", "c.py"), start=2):
        events.append({
            "id": f"R1-E0{i}", "type": "tool_call", "status": "",
            "truncated": "调用 read_file",
            "message": {"role": "assistant", "tool_calls": [
                {"id": f"c{i}", "type": "function",
                 "function": {"name": "read_file",
                              "arguments": json.dumps({"path": path})}}]},
        })
    events.append({"id": "R1-E05", "type": "final_answer", "status": "",
                   "truncated": "读完了",
                   "message": {"role": "assistant", "content": "读完了"}})
    task.rounds = [{
        "seq": 1,
        "user_input": {"original": "读三个文件", "normalized": "通读三个模块"},
        "events": events,
        # v1 落盘：无写操作 → 整轮一块（这正是丢描述的成因）
        "blocks": [{"id": "R1-B1", "kind": "work", "start": 0,
                    "end": len(events) - 1,
                    "start_event": "R1-E01", "end_event": "R1-E05"}],
        # v3 产物：按文件聚合 → 三块，描述逐块给出
        "block_summaries": {
            "R1-B1": "通读 a.py，弄清入口与装配顺序",
            "R1-B2": "通读 b.py，确认工具 schema 生成方式",
            "R1-B3": "通读 c.py，理清维护管线的两阶段",
        },
        "refined_index": {}, "end_state": "completed", "org_state": "done",
    }]
    agent = Agent(llm=_StubLLM(), tools=[], task=task)
    _make_open_round(agent, 2, "继续")

    body = "\n".join(m.get("content", "") for m in agent._assemble_messages())

    assert "▸ R1-B1: " in body
    for bid, desc in (("R1-B2", "通读 b.py"), ("R1-B3", "通读 c.py")):
        assert f"▸ {bid}: " in body, f"{bid} 的块细节未进上下文（v1/v3 编号错位）"
    assert "通读 c.py，理清维护管线的两阶段" in body


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


def test_render_collapsed_keeps_file_list_only():
    """二级折叠（2026-09-12 用户拍板）：折叠档只留 👤+🎯+📌+**文件名清单**。

    块摘要不再进折叠档——它们只由最近 keep_min 代的紧凑档承载，更老的轮
    "要了再取"（expand_history 轮号 → summary 档 → full 档）。
    为什么改：旧实现保留"文件仍存活"的块完整摘要，而真实会话里多数文件长期
    live，于是折叠档**单调膨胀**——实测本会话 24 个折叠轮 46,782 tok，其中
    80%（33,938 tok）是块摘要，把装配地板顶到 101,826 tok、越过 100,000 水位
    线：压缩刚结束就已在触发线之上，冷却一到期立刻再压，压了等于没压。
    """
    r1 = _mk_file_round(1, "写两个文件", ["a.txt", "b.txt"])
    agent = Agent(llm=_StubLLM(), tools=[])
    agent.rounds = [r1]
    from wovra import blocks as blocks_module
    from wovra import lifecycle as lifecycle_module
    ledger = lifecycle_module.FileLedger()
    ledger.update(r1, blocks=blocks_module.segment_round_by_file(r1))
    # 模拟 b.txt 已死：直接构造 delete 事件太啰嗦，用 ledger 状态注入
    ledger.entries().get("b.txt")["state"] = "dead"
    r1["block_summaries"] = {
        "R1-B1": "a.txt 描述",
        "R1-B2": "b.txt 描述",
    }
    r1["user_input"]["normalized"] = "写两个文件"
    r1["user_input"]["key_constraints"] = "不要用 git"
    out = agent._render_collapsed(r1, ledger)
    assert out is not None
    assert "（折叠）" in out
    assert "👤 用户:" in out
    assert "🎯 意图: 写两个文件" in out
    assert "📌 关键约束: 不要用 git" in out
    # 文件名清单进折叠档，已删文件标状态（清单是"这轮碰过什么"的索引）
    assert "涉及文件：" in out
    assert "a.txt" in out and "b.txt（已删）" in out
    # 块摘要不再进折叠档（这是本轮修复的核心）
    assert "a.txt 描述" not in out
    assert "b.txt 描述" not in out
    assert "2 块细节已折叠" in out
    assert "expand_history" in out


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
    # 折叠轮不再携带块摘要（二级折叠，2026-09-12），只给文件名清单
    assert "1 的块描述" not in joined
    assert "涉及文件：f1.txt" in joined


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


# ---- expand_history 展开通道（2026-09-11 实测修复：此前从未真实展开过） ----


def test_expand_block_uses_live_v3_not_stale_persisted():
    """块展开认现场 v3 块号，不认持久化的旧 v1 编号。

    实测 103 轮里 42 轮两者编号不一致（v3 按文件聚合、v1 按写操作截止）：
    用持久化编号会"块号不存在"或取到别的块。整理产物的块号来自 v3，
    所以展开也必须以 v3 为准。
    """
    r = _mk_file_round(1, "写两个文件", ["a.txt", "b.txt"])
    # 持久化 blocks 是旧 v1 编号：只有 B1（模拟错位轮）
    r["blocks"] = [{
        "id": "R1-B1", "kind": "work", "start": 0, "end": 1,
        "start_event": "R1-E01", "end_event": "R1-E02",
        "touched_files": ["a.txt"], "wrote_files": ["a.txt"],
        "command_types": [],
    }]
    agent = Agent(llm=_StubLLM(), tools=[], task=Task.create(goal="x"))
    agent.rounds = [r]

    # v3 现场算出 a.txt / b.txt 两个块 → R1-B2 必须能展开
    out = agent.expand_history("R1-B2")
    assert "未找到块" not in out
    assert "b.txt" in out


def test_expand_merged_group_anchor_expands_all_members():
    """合并组锚点（紧凑视图里的 [R1-2]）可直接展开——逐轮拼出组内轮次。"""
    r1 = _round(1, "甲问题", "甲回答")
    r2 = _round(2, "乙问题", "乙回答")
    r1["merged_anchor"] = "R1-2"
    r2["merged_skip"] = "R1-2"
    agent = Agent(llm=_StubLLM(), tools=[], task=Task.create(goal="x"))
    agent.rounds = [r1, r2]

    out = agent.expand_history("R1-2", level="full")
    assert "甲问题" in out and "乙问题" in out
    out_summary = agent.expand_history("R1-2", level="summary")
    assert "甲问题" in out_summary and "乙问题" in out_summary


def test_expand_levels_are_distinct_for_organized_round():
    """三档确有区分：truncated=事件索引、summary=块视图、full=原文。"""
    r = _round(1, "写文件", "写好了")
    r["org_state"] = "done"
    r["block_summaries"] = {"R1-B1": "创建 a.txt：写入测试内容"}
    agent = Agent(llm=_StubLLM(), tools=[], task=Task.create(goal="x"))
    agent.rounds = [r]

    trunc = agent.expand_history("R1", level="truncated")
    summ = agent.expand_history("R1", level="summary")
    full = agent.expand_history("R1", level="full")

    assert "块视图" not in trunc and "[R1-E01]" in trunc      # 最粗：索引行
    assert "块视图" in summ and "创建 a.txt" in summ          # 中：块视图
    # 细：原文（user 事件在轮头已给，正文只列非 user 事件）
    assert "R1-E02" in full and "写好了" in full
    # 三档内容互不相同
    assert len({trunc, summ, full}) == 3


# ---- 协议补缝：检查点轮边界的 tool_call/tool_result 跨轮拆分 ------------


def _checkpoint_split_rounds():
    """检查点轮边界的两轮：R1 以 verify 的 tool_call 结尾，R2 以其结果开头。

    这是 verify_milestone = 轮边界的固有形态：tool_call 落在闭合一侧，
    tool 结果落在开启一侧。R1 已整理、R2 未整理时装配流会出现悬空 tool。
    """
    call_msg = {"role": "assistant", "content": "",
                "tool_calls": [{"id": "c1", "type": "function",
                                "function": {"name": "todo",
                                             "arguments": "{\"action\":\"verify_milestone\"}"}}]}
    result_msg = {"role": "tool", "tool_call_id": "c1", "content": "大步已验收"}
    r1 = {
        "seq": 1,
        "user_input": {"original": "干活", "normalized": "澄清：干活"},
        "events": [
            {"id": "R1-E01", "type": "user", "status": "", "truncated": "干活",
             "message": {"role": "user", "content": "干活"}},
            {"id": "R1-E02", "type": "tool_call", "status": "",
             "truncated": "todo(verify_milestone)", "message": call_msg},
        ],
        "refined_index": {}, "end_state": "completed", "org_state": "done",
    }
    r2 = {
        "seq": 2,
        "user_input": {"original": "[运行时] 大步验收通过，轮次在此闭合",
                       "normalized": ""},
        "events": [
            {"id": "R2-E01", "type": "tool_result", "status": "",
             "truncated": "大步已验收", "message": result_msg},
            {"id": "R2-E02", "type": "final_answer", "status": "",
             "truncated": "收尾",
             "message": {"role": "assistant", "content": "收尾"}},
        ],
        "refined_index": {}, "end_state": "completed", "org_state": "",
    }
    return r1, r2, call_msg


def _assert_no_dangling_tool(msgs):
    """协议不变量：任何 tool 消息的前一条必须是带 tool_calls 的 assistant。"""
    for i, m in enumerate(msgs):
        if m.get("role") == "tool":
            prev = msgs[i - 1] if i else None
            assert prev is not None and prev.get("role") == "assistant" \
                and prev.get("tool_calls"), (
                f"位置 {i} 的 tool 消息悬空（前一条："
                f"{(prev or {}).get('role')}）——严格端点会 400"
            )


def test_checkpoint_split_pair_survives_compact_boundary():
    """已整理轮以 tool_call 结尾 + 未整理轮以 tool 结果开头 → 补缝。

    紧凑视图不含 tool_calls 消息；不补则装配流 tool 悬空，DeepSeek 400
    （2026-09-12 用 Wovra 改 Wovra 会话实测）。"""
    r1, r2, call_msg = _checkpoint_split_rounds()
    task = Task.create(goal="x")
    task.rounds = [r1, r2]
    agent = Agent(llm=_StubLLM(), tools=[], task=task)
    _make_open_round(agent, 3, "继续")

    msgs = agent._assemble_messages()

    _assert_no_dangling_tool(msgs)
    # 补上的正是 R1 的收尾 tool_call 原文（不是合成物）
    assert any(m.get("tool_calls") == call_msg["tool_calls"] for m in msgs)


def test_checkpoint_split_at_current_round_boundary():
    """同款补缝在当前轮边界：上一已整理轮以 tool_call 结尾、当前轮以
    tool 结果开头（结果事件先进本轮，本轮尚无别的消息）。"""
    r1, _, _ = _checkpoint_split_rounds()
    task = Task.create(goal="x")
    task.rounds = [r1]
    agent = Agent(llm=_StubLLM(), tools=[], task=task)
    agent.current_round = {
        "seq": 2, "user_input": {"original": "[运行时] 大步验收通过",
                                 "normalized": ""},
        "events": [], "refined_index": {}, "end_state": "open", "org_state": "",
    }
    agent.rounds.append(agent.current_round)
    agent.messages = []
    agent._record_event("tool_result", {"role": "tool", "tool_call_id": "c1",
                                        "content": "大步已验收"})

    msgs = agent._assemble_messages()

    _assert_no_dangling_tool(msgs)
