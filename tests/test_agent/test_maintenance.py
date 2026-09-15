"""Agent 运行时测试（自 test_agent.py 拆分，2026-09-11）。

本模块：test_maintenance。"""

import json
from wovra import registry as registry_module
from wovra import task as task_module
from wovra import views as views_module
from wovra.agent import Agent
from wovra.task import Task, TaskState

# 共享夹具/工具（_helpers.py 是原文件的公共头部）
from ._helpers import *  # noqa: F401,F403

def test_grace_exempts_all_rounds_within_limit(monkeypatch, tmp_path):
    """宽限期（2026-09-09 用户拍板：3 轮全豁免，无事件数上限——提出
    3 轮时已考虑 229 步巨轮）：前 grace 轮内轻轮与巨型轮一律豁免。"""
    light = _round_agent(monkeypatch, tmp_path, n_events=5)
    light.close_round()
    assert light._org_queue.qsize() == 0  # 轻轮：宽限期豁免

    mega = _round_agent(monkeypatch, tmp_path, n_events=130)
    mega.close_round()
    assert mega._org_queue.qsize() == 0  # 巨型轮：同样豁免（用户确认）


def test_organization_updates_state_and_block_summaries(monkeypatch, tmp_path):
    """水位触发批量整理：State Patch 合并、意图/关键约束/逐块描述写回。"""
    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path)
    org_json = json.dumps({
        "rounds": [{
            "seq": 1,
            "normalized_user_input": "用户想搞清楚项目的测试覆盖情况",
            "key_constraints": "不得修改现有测试用例",
            "block_summaries": [
                {"id": "R1-B1",
                 "summary": "回应测试覆盖询问：说明当前 tests/ 下 5 个套件的覆盖面与"
                            "缺口（边界条件与异常分支未覆盖），给出按优先级排列的补测"
                            "建议清单；无文件改动，仅口头结论"},
            ],
        }],
        "state_patch": {
            "completed": ["梳理测试覆盖"],
            "current_status": "测试覆盖已梳理完成",
            "goal": "搞清测试覆盖",
            "is_done": True,
        },
    }, ensure_ascii=False)
    responses = [
        [_chunk(_delta(content="干完了"))],
        [_chunk(_delta(content=org_json))],
    ]
    task = Task.create(goal="初始的模糊想法")
    # org_watermark=0：每轮闭合即触发（等价旧逐轮行为），便于单测
    agent = Agent(llm=_StubLLM(responses), tools=[], task=task, org_watermark=0, org_grace_rounds=0, org_cooldown_rounds=0)

    answer = agent.run("把活干完")

    assert answer == "干完了"
    # 轮闭合即生效（§50 用户口径：「分裂之后、下一轮对话之前」就该落地）——
    # 故这里直接读**正式字段**；暂存区只在"有开放轮"时才留东西。
    assert task.rounds[-1]["user_input"]["normalized"] == "用户想搞清楚项目的测试覆盖情况"
    assert task.rounds[-1]["user_input"]["key_constraints"] == "不得修改现有测试用例"
    assert "R1-B1" in task.rounds[-1]["block_summaries"]
    # State Patch 增量合并进任务状态
    assert task.task_state["goal"] == "搞清测试覆盖"
    assert task.task_state["is_done"] is True
    assert task.task_state["completed"] == ["梳理测试覆盖"]
    # Round 结构持久化：意图 / 关键约束 / 逐块描述写回
    assert task.rounds[-1]["block_summaries"]["R1-B1"].startswith("回应测试覆盖询问")
    assert task.rounds[-1]["org_state"] == "done"
    assert "pending_org" not in task.rounds[-1]  # 生效后暂存区清空
    agent._promote_org_results()                 # 幂等：再调一次不改动
    assert task.rounds[-1]["org_state"] == "done"


def test_organization_survives_invalid_json(monkeypatch, tmp_path):
    """整理输出不是合法 JSON 时，保留原状态而不是覆盖坏数据。"""
    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path)
    responses = [
        [_chunk(_delta(content="回答"))],
        [_chunk(_delta(content="这不是 JSON {{{"))],
        [_chunk(_delta(content="重试了还是 {{{ 不是"))],  # 重试仍失败
    ]
    task = Task.create(goal="初始目标")
    agent = Agent(llm=_StubLLM(responses), tools=[], task=task, org_watermark=0, org_grace_rounds=0, org_cooldown_rounds=0)

    agent.run("问")

    assert task.task_state == {}  # 原状态未被破坏
    assert task.rounds[-1]["org_state"] == "failed"  # 整批失败，回入水位
    assert task.rounds[-1]["refined_index"] == {}
    assert task.rounds[-1]["user_input"]["normalized"] == ""


def test_organization_patch_ignores_invalid_fields(monkeypatch, tmp_path):
    """state_patch 里的非法字段被忽略，合法字段照常合并。"""
    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path)
    org_json = json.dumps({
        "rounds": [{"seq": 1, "normalized_user_input": "x"}],
        "round_summary": "s",
        "state_patch": {"completed": "不是列表", "current_status": "进行中"},
    }, ensure_ascii=False)
    responses = [
        [_chunk(_delta(content="回答"))],
        [_chunk(_delta(content=org_json))],
    ]
    task = Task.create(goal="目标")
    agent = Agent(llm=_StubLLM(responses), tools=[], task=task, org_watermark=0, org_grace_rounds=0, org_cooldown_rounds=0)

    agent.run("问")

    agent._promote_org_results()  # 模拟下一轮开启：暂存的补丁生效
    assert task.task_state.get("completed", []) == []  # 非法列表被忽略
    assert task.task_state.get("current_status") == "进行中"


def test_watermark_defers_organization_below_threshold(monkeypatch, tmp_path):
    """当前上下文窗口体量未达水位 → 不发起任何整理调用（小会话成本归零）。"""
    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path)
    monkeypatch.setattr("wovra.tokens.estimate", lambda text: len(text or "") // 4)
    task = Task.create(goal="x")
    task.rounds = [_round(1, "甲" * 2400, "甲" * 2400)]
    agent = Agent(
        llm=_StubLLM(), tools=[], task=task,
        org_watermark=5000,
    )
    agent.last_context_estimate = 100  # 装配峰值远低于水位

    agent._maybe_organize_batch()

    assert agent.llm.calls == []  # 一次整理都没发起
    assert task.rounds[0]["org_state"] == "done"  # _round 助手自带 done，未被动过


def test_watermark_triggers_single_batch_call(monkeypatch, tmp_path):
    """窗口体量达水位 → 所有未整理轮一次批量调用整理（N 次 → 1 次）。"""
    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path)
    monkeypatch.setattr("wovra.tokens.estimate", lambda text: len(text or "") // 4)
    task = Task.create(goal="x")
    task.rounds = [
        _round(1, "甲" * 2400, "甲" * 2400),
        _round(2, "乙" * 2400, "乙" * 2400),
    ]
    for r in task.rounds:
        r["org_state"] = ""
    agent = Agent(
        llm=_StubLLM([[_chunk(_delta(content=_batch_org_json([1, 2])))]]),
        tools=[], task=task, org_watermark=2000, org_grace_rounds=0, org_cooldown_rounds=0,
    )
    agent.last_context_estimate = 5000  # 装配峰值 ≥ 水位 → 触发
    old_normalized = task.rounds[0]["user_input"]["normalized"]  # 助手自带的旧值

    agent._maybe_organize_batch()

    assert len(agent.llm.calls) == 1  # 两轮只花一次调用
    # 追加式输入：装配原文在前，整理指令/分块地图追加在后
    prompt = "\n".join(
        str(m.get("content") or "") for m in agent.llm.calls[0]["messages"]
    )
    assert "[整理指令]" in prompt and "[分块地图]" in prompt
    assert "甲" * 100 in prompt  # 装配原文直接进输入，不是重建索引
    # 产物进暂存区，正式字段保持旧值（本轮装配保持原样）
    assert task.rounds[0]["pending_org"]["normalized"] == "R1 意图"
    assert task.rounds[1]["pending_org"]["normalized"] == "R2 意图"
    assert task.rounds[0]["user_input"]["normalized"] == old_normalized
    assert all(r["org_state"] == "done" for r in task.rounds)
    # 下一轮开启：暂存生效——各轮产物写回各自的 Round，补丁只应用一次
    agent._promote_org_results()
    assert task.rounds[0]["user_input"]["normalized"] == "R1 意图"
    assert task.rounds[1]["user_input"]["normalized"] == "R2 意图"
    assert task.task_state["goal"] == "批量目标"


def test_watermark_collects_all_unorganized(monkeypatch, tmp_path):
    """批次上限已删除（2026-09-09 用户拍板）：到水位一次性收编全部未整理
    轮，不再分批。"""
    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path)
    monkeypatch.setattr("wovra.tokens.estimate", lambda text: len(text or "") // 4)
    task = Task.create(goal="x")
    task.rounds = [
        _round(1, "甲" * 2400, "甲" * 2400),
        _round(2, "乙" * 2400, "乙" * 2400),
        _round(3, "丙" * 2400, "丙" * 2400),
    ]
    for r in task.rounds:
        r["org_state"] = ""
    agent = Agent(
        llm=_StubLLM([[_chunk(_delta(content=_batch_org_json([1, 2, 3])))]]),
        tools=[], task=task, org_watermark=2000, org_grace_rounds=0, org_cooldown_rounds=0,
    )
    agent.last_context_estimate = 5000

    agent._maybe_organize_batch()

    assert task.rounds[0]["org_state"] == "done"
    assert task.rounds[1]["org_state"] == "done"
    assert task.rounds[2]["org_state"] == "done"  # 全部收编，不分批
    prompt = "\n".join(str(m.get("content") or "") for m in agent.llm.calls[0]["messages"])
    assert "丙" * 100 in prompt  # 第三轮原文也在快照里

    # 收尾补整理（run 模式退出路径）：无剩余未整理轮，不产生新调用
    agent.organize_backlog()
    assert len(agent.llm.calls) == 1


def test_pending_backlog_collected_on_trigger(monkeypatch, tmp_path):
    """上次会话崩溃遗留的 pending 轮仍是"未整理"，下次触发一并收编。"""
    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path)
    monkeypatch.setattr("wovra.tokens.estimate", lambda text: len(text or "") // 4)
    task = Task.create(goal="x")
    pending = _round(1, "甲" * 2400, "甲" * 2400)
    pending["org_state"] = "pending"  # 崩溃遗留：从未整理完成
    task.rounds = [pending]
    agent = Agent(
        llm=_StubLLM([[_chunk(_delta(content=_batch_org_json([1])))]]),
        tools=[], task=task, org_watermark=1200,
        org_grace_rounds=0, org_cooldown_rounds=0,
    )
    agent.last_context_estimate = 1200  # 达到水位 → 触发

    agent._maybe_organize_batch()

    assert len(agent.llm.calls) == 1
    assert task.rounds[0]["org_state"] == "done"


def test_crash_leftover_pending_not_blocked_by_cooldown(monkeypatch, tmp_path):
    """冷却只认真正维护过（done / 本进程在飞）：崩溃遗留的 pending
    （上个进程维护线程被中断的半程状态）不占冷却——F 组实证：6 轮
    pending 续跑后 R7/R8 闭合被 last_maintained=6 的冷却连挡两轮，
    压缩迟迟不开始。"""
    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path)
    monkeypatch.setattr("wovra.tokens.estimate", lambda text: len(text or "") // 4)
    task = Task.create(goal="x")
    rounds = []
    for i in range(1, 9):
        r = _round(i, "甲" * 2400, "甲" * 2400)
        r["org_state"] = "pending" if i <= 6 else ""  # R1-R6 崩溃遗留
        rounds.append(r)
    task.rounds = rounds
    agent = Agent(
        llm=_StubLLM([[_chunk(_delta(content=_batch_org_json(list(range(1, 9)))))]]),
        tools=[], task=task, org_watermark=1200,
        org_grace_rounds=3, org_cooldown_rounds=3,
    )
    agent.last_context_estimate = 1200

    agent._maybe_organize_batch()  # 当前轮 seq=8：8-6=2 < 3，旧逻辑会被挡

    assert len(agent.llm.calls) == 1  # 崩溃遗留不占冷却，直接补整理
    assert all(r["org_state"] == "done" for r in task.rounds)


def test_inflight_pending_still_counts_for_cooldown(monkeypatch, tmp_path):
    """本进程在飞批次仍占冷却：维护期间轮闭合不重复触发。"""
    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path)
    monkeypatch.setattr("wovra.tokens.estimate", lambda text: len(text or "") // 4)
    task = Task.create(goal="x")
    rounds = []
    for i in range(1, 9):
        r = _round(i, "甲" * 2400, "甲" * 2400)
        r["org_state"] = "pending" if i <= 6 else ""
        rounds.append(r)
    task.rounds = rounds
    agent = Agent(
        llm=_StubLLM(), tools=[], task=task, org_watermark=1200,
        org_grace_rounds=3, org_cooldown_rounds=3,
    )
    agent._org_inflight.update(range(1, 7))  # 本进程在飞（async 已入队）
    agent.last_context_estimate = 1200

    agent._maybe_organize_batch()

    assert len(agent.llm.calls) == 0  # 冷却生效：在飞批次期间不重复触发
    assert task.rounds[0]["org_state"] == "pending"  # 未动


def test_state_patch_maintains_escalations_and_experiments(monkeypatch, tmp_path):
    """决策升级与待办实验是状态账本一等公民（机制五），落盘可重载。"""
    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path)
    task = Task.create(goal="x")
    task.apply_state_patch({
        "escalations": ["预期 60s 内可玩，实测走路失效"],
        "experiments": ["打开 index.html，确认方向键能移动"],
    })
    task.save()

    state = Task.load(task.id).get_state()
    assert state.escalations == ["预期 60s 内可玩，实测走路失效"]
    assert state.experiments == ["打开 index.html，确认方向键能移动"]
    assert "决策升级" in state.render()
    assert "待办实验" in state.render()
    assert isinstance(TaskState().escalations, list)


def test_baseline_replays_full_and_skips_organization():
    """对照组：全量原文回放（含过去的完整回答），且不做任何整理调用。"""
    task = Task.create(goal="x")
    task.rounds = [_round(1, "第一轮原始提问", "第一轮完整回答内容")]
    responses = [[_chunk(_delta(content="ok"))]]
    agent = Agent(llm=_StubLLM(responses), tools=[], task=task, context_mode="baseline")

    agent.run("再来")

    assert agent.last_stats["llm_calls"] == 1  # 只有干活调用，没有整理调用
    sent = agent.llm.calls[-1]["messages"]
    assert any("第一轮完整回答内容" in (m.get("content") or "") for m in sent)


def test_baseline_threshold_compaction(monkeypatch, tmp_path):
    """对照组：真实上下文体量（下一次请求的估算）达 80% × 窗口时压缩。"""
    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path)
    monkeypatch.setattr("wovra.tokens.estimate", lambda text: len(text or "") // 4)
    task = Task.create(goal="x")
    # 甲/乙可区分：R1 压缩后其原文应退出装配，R2 保留
    task.rounds = [_round(1, "甲" * 2400, "甲" * 2400), _round(2, "乙" * 2400, "乙" * 2400)]
    summary_text = "前两轮的压缩摘要：完成了若干工作。"
    responses = [
        [_chunk(_delta(content="第三轮的回答"))],  # 干活调用
        [_chunk(_delta(content=summary_text))],    # 触发后的压缩调用
    ]
    agent = Agent(
        llm=_StubLLM(responses), tools=[], task=task,
        context_mode="baseline", context_limit=1000,  # 80% = 800
    )

    # 水位 = R1+R2+R3 的内容估算 ≈ 1200+ ≥ 800 → 本轮闭合即触发
    agent.run("第三轮")

    assert task.baseline_summary == summary_text
    assert task.rounds[0].get("compacted") is True
    assert not task.rounds[1].get("compacted")  # 最近 2 轮保留原文

    # 装配：压缩摘要进入上下文，被压缩轮次的原文退出
    agent.current_round = None
    msgs = agent._assemble_messages()
    bodies = "\n".join(m.get("content", "") for m in msgs)
    assert "前两轮的压缩摘要" in bodies
    assert "甲" * 2400 not in bodies
    assert "乙" * 2400 in bodies


def test_baseline_compaction_ignores_billing_watermark(monkeypatch, tmp_path):
    """回归：水位按窗口占用口径，不再被计费口径虚增提前触发。

    实测（会话 3c87e1）：累计计费口径把水位推到真实上下文的 19 倍，
    真实上下文仅 3 万 tok 就被提前压缩。"""
    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path)
    monkeypatch.setattr("wovra.tokens.estimate", lambda text: len(text or "") // 4)
    task = Task.create(goal="x")
    task.rounds = [_round(1, "轻量对话", "轻量回答")]
    responses = [
        [_chunk(_delta(content="r2")), _chunk(usage=_usage(500_000, 10, 500_010))],
        [_chunk(_delta(content="r3")), _chunk(usage=_usage(500_000, 10, 500_010))],
    ]
    agent = Agent(
        llm=_StubLLM(responses), tools=[], task=task,
        context_mode="baseline", context_limit=1000,
    )

    agent.run("第二轮")
    agent.run("第三轮")  # 计费累计 100 万 ≥ 800，但真实上下文极小

    assert agent._baseline_prompt_used == 1_000_000  # 计费口径照记（成本记录）
    assert task.baseline_summary == ""  # 水位按内容估算 → 不触发压缩
    assert not any(r.get("compacted") for r in task.rounds)


def test_org_auto_repairs_unescaped_quotes(monkeypatch, tmp_path):
    """未转义的英文双引号能被**自动修复**，不消耗重发机会（零 LLM 成本）。

    2026-09-11 用户提问："这种转义，我不能用代码直接给替换了再解析吗？"
    ——答案是可以：判据是结构性的（引号右侧若非 `: , } ]` 即内容引号），
    并且**解析本身就是校验**，修不好就不采纳。这里钉住三件事：
    修好了 → 不重发（只要 2 次 LLM 调用）、产物照常落地、且留痕说明修过。
    """
    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path)
    # 真凶同款：长中文叙述里混进未转义的双引号 → JSON 提前断串
    broken = ('{"rounds": [{"seq": 1, "normalized_user_input": "于是"读到含'
              '\'不存在\'的文件"整块"}], "state_patch": {}}')
    org_chunk = _chunk(_delta(tool_calls=[
        _fragment(0, id="c1", name="submit_organization", arguments=broken),
    ]))
    task = Task.create(goal="目标")
    agent = Agent(llm=_StubLLM([[_chunk(_delta(content="好"))], [org_chunk]]),
                  tools=[], task=task, org_watermark=0, org_grace_rounds=0,
                  org_cooldown_rounds=0)

    agent.run("问")

    assert len(agent.llm.calls) == 2, "自动修复成功就不该再触发重发"
    assert task.rounds[-1]["org_state"] == "done"
    assert (task.rounds[-1]["user_input"]["normalized"]
            == "于是\"读到含'不存在'的文件\"整块")   # 引号作为内容被保留
    records = [e["detail"] for e in task.history if e["kind"] == "maintenance"]
    assert any("已自动修复" in d for d in records), f"修复未留痕：{records}"


def test_organization_retries_once_with_diagnosis(monkeypatch, tmp_path):
    """整理产物不可用时：**带诊断**重发一次（且只一次）。

    2026-09-11 用户澄清：当初的"不重试"是测试期用来逼出原因的手段，不是
    机制——失败应当"找原因、调整后再试"，而不是原封不动重发整份产物。
    这里锁三件事：重发确实发生、重发输入里带着失败诊断、绝不无限重试。
    """
    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path)
    # 真·歧义畸形：内容引号后紧跟逗号（`他说"好",然后`）——结构引号与内容引号
    # 在文本层面无法区分，自动修复修不了（见 _requote_json 的诚实边界），
    # 因此必须走"带诊断重发"这条恢复路径。
    broken = ('{"rounds": [{"seq": 1, "normalized_user_input": "他说"好",然后'
              '没了"}], "state_patch": {}}')
    repair_chunk = _chunk(_delta(tool_calls=[
        _fragment(0, id="c2", name="submit_organization", arguments=json.dumps({
            "rounds": [{"seq": 1, "normalized_user_input": "修正后的意图",
                        "key_constraints": "", "block_summaries": []}],
            "state_patch": {},
        }, ensure_ascii=False)),
    ]))
    responses = [
        [_chunk(_delta(content="干完了"))],
        [_chunk(_delta(tool_calls=[
            _fragment(0, id="c1", name="submit_organization", arguments=broken),
        ]))],
        [repair_chunk],  # 第二跳（带诊断）给出修正后的合法产物
    ]
    task = Task.create(goal="目标")
    agent = Agent(llm=_StubLLM(responses), tools=[], task=task,
                  org_watermark=0, org_grace_rounds=0, org_cooldown_rounds=0)

    agent.run("问")

    assert task.rounds[-1]["org_state"] == "done"
    assert task.rounds[-1]["user_input"]["normalized"] == "修正后的意图"

    # 注意：StubLLM 的 lane 只是"非分裂/非咨询"的默认值，干活调用也是 org——
    # 真正的整理调用按"消息里带 [整理指令]"识别
    org_calls = [
        c for c in agent.llm.calls
        if any("[整理指令]" in str(m.get("content") or "") for m in c["messages"])
    ]
    assert len(org_calls) == 2, "应恰好重发一次"
    repair_msgs = org_calls[1]["messages"]
    # 重发输入 = 原 messages 一字不动 + assistant(失败调用) + tool(诊断)
    assert repair_msgs[: len(org_calls[0]["messages"])] == org_calls[0]["messages"]
    hint = repair_msgs[-1]
    assert hint["role"] == "tool"
    assert "不是合法 JSON" in hint["content"]
    assert "位置" in hint["content"]          # 带出错位置
    assert "中文引号" in hint["content"]      # 带上防复发的具体调整
    history = [e["detail"] for e in task.history if e["kind"] == "maintenance"]
    assert any("带诊断重发一次" in d for d in history)


def test_organization_retry_is_bounded(monkeypatch, tmp_path):
    """重发仍失败 → 整批 failed，绝不无限重试（只重发一次）。"""
    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path)
    responses = [
        [_chunk(_delta(content="干完了"))],
        [_chunk(_delta(content="我觉得应该这样：blahblah"))],  # 非法产物
        [_chunk(_delta(content="还是 blahblah"))],             # 重发仍非法
    ]
    task = Task.create(goal="目标")
    agent = Agent(llm=_StubLLM(responses), tools=[], task=task,
                  org_watermark=0, org_grace_rounds=0, org_cooldown_rounds=0)

    agent.run("问")

    assert len(agent.llm.calls) == 3  # 干活 1 + 整理 1 + 带诊断重发 1，到此为止
    assert task.rounds[-1]["org_state"] == "failed"   # 整批失败，回入水位
    assert task.task_state == {}                      # 补丁未应用
    assert task.rounds[-1]["user_input"]["normalized"] == ""
    history = [e["detail"] for e in task.history if e["kind"] == "maintenance"]
    assert any("已带诊断重发一次仍失败" in d for d in history)


def test_org_submits_via_resident_tool(monkeypatch, tmp_path):
    """整理产物经常驻工具 submit_organization 提交：与工作对话同一 tools
    数组（序列化恒定，前缀缓存常骑），调用参数被直接解析进暂存区。"""
    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path)
    org_args = json.dumps({
        "rounds": [{
            "seq": 1,
            "normalized_user_input": "澄清的意图",
            "key_constraints": "禁止 git",
            "block_summaries": [{"id": "R1-B1", "summary": "完成某事：细节描述"}],
        }],
        "state_patch": {"completed": ["完成项"]},
    }, ensure_ascii=False)
    org_chunk = _chunk(_delta(tool_calls=[
        _fragment(0, id="c9", name="submit_organization", arguments=org_args),
    ]))
    responses = [
        [_chunk(_delta(content="干完了"))],
        [org_chunk],
    ]
    task = Task.create(goal="目标")
    agent = Agent(llm=_StubLLM(responses), tools=[], task=task, org_watermark=0, org_grace_rounds=0, org_cooldown_rounds=0)

    agent.run("问")

    assert len(agent.llm.calls) == 2
    # 工具契约（2026-09-11 缓存复议改定）：维护调用与工作调用使用**完全
    # 相同的 tools 数组**（tools 是前缀的一部分，数组一差分叉即整段未命中）。
    # 09-10 曾收窄为单一出口，实测代价是 org 首跳命中 0.4% ≈ 0.6 元/批，
    # 而收益（防跑偏）未成立——维护调用不执行工具，漂移只让这批没产物，
    # 且已有带诊断重发兜底。WOVRA_MAINT_NARROW_TOOLS=1 可切回收窄。
    maint_tools = [t["function"]["name"] for t in agent.llm.calls[1]["tools"]]
    work_tools = [t["function"]["name"] for t in agent.llm.calls[0]["tools"]]
    assert maint_tools == work_tools          # 恒定：与工作调用同序列化
    assert "submit_organization" in maint_tools
    assert "write_file" not in maint_tools    # 本 agent 未注册工作工具（tools=[]）
    agent._promote_org_results()
    assert task.rounds[-1]["user_input"]["key_constraints"] == "禁止 git"
    assert task.rounds[-1]["block_summaries"]["R1-B1"] == "完成某事：细节描述"
    assert task.task_state["completed"] == ["完成项"]


def test_org_tool_call_without_seq_matches_positionally(monkeypatch, tmp_path):
    """GLM 实测会整字段省略 seq：一一对应声明下按位置兜底匹配。"""
    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path)
    org_args = json.dumps({
        "rounds": [{  # 没有 seq 字段
            "normalized_user_input": "位置匹配的意图",
            "key_constraints": "",
            "block_summaries": [{"id": "R1-B1", "summary": "细节描述"}],
        }],
        "state_patch": {},
    }, ensure_ascii=False)
    org_chunk = _chunk(_delta(tool_calls=[
        _fragment(0, id="c9", name="submit_organization", arguments=org_args),
    ]))
    task = Task.create(goal="目标")
    agent = Agent(llm=_StubLLM([[_chunk(_delta(content="好"))], [org_chunk]]),
                  tools=[], task=task, org_watermark=0, org_grace_rounds=0, org_cooldown_rounds=0)

    agent.run("问")

    assert task.rounds[-1]["user_input"]["normalized"] == "位置匹配的意图"
    assert "R1-B1" in task.rounds[-1]["block_summaries"]


def test_org_partial_coverage_marks_missing_rounds_failed(monkeypatch, tmp_path):
    """产物只覆盖部分轮 → 缺产物的轮判 failed（回入水位），不得标 done。

    真凶（2026-09-11 实测）：`_organize_rounds` 原先无条件对**全批**打
    `org_state="done"`，而它只判 `staged == 0` 为失败。模型少输出一轮时
    （seq 匹配不上，位置兜底又要求项数相等），那一轮既无块描述又被标 done
    → 视图降级成事件索引，且因 done 永不再整理：**静默的质量损失**，
    report/maint 上都看不出来（账面是 100% 已整理）。

    这里复刻：批次 3 轮、模型只回 2 项 → R3 必须 failed（而非 done）。
    """
    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path)
    partial = json.dumps({
        "rounds": [
            {"seq": 1, "normalized_user_input": "意图1", "key_constraints": "",
             "block_summaries": [{"id": "R1-B1", "summary": "描述1"}]},
            {"seq": 2, "normalized_user_input": "意图2", "key_constraints": "",
             "block_summaries": [{"id": "R2-B1", "summary": "描述2"}]},
        ],
        "state_patch": {},
    }, ensure_ascii=False)
    org_chunk = _chunk(_delta(tool_calls=[
        _fragment(0, id="c1", name="submit_organization", arguments=partial),
    ]))
    stub = _StubLLM([[org_chunk]])
    task = Task.create(goal="部分覆盖")
    task.rounds = [_mk_file_round(s, f"第{s}轮", [f"f{s}.py"]) for s in (1, 2, 3)]
    for r in task.rounds:
        r["org_state"] = ""
    agent = Agent(llm=stub, tools=[], task=task)

    ok, _exchange = agent._organize_rounds(agent.rounds, base_messages=[])

    assert ok is True                                  # 有产物，不是整批失败
    states = {r["seq"]: r["org_state"] for r in agent.rounds}
    assert states == {1: "done", 2: "done", 3: "failed"}, states
    # 覆盖到的轮照常有产物（不因修复而丢）
    assert agent.rounds[0]["pending_org"]["normalized"] == "意图1"
    assert agent.rounds[2].get("pending_org") is None
    # 代次只打给真生效的轮——failed 轮下一批重做时不该带旧代次
    assert all("org_generation" not in r for r in agent.rounds if r["org_state"] == "failed")
    records = [e["detail"] for e in task.history if e["kind"] == "maintenance"]
    assert any("产物只覆盖 2/3 轮" in d and "R[3]" in d for d in records), records


def test_org_unusable_product_twice_marks_failed(monkeypatch, tmp_path):
    """两跳都拿不到可用产物（rounds 空）：整批 failed，不留半份暂存。"""
    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path)
    bad = json.dumps({"rounds": [], "state_patch": {}})
    org_chunk = _chunk(_delta(tool_calls=[
        _fragment(0, id="c9", name="submit_organization", arguments=bad),
    ]))
    task = Task.create(goal="目标")
    agent = Agent(llm=_StubLLM([[_chunk(_delta(content="好"))], [org_chunk], [org_chunk]]),
                  tools=[], task=task, org_watermark=0, org_grace_rounds=0, org_cooldown_rounds=0)

    agent.run("问")

    assert task.rounds[-1]["org_state"] == "failed"
    assert "pending_org" not in task.rounds[-1]


def test_org_failure_records_evidence(monkeypatch, tmp_path):
    """整理失败必须留现场（worklog-20260911.md §11）。

    19:43 那次失败只留下 `org=False` 一行：产物与原因都没留，事后定位
    真因只能重跑一次 213K token 的输入。分裂阶段早有同类留痕，整理路
    漏了——这里钉住"失败必须写清为什么"。
    """
    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path)
    # 复刻真凶这一类、但取**自动修复覆盖不到**的歧义样本（内容引号紧跟逗号）：
    # 修不了 → 重发 → 仍失败，用于钉住"失败必须留现场"。
    broken = (
        '{"rounds": [{"seq": 1, "normalized_user_input": "读源码", '
        '"key_constraints": "", "block_summaries": [{"id": "R1-B1", '
        '"summary": "于是他说"对",然后没了整块被打成幽灵"}]}], '
        '"state_patch": {}}'
    )
    org_chunk = _chunk(_delta(tool_calls=[
        _fragment(0, id="c9", name="submit_organization", arguments=broken),
    ]))
    task = Task.create(goal="目标")
    agent = Agent(llm=_StubLLM([[_chunk(_delta(content="好"))], [org_chunk], [org_chunk]]),
                  tools=[], task=task, org_watermark=0, org_grace_rounds=0,
                  org_cooldown_rounds=0)

    agent.run("问")

    assert task.rounds[-1]["org_state"] == "failed"
    records = [e["detail"] for e in task.history if e["kind"] == "maintenance"]
    evidence = [d for d in records if "无可用产物" in d]
    assert evidence, f"失败现场未落 history：{records}"
    text = evidence[0]
    assert "submit_organization 参数" in text
    assert "JSON 解析失败" in text and "位置" in text
    assert "现场" in text          # 出错位置附近的原文片段
    assert "已带诊断重发一次仍失败" in text  # 重发过、仍失败
    # 重发前那一次也要留痕（为什么触发了重发）
    assert any("带诊断重发一次" in d for d in records)


def test_org_accepts_repairable_json(monkeypatch, tmp_path):
    """轻量修复：尾随逗号、非法转义（\\'）、串内裸控制字符都该救回来。

    这三类是模型产物的常见畸形，修它们零成本、无歧义（未转义引号有
    歧义，不猜——按失败处理并留痕）。
    """
    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path)
    # 三类畸形同现：非法转义 \'、串内裸换行、尾随逗号
    repairable = '''{"rounds": [{"seq": 1, "normalized_user_input": "意图\\'带单引号\\'", "key_constraints": "", "block_summaries": [{"id": "R1-B1", "summary": "第一行
第二行"}]},], "state_patch": {},}'''
    org_chunk = _chunk(_delta(tool_calls=[
        _fragment(0, id="c9", name="submit_organization", arguments=repairable),
    ]))
    task = Task.create(goal="目标")
    agent = Agent(llm=_StubLLM([[_chunk(_delta(content="好"))], [org_chunk]]),
                  tools=[], task=task, org_watermark=0, org_grace_rounds=0,
                  org_cooldown_rounds=0)

    agent.run("问")

    assert task.rounds[-1]["org_state"] == "done"
    last = task.rounds[-1]
    assert last["user_input"]["normalized"] == "意图'带单引号'"
    assert "第二行" in last["block_summaries"]["R1-B1"]


def test_assembly_does_not_duplicate_last_closed_round(monkeypatch, tmp_path):
    """闭合后装配不得把"刚闭合的轮"再算一遍（worklog-20260911.md §11）。

    close_round 把 current_round 置空但不清理 self.messages，而
    _current_round_messages() 原先会直接返回残留的 self.messages——于是
    整理快照 497 条而非 422 条（实测差值恰为末轮事件数 75），每批白烧
    ~18K tok，组织器还会把末轮看两遍。
    """
    agent = _round_agent(monkeypatch, tmp_path, n_events=5)
    agent.messages = [{"role": "user", "content": "上一个轮的消息"}]
    agent.close_round()  # current_round → None，self.messages 残留

    assert agent.current_round is None
    assert agent._current_round_messages() == []
    assembled = agent._assemble_messages()
    assert all(m.get("content") != "上一个轮的消息" for m in assembled)


def test_protection_grace_and_cooldown(monkeypatch, tmp_path):
    """保护机制：会话前 N 轮硬豁免维护（宽限），两次维护之间**最少 N 轮
    不触发**（最小间隔，2026-09-12 用户口径更正）——适配大项目起点，
    窗口保底紧急折叠不受豁免（另一条线）。

    口径更正要点：原实现用 `<`，即"距上次维护满 N 轮就放行"，只安静
    N−1 轮（R4 整理完 → R5 挡、R6 挡、R7 放行），与用户说的"最少 3 轮内
    不触发"差一轮；且同一份代码里宽限用 `<=`（R1-R3 豁免、R4 才放行），
    两个门不等式方向不一致本属笔误。改用 `<=` 后：R4 整理完 → R5/R6/R7
    都挡、R8 才放行。
    """
    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path)

    def org_json(*seqs):
        return json.dumps({
            "rounds": [{"seq": s, "normalized_user_input": f"R{s} 意图",
                        "key_constraints": "", "block_summaries": []} for s in seqs],
            "state_patch": {},
        }, ensure_ascii=False)

    responses = [
        [_chunk(_delta(content="R1"))],                    # R1（宽限）
        [_chunk(_delta(content="R2"))],                    # R2（宽限）
        [_chunk(_delta(content="R3"))],                    # R3（宽限）
        [_chunk(_delta(content="R4"))],                    # R4
        [_chunk(_delta(content=org_json(4)))],             # R4 触发（宽限外首次）
        [_chunk(_delta(content="R5"))],                    # R5（间隔 1<=3 挡）
        [_chunk(_delta(content="R6"))],                    # R6（间隔 2<=3 挡）
        [_chunk(_delta(content="R7"))],                    # R7（间隔 3<=3 挡）
        [_chunk(_delta(content="R8"))],                    # R8
        [_chunk(_delta(content=org_json(5, 6, 7, 8)))],    # R8 触发（间隔 4 达标）
    ]
    task = Task.create(goal="目标")
    agent = Agent(llm=_StubLLM(responses), tools=[], task=task,
                  org_watermark=0, org_grace_rounds=3, org_cooldown_rounds=3)

    for i in range(1, 9):
        agent.run(f"轮{i}")

    org_calls = [c for c in agent.llm.calls if c.get("lane") == "org"
                 and "[整理指令]" in str(c["messages"][-1].get("content", ""))]
    assert len(org_calls) == 2                       # R4、R8 两次
    # 宽限期的证据是调用时点（R1-R3 闭合时零维护调用）；R4 批次把它们
    # 一并收编属正常语义，故只断言 R1 无产物
    assert task.rounds[0]["user_input"]["normalized"] == ""
    assert task.rounds[3]["org_state"] == "done"     # R4 整理
    assert task.rounds[7]["org_state"] == "done"     # R8 批次补齐 R5-R8
    assert task.rounds[4]["user_input"]["normalized"] == "R5 意图"
    # 中间三轮在 R5/R6/R7 闭合时都被最小间隔挡住——它们的产物只能来自
    # R8 那次批次（这正是"最少 3 轮内不触发"的直接证据：若沿用旧的 `<`，
    # R7 闭合就会触发第三批，org_calls 会是 3 次）。§50 之后产物在 R8 闭合
    # 时即生效，故这里读**已生效**的字段，而不是暂存区。
    for r in task.rounds[4:7]:
        assert r["org_state"] == "done"                    # 由 R8 批次补齐
        assert r["user_input"]["normalized"] != ""         # 产物已落地


def test_split_lane_stages_domains_in_parallel(monkeypatch, tmp_path):
    """水位维护双路并行：整理 + 分裂分析同一快照、同 tools；分裂产物
    （现状清单/归属/可分性）暂存批首轮，promote 后落正式字段。"""
    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path)
    domains_args = json.dumps({
        "domains": [{
            "name": "web 演示项目",
            "file_domains": ["index.html", "js/"],
            "constraints": ["不用 git"],
            "goal": "演示页",
            "block_ids": ["R1-B1"],
            "superseded": [{"block_ids": ["R1-B1"], "note": "演示取代标注"}],
        }],
        "unassigned": {"block_ids": [], "reason": "无"},
        "split_assessment": {"splittable": False, "reason": "单一活性文件域"},
    }, ensure_ascii=False)
    domains_chunk = _chunk(_delta(tool_calls=[
        _fragment(0, id="d1", name="submit_domains", arguments=domains_args),
    ]))
    org_json = json.dumps({
        "rounds": [{"seq": 1, "normalized_user_input": "意图",
                    "key_constraints": "", "block_summaries": []}],
        "state_patch": {},
    }, ensure_ascii=False)
    task = Task.create(goal="目标")
    task.rounds = [_round(1, "第一轮", "答案")]
    task.rounds[0]["org_state"] = ""
    agent = Agent(
        llm=_StubLLM([[_chunk(_delta(content=org_json))]], split_responses=[[domains_chunk]]),
        tools=[], task=task, org_watermark=0, org_grace_rounds=0, org_cooldown_rounds=0,
    )
    agent.last_context_estimate = 5000

    agent._maybe_organize_batch()

    # 工具契约（2026-09-11 缓存复议改定）：org/split 都使用与工作调用
    # **相同**的 tools 数组。收窄模式只在 env 开关打开时生效。
    org_calls = [c for c in agent.llm.calls if c.get("lane") == "org"]
    split_calls = [c for c in agent.llm.calls if c.get("lane") == "split"]
    assert org_calls and split_calls
    assert [t["function"]["name"] for t in org_calls[0]["tools"]] != [
        "submit_organization"
    ]
    assert [t["function"]["name"] for t in split_calls[0]["tools"]] == [
        t["function"]["name"] for t in org_calls[0]["tools"]
    ]
    r1 = task.rounds[0]
    assert r1["org_state"] == "done"
    assert r1["pending_org"]["domains"][0]["name"] == "web 演示项目"
    # **分裂主体**在提单时落笔（worklog §64）：异步维护跨轮完成时，promote
    # 那一刻的"本轮视图"早已换人，不能拿它当分裂主体
    assert "split_parent" in r1["pending_org"]
    # A 步（2026-09-15）：展示类字段（split_assessment）产物就绪即落到轮上
    assert (r1.get("split_assessment")
            or r1["pending_org"].get("split_assessment"))["splittable"] is False
    agent._promote_org_results()
    assert r1["domains"][0]["name"] == "web 演示项目"
    assert r1["split_assessment"]["splittable"] is False


def test_split_lane_failure_independent(monkeypatch, tmp_path):
    """分裂路挂了（无预备响应 → 异常）：整理产物照常落地，无 domains 残留。"""
    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path)
    org_json = json.dumps({
        "rounds": [{"seq": 1, "normalized_user_input": "意图",
                    "key_constraints": "", "block_summaries": []}],
        "state_patch": {},
    }, ensure_ascii=False)
    task = Task.create(goal="目标")
    task.rounds = [_round(1, "第一轮", "答案")]
    task.rounds[0]["org_state"] = ""
    agent = Agent(llm=_StubLLM([[_chunk(_delta(content=org_json))]]),
                  tools=[], task=task, org_watermark=0, org_grace_rounds=0, org_cooldown_rounds=0)
    agent.last_context_estimate = 5000

    agent._maybe_organize_batch()

    r1 = task.rounds[0]
    assert r1["org_state"] == "done"
    assert r1["pending_org"]["normalized"] == "意图"   # 整理产物在
    assert "domains" not in r1["pending_org"]           # 分裂路无残留


def test_split_instruction_carries_chat_share_fact(monkeypatch, tmp_path):
    """分裂指令带残留桶占比事实（零 LLM 体量）：模型据此执行
    15% 门槛——纯对话块 ≤15% 不构成顶层节点、不拆出。"""
    agent, task = _split_fixture(
        monkeypatch, tmp_path,
        org_pool=[[_chunk(_delta(content=_org_json()))]],
        split_pool=[[_domains_chunk()]],
    )

    agent._maybe_organize_batch()

    split_calls = [c for c in agent.llm.calls if c.get("lane") == "split"]
    assert split_calls
    prompt = "\n".join(
        str(m.get("content") or "") for m in split_calls[0]["messages"]
    )
    assert "纯对话块（无文件交互，闲聊）内容占比" in prompt
    assert "%" in prompt  # 占比数值（本批内容量级由事件字符数估算）


def test_chat_block_share_counts_fallback_only():
    """残留桶占比只算 fallback（纯对话）块：文件工作块不摊进闲聊占比。"""
    from wovra import blocks as blocks_module

    agent = Agent(llm=_StubLLM(), tools=[])

    def _ev(eid, etype, content, tool_call=None):
        message = {"role": "user" if etype == "user" else
                   ("assistant" if etype != "tool_result" else "tool"),
                   "content": content}
        if tool_call:
            message["tool_calls"] = [{"id": f"c{eid}", "function": tool_call}]
        return {"id": eid, "type": etype, "status": "", "truncated": "",
                "message": message}

    chat_round = {"seq": 1, "user_input": {"original": "u", "normalized": ""},
                  "events": [_ev("R1-E01", "user", "u"),
                             _ev("R1-E02", "final_answer", "聊" * 200)]}
    tiny_chat = {"seq": 1, "user_input": {"original": "u", "normalized": ""},
                 "events": [_ev("R1-E01", "user", "u"),
                            _ev("R1-E02", "final_answer", "好")]}
    file_round = {"seq": 2, "user_input": {"original": "hi", "normalized": ""},
                  "events": [_ev("R2-E01", "user", "hi"),
                             _ev("R2-E02", "tool_call", "",
                                 {"name": "write_file",
                                  "arguments": '{"path": "a.py"}'}),
                             _ev("R2-E03", "tool_result", "内容" * 300)]}

    rbs = {r["seq"]: blocks_module.segment_round_by_file(r)
           for r in (chat_round, file_round)}
    # 纯聊天轮：全部是 fallback → 占比 ≈ 100%
    share_chat = agent._chat_block_share([chat_round], rbs)
    assert share_chat > 0.9
    # 文件工作轮 + 极小聊天轮：文件块不摊进占比 → 残留桶占比很小
    share_work = agent._chat_block_share([tiny_chat, file_round], rbs)
    assert 0.0 < share_work < 0.15


def test_extract_domains_accepts_empty_domains():
    """判不可分时空 domains 是合法结果，不能被当无产物丢弃（2026-09-11
    实测：合法的"不可分"响应被当失败，分裂静默落空）。"""
    agent = Agent(llm=_StubLLM(), tools=[])
    args = json.dumps({
        "domains": [],
        "unassigned": {"block_ids": ["R1-B1"], "reason": "纯聊天"},
        "split_assessment": {"splittable": False, "reason": "全链单子"},
    })
    product = agent._extract_domains(
        "", [{"name": "submit_domains", "arguments": args}]
    )
    assert product is not None
    domains, unassigned, split = product
    assert domains == [] and split["splittable"] is False
    assert unassigned["block_ids"] == ["R1-B1"]
    # 截断到空壳 {} 能解析成功，但三键全无——那是无产物，不是空域合法
    assert agent._extract_domains(
        "", [{"name": "submit_domains", "arguments": "{}"}]
    ) is None


def test_split_degraded_fallback_when_product_unusable(monkeypatch, tmp_path):
    """submit_domains 参数解析失败（两跳都坏）：不静默落空——保守"不可分"
    落档 + 留痕，并写明已带诊断重发一次仍失败。"""
    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path)
    bad = _chunk(_delta(tool_calls=[
        _fragment(0, id="d1", name="submit_domains",
                  arguments='{"domains": [{"name": "x"'),
    ]))
    agent, task = _split_fixture(
        monkeypatch, tmp_path,
        org_pool=[[_chunk(_delta(content=_org_json()))]],
        split_pool=[[bad], [bad]],   # 首次 + 带诊断重发，两次都坏
    )
    agent._maybe_organize_batch()
    sa = task.rounds[0]["pending_org"]["split_assessment"]
    assert "splittable" not in sa            # 2026-09-14：模型侧无判定字段
    assert "无可用产物" in sa["reason"]
    assert "已带诊断重发一次仍失败" in sa["reason"]
    assert any(
        "带诊断重发一次" in e.get("detail", "") for e in task.history
        if e.get("kind") == "maintenance"
    )
    assert any(
        "无可用产物" in e.get("detail", "") and "重发一次仍失败" in e.get("detail", "")
        for e in task.history if e.get("kind") == "maintenance"
    )


def test_split_retries_once_with_diagnosis(monkeypatch, tmp_path):
    """分裂阶段的带诊断重发（与 org 对称，2026-09-11）：首次产物不可用 →
    尾部追加失败现场重发一次；成功则分析继续，且**只重发一次**。

    重发输入必须是首次输入的**严格追加**（前缀逐字一致）——否则这次调用
    骑不到任何缓存，第二跳比重新整理还贵。
    """
    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path)
    bad = _chunk(_delta(tool_calls=[
        _fragment(0, id="d1", name="submit_domains",
                  arguments='{"domains": [{"name": "x"'),
    ]))
    good = _chunk(_delta(tool_calls=[
        _fragment(0, id="d2", name="submit_domains",
                  arguments=_DOMAINS_ARGS),
    ]))
    agent, task = _split_fixture(
        monkeypatch, tmp_path,
        org_pool=[[_chunk(_delta(content=_org_json()))]],
        split_pool=[[bad], [good]],
    )
    agent._maybe_organize_batch()

    # 产物生效（重发成功 → 分析继续）
    assert task.rounds[0]["pending_org"]["domains"][0]["name"] == "web 演示"
    # 恰好两次 split 调用（首次 + 重发一次，不多不少）
    split_calls = [c for c in agent.llm.calls if c.get("lane") == "split"]
    assert len(split_calls) == 2
    # 重发输入 = 首次输入 + 尾部追加（前缀逐字一致 → 骑满缓存）
    first, second = split_calls[0]["messages"], split_calls[1]["messages"]
    assert second[:len(first)] == first
    assert len(second) > len(first)
    # 诊断里带失败现场与"重发"要求
    tail = second[-1]
    assert "submit_domains 参数" in tail["content"]
    assert "不要使用英文双引号" in tail["content"]
    # 工具契约（2026-09-11 缓存复议）：重发同样用恒定数组（前缀一致才能
    # 骑上首跳建立的缓存）
    assert [t["function"]["name"] for t in split_calls[1]["tools"]] == [
        t["function"]["name"] for t in split_calls[0]["tools"]
    ]
    # 留痕：发起重发 + 重发可用
    details = [e.get("detail", "") for e in task.history
               if e.get("kind") == "maintenance"]
    assert any("split：产物不可用，带诊断重发一次" in d for d in details)
    assert any("split：重发产物可用" in d for d in details)


def test_org_instruction_carries_format_discipline(monkeypatch, tmp_path):
    """护栏前移（2026-09-11 机制评审）：英文双引号约束写进**常规**整理
    指令（从"治"变"防"），而不只在失败后的重发提示里。

    实测根因（worklog §11.2）：长中文叙述里混进未转义 ASCII 双引号会截断
    JSON 串。此前只在重发时提示——防的成本只有一行，能少一次畸形产物。
    """
    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path)
    org_json = _org_json()
    task = Task.create(goal="目标")
    # async_organization=False：同步整理，调用记录确定（便于断言）
    agent = Agent(llm=_StubLLM([[_chunk(_delta(content="好"))],
                                [_chunk(_delta(content=org_json))]]),
                  tools=[], task=task, org_watermark=0, org_grace_rounds=0,
                  org_cooldown_rounds=0, async_organization=False)

    agent.run("问")

    # 注意：替身 LLM 的 lane 字段只是"非分裂非咨询"的默认值，不能当整理
    # 调用的判据——必须按消息内容（[整理指令]）识别。
    org_calls = [
        c for c in agent.llm.calls
        if any("[整理指令]" in str(m.get("content") or "")
               for m in c["messages"])
    ]
    assert org_calls, "应有一次整理调用"
    instruction = org_calls[0]["messages"][-1]["content"]
    assert "[格式纪律]" in instruction
    assert "不要使用英文双引号" in instruction
    assert "中文引号" in instruction


def test_split_auto_assigns_blocks_by_file_domain(monkeypatch, tmp_path):
    """块的文件 ∈ 域 file_domains → Runtime 机械归入（模型可省 block_ids，
    大幅缩小输出防截断）。纯聊天块不自动归域。"""
    from wovra import blocks as blocks_module

    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path)
    domains_args = json.dumps({
        "domains": [{
            "name": "后端",
            "description": "服务端逻辑",
            "file_domains": ["src/a.py"],
            "block_ids": [],
        }],
        "unassigned": {"block_ids": [], "reason": ""},
        "split_assessment": {"splittable": True, "reason": "可拆"},
    }, ensure_ascii=False)
    chunk = _chunk(_delta(tool_calls=[
        _fragment(0, id="d1", name="submit_domains", arguments=domains_args),
    ]))
    task = Task.create(goal="目标")
    # 文件轮（src/a.py 的 file 块）+ 纯聊天轮（fallback 块，轮头外的
    # 最终回答承载）——用户事件不入块（轮头承载，见 segment_round_by_file）
    task.rounds = [
        _mk_file_round(1, "写文件", ["src/a.py"]),
        _round(2, "闲聊", "好"),
    ]
    for r in task.rounds:
        r["org_state"] = ""
    agent = Agent(
        llm=_StubLLM([[_chunk(_delta(content=_org_json()))]],
                     split_responses=[[chunk]]),
        tools=[], task=task, org_watermark=0, org_grace_rounds=0,
        org_cooldown_rounds=0,
    )
    agent.last_context_estimate = 5000
    agent._maybe_organize_batch()

    blocks = blocks_module.segment_round_by_file(task.rounds[0])
    file_bid = next(b["id"] for b in blocks if b.get("file") == "src/a.py")
    chat_bid = blocks_module.segment_round_by_file(task.rounds[1])[0]["id"]
    doms = task.rounds[0]["pending_org"]["domains"]
    assert file_bid in doms[0]["block_ids"]       # 文件块自动归域
    assert chat_bid not in doms[0]["block_ids"]   # 纯聊天块不自动归域
    bucket = [d for d in doms if d.get("main_agent")]
    assert bucket and chat_bid in bucket[0]["chat_block_ids"]  # 闲聊→主 agent 桶节点


def test_split_auto_claims_file_without_domain(monkeypatch, tmp_path):
    """文件没被域认领 → **Runtime 机械归位**，不再整批中止（2026-09-15 用户授权：
    "你看着按最好的来，我只要效果，可以和之前冲突的方法实现"）。

    旧口径（2026-09-13）"主 agent 不许有文件 → 直接报错中止"在实测里变成了空转：
    会话 20260915-131130-010611 两批分裂 27/29 个文件全未认领（模型把路径写成
    项目自己的视角/漏前缀），每闭合一轮再来一次、再失败一次，烧掉 134 万 prompt
    tok 一个域都没长出来。现在改成：**最近的节点接手 + 留痕**——文件仍然
    **绝不落主 agent**（原口径的实质保留），产物照常暂存。

    判据仍是 `views.ownership` 的同一套（文件集合匹配；匹配不到时跟本轮
    `active_view` 走）；这里让轮**没有**归属、文件**也没人认领**。
    """
    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path)
    # 域只认领 src/other.py —— 轮里写的 src/a.py 没人要
    domains_args = json.dumps({
        "domains": [{
            "name": "后端",
            "description": "服务端逻辑",
            "file_domains": ["src/other.py"],
            "block_ids": [],
        }],
        "unassigned": {"block_ids": [], "reason": ""},
        "split_assessment": {"splittable": True, "reason": "可拆"},
    }, ensure_ascii=False)
    chunk = _chunk(_delta(tool_calls=[
        _fragment(0, id="d1", name="submit_domains", arguments=domains_args),
    ]))
    task = Task.create(goal="目标")
    task.rounds = [_mk_file_round(1, "写文件", ["src/a.py"])]
    for r in task.rounds:
        r["org_state"] = ""
        r["active_view"] = ""            # 本轮也没归域 → 兜底不成立，是真漏认领
    agent = Agent(
        llm=_StubLLM([[_chunk(_delta(content=_org_json()))]],
                     split_responses=[[chunk]]),
        tools=[], task=task, org_watermark=0, org_grace_rounds=0,
        org_cooldown_rounds=0,
    )
    agent.last_context_estimate = 5000
    agent._maybe_organize_batch()

    # ① 留痕：自动归位写进账（谁被挂到哪），不再是"中止"
    entries = [str(e.get("detail") or "") for e in (task.history or [])
               if e.get("kind") == "maintenance"]
    assert any("自动归位" in x and "src/a.py" in x for x in entries), entries[-3:]
    assert not any("中止" in x for x in entries), entries[-3:]
    # ② 产物照常暂存：src/a.py 挂进「后端」（同目录 src/other.py 在那儿），
    #    绝不落主 agent；轮不再标 failed
    assert task.rounds[0]["org_state"] == "done"
    doms = (task.rounds[0].get("pending_org") or {}).get("domains") or []
    assert doms, "产物必须暂存"
    back = next(d for d in doms if d.get("name") == "后端")
    assert "src/a.py" in (back.get("files") or [])
    assert all(not d.get("main_agent") or "src/a.py" not in (d.get("files") or [])
               for d in doms)
    assert not any((d.get("files") or []) and d.get("main_agent") for d in doms)


def test_registry_entry_description_falls_back_mechanically():
    """没写描述的节点 → 注册表条目用「名字+文件清单」机械兜底（不许出现"（无描述）"）。

    指令现在只要求**顶层节点**写描述（产物太长会撞端点输出预算被截断——实测一次
    13,523 字符的产物整批作废）。路由靠职责表，所以其余节点必须有兜底文本。
    """
    from wovra import registry as registry_module

    domains = [
        {"name": "协议线", "description": "负责协议规格与编解码", "files": ["a.hpp"]},
        {"name": "实现线", "files": ["b.cpp", "c.cpp"]},          # 无描述
    ]
    entries = registry_module.build_entries(domains, "")
    by_name = {str(e.get("name")): str(e.get("description")) for e in entries}
    assert by_name["协议线"] == "负责协议规格与编解码"
    assert by_name["实现线"].startswith("实现线：维护 ")
    assert "b.cpp" in by_name["实现线"]
    assert all(d and d != "（无描述）" for d in by_name.values())


def test_runtime_auto_buckets_do_not_become_agents(monkeypatch, tmp_path):
    """Runtime 自动归类的桶**不生成子 agent**（2026-09-15）。

    它们是"没人认领的文件按目录先接住"的机械桶（`_auto_claim` 建的、带
    `runtime_auto=True`）。让它们参与 `build_entries` 的选层会凭空多出几个
    子 agent——实测一次瘦身实验里 4 个「cpp（Runtime 自动归类）」之类的桶各占
    一个 agent，把用户满意的"不过分分裂"直接破坏。文件覆盖不受影响（覆盖检查
    按 domains 算，与 entries 无关）。
    """
    from wovra import registry as registry_module

    domains = [
        {"name": "协议线", "description": "协议", "files": ["a.hpp"]},
        {"name": "cpp（Runtime 自动归类）", "description": "机械桶",
         "runtime_auto": True, "files": ["x.cpp", "y.cpp"]},
        {"name": "tests（Runtime 自动归类）", "description": "机械桶",
         "runtime_auto": True, "files": ["t.py"]},
    ]
    entries = registry_module.build_entries(domains, "")
    names = [str(e.get("name")) for e in entries]
    assert "协议线" in names
    assert not any("自动归类" in n for n in names), names
    # 覆盖不丢：那些文件仍在 domains 的文件集合里（覆盖检查的口径）
    merged, added, _u, _s = registry_module.project_merge([], domains)
    assert "协议线" in [e.get("name") for e in merged]


def test_extract_domains_salvages_truncated_call(monkeypatch, tmp_path):
    """截断产物经**提取路径**也能用（并留一条抢救痕迹）——不是只测静态函数。"""
    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path)
    task = Task.create(goal="目标")
    agent = Agent(llm=_StubLLM([[]]), tools=[], task=task)
    truncated = ('{"domains": [{"name": "A 线", "files": ["L00"]},'
                 ' {"name": "B 线", "files": ["L01"], "desc')
    product = agent._extract_domains(
        "", [{"name": "submit_domains", "arguments": truncated}])
    assert product is not None
    domains, _unassigned, _split = product
    assert [d.get("name") for d in domains] == ["A 线"]
    joined = "\n".join(str(h.get("detail")) for h in task.history)
    assert "抢救" in joined, joined[-3:]


def test_stream_call_retries_on_endpoint_throttle(monkeypatch):
    """端点限流（突发保护）→ **退避重试**，不再一次失败就让整批白算。

    实测 2026-09-09：一批整理整批死在 "System protection triggered by
    request burst" 上（当时没有任何重试）。

    另钉住计时口径（2026-09-15 用户报"TTFT 虚大"）：退避的 sleep **不进**
    ttft/dur——此前 start 在重试循环之外，一次重试就把首字延迟抬高 15s
    （两次 45s），把六格的 TTFT 均值抬飞。
    """
    from wovra.agent import core as core_module
    waits: list[float] = []
    monkeypatch.setattr(core_module.time, "sleep", lambda s: waits.append(s))
    monkeypatch.setattr(core_module.time, "monotonic", lambda: 1_000.0)  # 冻结时钟

    class _ThrottleLLM:
        model = "stub"

        def __init__(self, failures, message):
            self.failures = failures
            self.message = message
            self.calls = 0

        def chat(self, messages, tools=None, stream=True, **kwargs):
            self.calls += 1
            if self.calls <= self.failures:
                raise RuntimeError(self.message)
            return iter([_chunk(_delta(content="好"))])

    burst = ("System protection triggered by request burst. Please slow down "
             "traffic growth and increase requests gradually before retrying.")
    llm = _ThrottleLLM(2, burst)
    agent = Agent(llm=llm, tools=[], task=None)
    content, _tcs, _usage = agent._stream_call([{"role": "user", "content": "hi"}])
    assert content == "好" and llm.calls == 3          # 两次限流后成功
    assert waits == [15.0, 30.0]                       # 退避 15s/30s（没真睡）
    # 冻结时钟下：ttft/dur 只反映成功那次尝试（= 0s），不含 45s 的退避等待
    assert agent.last_stats["ttft_max"] == 0.0
    assert agent.last_stats["ttft_seconds"] == 0.0

    # 非限流错误：立刻抛，不许把真 bug 当限流吞掉
    llm2 = _ThrottleLLM(1, "ValueError: boom")
    agent2 = Agent(llm=llm2, tools=[], task=None)
    try:
        agent2._stream_call([{"role": "user", "content": "hi"}])
        raise AssertionError("非限流错误必须立刻抛")
    except RuntimeError as error:
        assert "boom" in str(error) and llm2.calls == 1
        assert waits == [15.0, 30.0]                   # 非限流：一次都不退避


def test_split_salvages_truncated_product():
    """产物被**截断**时抢救出写完的部分（2026-09-15）。

    分裂/整理产物动辄一两万字符，模型输出撞端点预算就被截断 → `json.loads`
    失败 → 整批白算（实测会话 20260915-131130-010611：第一次产物 13,523 字符
    死在解析上，重发再烧 20 万 tok）。抢救：逐个元素抠，写完的节点照收，
    丢掉没写完的尾巴。
    """
    from wovra.agent.maintenance import _salvage_state_json

    full = json.dumps({
        # 顺序刻意让两个"小对象"在前：截断发生在它们之后，抢救才能把三段都拿回来
        "split_assessment": {"splittable": True},
        "unassigned": {"block_ids": ["R1-B1"]},
        "domains": [
            {"name": "A 线", "files": ["L00"]},
            {"name": "B 线", "files": ["L01"], "description": "带 } 与 「引号」 的描述"},
            {"name": "C 线", "files": ["L02"]},
        ],
    }, ensure_ascii=False)
    # 截断在第 2 个节点中间（模拟输出预算掐断）
    cut = full.index("C 线")
    salvaged = _salvage_state_json(full[:cut])
    assert salvaged is not None
    names = [d.get("name") for d in salvaged.get("domains") or []]
    assert names == ["A 线", "B 线"], names          # 写完的都要，未写完的丢掉
    assert salvaged.get("split_assessment") == {"splittable": True}
    # 正常 JSON 一律走原路（不误伤）
    assert _salvage_state_json(full)["domains"][0]["name"] == "A 线"
    # 彻底没形的东西 → None（不能凭空造产物）
    assert _salvage_state_json("模型这次没调工具，只说了几句话") is None


def test_split_instruction_marks_rounds_for_descriptions(monkeypatch, tmp_path):
    """硬数据每行带**轮数**、指令允许叶子省描述（产物瘦身防截断）。"""
    task = Task.create(goal="目标")
    task.rounds = [
        _mk_file_round(1, "写 a", ["src/a.py"]),
        _mk_file_round(2, "再写 a", ["src/a.py"]),
        _mk_file_round(3, "写 b", ["src/b.py"]),
    ]
    for r in task.rounds:
        r["org_state"] = ""
    agent = Agent(llm=_StubLLM([[ _chunk(_delta(content=_org_json() or "")) ]]),
                  tools=[], task=task)
    lines, n = agent._split_hard_data(agent.rounds)
    joined = "\n".join(lines)
    assert n == 2
    assert "src/a.py" in joined and "轮 2" in joined     # 多轮文件有轮数
    assert "src/b.py" in joined and "轮 1" in joined     # 单轮文件也有（叶子据此省描述）
    # 指令里写明新口径（2026-09-15 用户："分裂只需要写结构树和每个 agent 的
    # 职责，其它都不需要搞了"）：节点用 path 声明范围、不逐文件列清单、
    # 文件描述由 Runtime 取文件开头
    from wovra.agent import prompts as prompts_module
    instr = prompts_module._SPLIT_INSTRUCTIONS
    assert "每个节点填 name + parent + path" in instr
    assert "不要逐文件列清单" in instr
    assert "文件开头" in instr


def test_split_normalizes_prefixless_path_refs(monkeypatch, tmp_path):
    """路径引用要**归一**：模型按"项目自己的视角"写 `src/a.cpp`，真实是
    `cpp/src/a.cpp` —— 唯一后缀命中就补成全路径，不靠模型把前缀写对。

    实测（2026-09-15 会话 20260915-131130-010611）：工作区里混着 Python 版、
    `cpp/`、文档，两批分裂都因为"27/29 个文件没有域认领"整批中止；根因之一
    就是这种缺前缀/带反引号的路径引用**原样放行**、在覆盖检查里全军覆没。
    """
    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path)
    domains = [{
        "name": "C++ 实现",
        "description": "cpp/ 下的实现",
        "files": ["`src/gripper.cpp`"],     # 缺前缀 + 反引号
    }]
    live = {"L00": "cpp/src/gripper.cpp", "L01": "cpp/include/omni.hpp"}
    defects, normalized = Agent._resolve_file_refs(domains, None, live, {})
    assert defects == []                                   # 归一是**痕迹**不是缺陷
    assert domains[0]["files"] == ["cpp/src/gripper.cpp"]
    assert normalized and "cpp/src/gripper.cpp" in normalized[0]
    # 歧义（两个同名后缀）不许乱指：原样保留，交给 Runtime 归位
    live2 = {"L00": "a/x.py", "L01": "b/x.py"}
    d2 = [{"name": "N", "files": ["x.py"]}]
    _defects2, normalized2 = Agent._resolve_file_refs(d2, None, live2, {})
    assert d2[0]["files"] == ["x.py"] and normalized2 == []


def test_split_does_not_abort_when_round_is_already_routed(monkeypatch, tmp_path):
    """文件没人认领、但**本轮已归某域** → 不算漏认领（跟 `views.ownership` 同判据）。

    这条正是实测会话 20260913-175945-533c5d 的情形：R5 的 `active_view` 已经是
    「眼睛（视觉通道）与改文件回显」，它的 7 个文件块按轮归属本来就该进那个域——
    分裂检查不能对着已经归好域的轮报错。
    """
    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path)
    domains_args = json.dumps({
        "domains": [{
            "name": "甲",
            "description": "别的活儿",
            "file_domains": ["src/other.py"],
            "block_ids": [],
        }],
        "unassigned": {"block_ids": [], "reason": ""},
        "split_assessment": {"splittable": True, "reason": "可拆"},
    }, ensure_ascii=False)
    chunk = _chunk(_delta(tool_calls=[
        _fragment(0, id="d1", name="submit_domains", arguments=domains_args),
    ]))
    task = Task.create(goal="目标")
    task.rounds = [_mk_file_round(1, "写文件", ["src/a.py"])]
    for r in task.rounds:
        r["org_state"] = ""
        r["active_view"] = "甲"          # 本轮已归"甲" → 文件块跟着它
    agent = Agent(
        llm=_StubLLM([[_chunk(_delta(content=_org_json()))]],
                     split_responses=[[chunk]]),
        tools=[], task=task, org_watermark=0, org_grace_rounds=0,
        org_cooldown_rounds=0,
    )
    agent.last_context_estimate = 5000
    agent._maybe_organize_batch()

    entries = [str(e.get("detail") or "") for e in (task.history or [])
               if e.get("kind") == "maintenance"]
    assert not any("分裂中止" in x or "SplitCoverageError" in x for x in entries)
    # 产物正常暂存（照旧往下走）
    assert (task.rounds[0].get("pending_org") or {}).get("domains")


def test_label_blocks_batch_semantic_labeling(monkeypatch, tmp_path):
    """机制二：一次调用为全部块产出路由式摘要 + 大类归类。"""
    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path)
    labeling = json.dumps({
        "categories": [
            {"id": "A", "name": "页面搭建", "description": "创建初始页面"},
            {"id": "B", "name": "功能扩展", "description": "增量加功能"},
        ],
        "blocks": [
            {"id": "R2-B1", "category": "A", "summary": "write_file → index.html（14KB 初版）"},
            {"id": "R2-B2", "category": "B", "summary": "edit_file → index.html（修引号）"},
        ],
    }, ensure_ascii=False)
    task = Task.create(goal="x")
    task.rounds = [_round(2, "做页面", "好了")]  # _round 自带 done+blocks 外的字段
    task.rounds[0]["blocks"] = [
        {"id": "R2-B1", "kind": "work", "start": 0, "end": 1,
         "start_event": "R2-E01", "end_event": "R2-E02",
         "touched_files": ["index.html"], "wrote_files": ["index.html"],
         "command_types": []},
    ]
    agent = Agent(llm=_StubLLM([[_chunk(_delta(content=labeling))]]),
                  tools=[], task=task)

    result = agent.label_blocks()

    assert len(result["categories"]) == 2
    assert result["labels"]["R2-B1"]["category"] == "A"
    assert "write_file → index.html" in result["labels"]["R2-B1"]["summary"]
    prompt = agent.llm.calls[0]["messages"][0]["content"]
    assert "R2-B1" in prompt and "路由式" in prompt  # 摘要进了提示词


def test_org_generation_restored_from_disk_on_restart():
    """重启恢复代次计数：不能从 0 重来，否则新批次与旧批次撞号。

    撞号的后果是折叠判定反转——下一批新整理被打上"最老"的号，而十几
    代前的旧轮因号更大成为"最近三代"，二者身份对调。恢复口径与装配端
    一致：无 org_generation 的已整理轮视为第 1 代。
    """
    task = Task.create(goal="g")
    rounds = []
    for seq in range(1, 9):
        r = _mk_file_round(seq, f"轮{seq}", [f"f{seq}.txt"])
        r["org_state"] = "done"
        rounds.append(r)
    rounds[0]["org_generation"] = 3   # 磁盘上已有 3 代
    rounds[1]["org_generation"] = 3
    rounds[2]["org_generation"] = 4
    task.rounds = rounds

    agent = Agent(llm=_StubLLM(), tools=[], task=task)
    assert agent._org_generation == 4          # 从磁盘最大值续起
    # 隔离验证：无字段的 done 轮按第 1 代口径计入
    only_legacy = Task.create(goal="g2")
    legacy = _mk_file_round(1, "旧轮", ["a.txt"])
    legacy["org_state"] = "done"               # 无 org_generation（迁移前数据）
    only_legacy.rounds = [legacy]
    agent2 = Agent(llm=_StubLLM(), tools=[], task=only_legacy)
    assert agent2._org_generation == 1         # 与装配端"旧轮=第 1 代"对齐
    # 下一批新整理（+1）不会是 1，因此不会被判成最老一代而立即折叠
    agent2._org_generation += 1
    assert agent2._org_generation == 2


def test_org_generation_counter_continues_across_restart():
    """跨重启不撞号：重启后新批次代次严格大于磁盘上的所有批次。"""
    task = Task.create(goal="g")
    r1 = _mk_file_round(1, "写 a", ["a.txt"])
    r1["org_state"], r1["org_generation"] = "done", 5
    r1["block_summaries"] = {"R1-B1": "a.txt 描述"}
    task.rounds = [r1]
    agent = Agent(llm=_StubLLM(), tools=[], task=task)
    agent._org_generation += 1
    assert agent._org_generation == 6
    # 折叠窗口按 6 计：keep_min = 4，第 5 代与第 6 代都保留
    r1["org_generation"] = 5
    r2 = _mk_file_round(2, "写 b", ["b.txt"])
    r2["org_state"], r2["org_generation"] = "done", 6
    r2["block_summaries"] = {"R2-B1": "b.txt 描述"}
    agent.rounds = [r1, r2]
    msgs = agent._assemble_messages_impl()
    joined = "\n".join(str(m.get("content") or "") for m in msgs)
    assert "R1]（折叠）" not in joined          # 第 5 代在最近 3 代窗口内
    assert "R2]（折叠）" not in joined


def test_split_appends_to_org_conversation(monkeypatch, tmp_path):
    """分裂输入 = 整理对话的纯追加（缓存友好）：split messages 以 org
    messages 为前缀 + assistant(提交调用) + tool 结果 + 分裂指令。"""
    agent, task = _split_fixture(
        monkeypatch, tmp_path,
        [[_chunk(_delta(content=_org_json()))]], [[_domains_chunk()]],
    )
    agent._maybe_organize_batch()

    org_calls = [c for c in agent.llm.calls if c.get("lane") == "org"]
    split_calls = [c for c in agent.llm.calls if c.get("lane") == "split"]
    assert org_calls and split_calls
    org_msgs = org_calls[0]["messages"]
    split_msgs = split_calls[0]["messages"]
    assert len(split_msgs) > len(org_msgs)
    assert split_msgs[:len(org_msgs)] == org_msgs          # 纯追加：前缀骑缓存
    appended = split_msgs[len(org_msgs):]
    if appended[0].get("tool_calls"):
        assert appended[0]["tool_calls"][0]["function"]["name"] == "submit_organization"
        assert appended[1]["role"] == "tool"               # 提交结果
        assert "[分裂结构指令]" in str(appended[2]["content"])  # 分裂指令
    else:
        # 正文 JSON fallback：assistant 原文 + 分裂指令
        assert appended[0]["role"] == "assistant"
        assert "[分裂结构指令]" in str(appended[1]["content"])
    # 开思考（不传 thinking disabled）：语义判断需要推理（用户拍板）
    assert "extra_body" not in split_calls[0] or not split_calls[0].get("extra_body")
    # 产物：thoughts 与 domains 都落暂存
    assert task.rounds[0]["pending_org"]["domains"][0]["name"] == "web 演示"
    assert (
            task.rounds[0].get("split_assessment")
            or task.rounds[0]["pending_org"].get("split_assessment")
        )["splittable"] is False


def test_promote_materializes_registry_entries(monkeypatch, tmp_path):
    """Level 1 视图分化第一步（2026-09-11 用户拍板 A）：分裂产物生效时
    机械翻译成注册表条目——此前注册表永远只有主 agent A，域树落档后
    没有任何下游消费者。

    幂等：同一批产物重复 promote 不重复追加（崩溃补做/重启重放安全）。
    """
    agent, task = _split_fixture(
        monkeypatch, tmp_path,
        org_pool=[[_chunk(_delta(content=_org_json()))]],
        split_pool=[[_domains_chunk()]],
    )
    assert [e["id"] for e in task.registry] == ["Main"]   # promote 前只有主 agent
    agent._maybe_organize_batch()
    # **A 步（2026-09-15）**：产物一就绪，注册表**立刻**落（子 agent 即时可见）——
    # 全量路径的装配不读注册表，不必等轮边界；而 `domains`（上下文替换）仍在暂存区
    assert [e["id"] for e in task.registry] == ["Main", "A"]
    assert task.rounds[0]["pending_org"]["domains"]      # 域树仍等边界

    agent._promote_org_results()                         # 边界步：落域树（注册表幂等）
    ids = [e["id"] for e in task.registry]
    assert ids == ["Main", "A"]                         # 域树 → 路径 ID（顶层 A/B/C）
    entry = task.registry[1]
    assert entry["name"] == "web 演示"
    assert entry["description"] == "纯 HTML 演示页，产出可视化灵感"
    assert entry["file_domains"] == ["index.html"]
    # 2026-09-14：树里挂在该节点下的块算"命中"（模型给关系、代码算归宿），
    # 故这里不再是 dormant，而是有材料的 active
    assert entry["status"] == "active"
    # 留痕：注册表变更进 maintenance 账本，可查
    # 留痕（A 步"提前落实"/B 步"落实为注册表条目"两可，2026-09-15）
    assert any(
        "registry：" in str(h.get("detail"))
        for h in task.history if h.get("kind") == "maintenance"
    )

    # 幂等：重复 promote（模拟崩溃补做）不重复追加，条目数不变
    agent._promote_org_results()
    assert [e["id"] for e in task.registry] == ["Main", "A"]


def _files_domains_chunk():
    """分裂产物：显式给出 files 清单（走归属结算那条路）。"""
    args = json.dumps({
        "thoughts": [],
        "domains": [{
            "name": "诊断探针与常驻仪器",
            "description": "取证脚本与常驻仪器",
            "files": ["output/_p1.py", "output/_p2.py"],
            "block_ids": [],
        }],
        "split_assessment": {"splittable": True, "reason": "两条独立工作线"},
    }, ensure_ascii=False)
    return _chunk(_delta(tool_calls=[
        _fragment(0, id="d1", name="submit_domains", arguments=args),
    ]))


def test_promote_settles_ownership_out_of_main_agent(monkeypatch, tmp_path):
    """**归属结算**（2026-09-13，worklog §78）：分裂产物认领的文件，promote 时
    从主 agent 的清单里减掉——否则一个文件同时挂在两个 agent 名下，而
    `_file_permission` 先看 mine，两家都会放行，F2 互斥事实上失效。

    实测缺陷：会话 20260913-125849-2963df 里 `Main.files`(17) ∩ `D.files`(17)
    = 16 个文件（`output/` 下的一堆 `_` 探针），页面就成了"主 agent 挂着一堆
    临时测试脚本"。口径来源是分裂指令自己那句"把**全部文件活**搬进它，主
    agent 只剩闲聊 + 环境块"——写在提示词里不算数，机制负责执行。
    """
    agent, task = _split_fixture(
        monkeypatch, tmp_path,
        org_pool=[[_chunk(_delta(content=_org_json()))]],
        split_pool=[[_files_domains_chunk()]],
    )
    main = next(e for e in task.registry if e["id"] == "Main")
    main["files"] = ["output/_p1.py", "output/_p2.py", "output/_stale.py"]
    main["file_notes"] = {"output/_p1.py": "旧描述", "output/_p2.py": "旧描述",
                         "output/_stale.py": "旧描述"}

    agent._maybe_organize_batch()
    agent._promote_org_results()

    entry = next(e for e in task.registry if e["id"] == "A")
    assert registry_module.entry_files(entry) == ["output/_p1.py", "output/_p2.py"]
    # 搬走的走了、没被认领的留着（不是清空，是结算）
    assert registry_module.entry_files(main) == ["output/_stale.py"]
    assert set(main["file_notes"]) == {"output/_stale.py"}
    # 全注册表无重叠 —— 这一条就是实测缺的那一步
    assert registry_module.registry_defects(task.registry) == []
    assert any(
        "归属结算" in str(h.get("detail"))
        for h in task.history if h.get("kind") == "maintenance"
    )


def test_promote_rejects_product_that_would_break_cross_entry_f2(monkeypatch, tmp_path):
    """**落点预演**是道真闸门：产物内部合规 ≠ 落进注册表后合规。

    `split_defects` 只看新域树自己（看不见注册表里早已据着同一批文件的旧
    条目），所以 promote 必须**先投影、再体检、后落地**：不通过就与内部缺陷
    同等拒收——不写 `r["domains"]`、不动注册表、落一条醒目 split_defect。
    用户口径："有些错误是根基，其错了，我下面测试无意义"。
    """
    agent, task = _split_fixture(
        monkeypatch, tmp_path,
        org_pool=[[_chunk(_delta(content=_org_json()))]],
        split_pool=[[_files_domains_chunk()]],
    )
    monkeypatch.setattr(
        registry_module, "registry_defects",
        lambda _reg: ["F2 违反：Main.files 与 A.files 共认 1 个文件（output/_p1.py）"],
    )
    notes: list[str] = []
    agent.on_progress = notes.append

    agent._maybe_organize_batch()
    agent._promote_org_results()

    assert [e["id"] for e in task.registry] == ["Main"]        # 注册表一字未动
    assert not task.rounds[0].get("domains")                   # 产物不落档
    assert not task.rounds[0].get("pending_org")               # 不留暂存
    assert any(h.get("kind") == "split_defect" for h in task.history)
    assert any("⛔" in n for n in notes)


def test_maintenance_reports_stage_progress(monkeypatch, tmp_path):
    """整理/分裂两个阶段都要往**直播流**报状态（2026-09-13 用户报"卡到回答中了"）。

    实测一批维护 = org 75s + split 31s（共 **106 秒**），而这期间**一个分片都不推**
    —— 界面就停在最后一段流留下的"回答中…"不动，看着像卡死。阶段名推出去，
    运行线才说得清在干什么（CLI 侧只是多两行提示，无副作用）。
    """
    agent, task = _split_fixture(
        monkeypatch, tmp_path,
        org_pool=[[_chunk(_delta(content=_org_json()))]],
        split_pool=[[_domains_chunk()]],
    )
    seen: list[str] = []
    agent.on_progress = seen.append
    agent._maybe_organize_batch()

    assert seen, "维护必须报阶段状态，否则这一个多钟头界面是死的"
    assert any("整理上下文" in s for s in seen), seen
    assert any("分裂分析" in s for s in seen), seen
    assert any("整理完成" in s for s in seen), seen


def test_split_skipped_when_org_fails(monkeypatch, tmp_path):
    """org 失败则 split 跳过（分裂依赖整理质量，失败批次不产出）。"""
    agent, task = _split_fixture(
        monkeypatch, tmp_path, [], [[_domains_chunk()]],  # org 池空 → 失败
    )
    agent._maybe_organize_batch()

    assert task.rounds[0]["org_state"] == "failed"
    assert not any(c.get("lane") == "split" for c in agent.llm.calls)


def test_split_instruction_carries_view_watermarks(monkeypatch, tmp_path):
    """逐层分裂硬数据（零 LLM）：分裂指令带上各视图自身体量与「是否到自身水位」。

    判据归机制（Runtime 给体量事实）、语义归模型（这一摊活是否真已分成互不
    相干的两条线）——plan §13.1。水位按**视图各自**计量，故判据只能由 Runtime
    现算，不能由模型自述。
    """
    agent, task = _split_fixture(
        monkeypatch, tmp_path,
        org_pool=[[_chunk(_delta(content=_org_json()))]],
        split_pool=[[_domains_chunk()]],
    )
    task.rounds[0]["domains"] = [
        {"name": "工具层", "description": "边界守卫",
         "file_domains": ["src/wovra/tools/"]},
    ]
    # 注意：Agent 构造时会复制一份 rounds（agent.rounds 与 task.rounds 不同
    # 对象）——分裂硬数据读的是 agent.rounds，故 domains 必须落在它上面。
    agent.rounds[0]["domains"] = task.rounds[0]["domains"]
    agent._maybe_organize_batch()

    split_calls = [c for c in agent.llm.calls if c.get("lane") == "split"]
    assert split_calls
    prompt = "\n".join(
        str(m.get("content") or "") for m in split_calls[0]["messages"]
    )
    assert "各视图自身体量" in prompt            # 硬数据行（体量事实仍给）
    assert "工具层" in prompt
    assert "考虑在其内部再裂一层" in prompt      # 到没到水位的机械结论仍给（体量事实）
    assert "你的**唯一任务**" in prompt          # 只写树 + 职责，不做分裂判定
    assert "每个节点填 name + parent + path" in prompt   # 2026-09-15：节点用路径声明范围
    assert "职责只写给会成为 agent 的节点" in prompt      # 职责只给顶层
    assert "不要逐文件列清单" in prompt          # 文件归属由代码算


def test_split_view_watermarks_skips_main_agent_and_empty_domains(monkeypatch, tmp_path):
    """主 agent 是兜底桶（不参与"拆不拆自己"）；无域时不产生硬数据行。"""
    agent = Agent(llm=_StubLLM(), tools=[])
    agent.rounds = [_mk_file_round(1, "写文件", ["a.txt"])]
    assert agent._split_view_watermarks() == []          # 无分裂产物 → 无行

    agent.rounds[0]["domains"] = [{"name": "甲", "file_domains": ["a.txt"]}]
    lines = agent._split_view_watermarks()
    joined = "\n".join(lines)
    assert "甲" in joined
    assert "主agent" not in joined and "A：" not in joined


def test_promote_records_economics_and_lifecycle(monkeypatch, tmp_path):
    """promote 通路（步 3 接线）：产物生效时把经济判据与生命周期动作记进账本。

    「发现职责 ≠ 创建 Agent」必须体现在留痕里：视图不由这里创建（装配层按
    domains 机械派生），这里只算与记——判据为负者只记录不拆（plan §13.3）。
    """
    agent, task = _split_fixture(
        monkeypatch, tmp_path,
        org_pool=[[_chunk(_delta(content=_org_json()))]],
        split_pool=[[_domains_chunk()]],
    )
    agent._maybe_organize_batch()
    agent._promote_org_results()

    details = "\n".join(
        str(h.get("detail")) for h in task.history if h.get("kind") == "maintenance"
    )
    assert "分裂经济判据" in details          # (B − B′) × N_future − C_split
    assert "生命周期动作" in details
    assert "发现职责≠创建 Agent" in details
    assert "C_split" in details and "18,830" in details
    # 域已进注册表（机械翻译仍走 registry.merge_into）
    assert [e["id"] for e in task.registry] == ["Main", "A"]


def test_split_hard_data_lists_live_files(monkeypatch, tmp_path):
    """硬数据（零 LLM）：活性文件清单 + 数量；只读文件不算活性。"""
    agent = Agent(llm=_StubLLM(), tools=[])
    agent.rounds = [_mk_file_round(1, "写文件", ["a.txt", "b.txt"])]
    lines, n = agent._split_hard_data(agent.rounds)
    assert n == 2
    joined = "\n".join(lines)
    assert "a.txt" in joined and "b.txt" in joined
    assert "活性文件数：2" in joined


def test_split_hard_data_lists_non_live_files_as_history(monkeypatch, tmp_path):
    """非 LIVE 文件单独列一段、**逐条列出且必须挂满**（2026-09-14 用户口径：
    磁盘上没了不等于没用——它们的结论后面要装配进上下文）：
    不占分裂单元、不计入上限。"""
    agent = Agent(llm=_StubLLM(), tools=[])
    agent.rounds = [
        _mk_file_round(1, "写文件", ["a.txt"]),
        _mk_read_round(2, "读一眼旧文件", ["old.txt"]),
    ]
    lines, n = agent._split_hard_data(agent.rounds)
    joined = "\n".join(lines)
    assert n == 1                                   # 只读的不算活性、不计上限
    assert "非 LIVE 文件清单" in joined and "共 1 个" in joined
    assert "H00 = old.txt" in joined and "L00 = a.txt" in joined
    # 2026-09-15 新口径：文件归属由代码按 path 范围**机械分配**、历史文件由 Runtime
    # **机械挂载**——故硬数据里不再要求"逐文件写编号 / 一个不漏地填 history_files"
    assert "机械分配" in joined and "机械挂载" in joined
    assert "不需要" in joined and "history_files" in joined


def test_split_auto_claims_non_live_file_without_home(monkeypatch, tmp_path):
    """非 LIVE 文件没挂到任何节点 → 同样 **Runtime 机械归位**（不落主 agent）。

    用户口径（2026-09-14）"非 LIVE 文件也是必须填满的"仍然成立——只是由代码来挂：
    挂到最近的节点，全都不沾边就新建 Runtime 节点（按顶层目录聚合）。
    """
    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path)
    domains_args = json.dumps({
        "domains": [{
            "name": "后端", "file": "src/a.py",
            "description": "服务端逻辑", "history_files": [],
        }],
        "unassigned": {"block_ids": [], "reason": ""},
    }, ensure_ascii=False)
    chunk = _chunk(_delta(tool_calls=[
        _fragment(0, id="d1", name="submit_domains", arguments=domains_args),
    ]))
    task = Task.create(goal="目标")
    task.rounds = [
        _mk_file_round(1, "写文件", ["src/a.py"]),
        _mk_read_round(2, "读临时脚本", ["tmp_probe.py"]),
    ]
    for r in task.rounds:
        r["org_state"] = ""
    agent = Agent(
        llm=_StubLLM([[_chunk(_delta(content=_org_json() or ""))]],
                     split_responses=[[chunk]]),
        tools=[], task=task, org_watermark=0, org_grace_rounds=0,
        org_cooldown_rounds=0,
    )
    agent.last_context_estimate = 5000
    agent._maybe_organize_batch()

    joined = "\n".join(str(h.get("detail")) for h in task.history
                        if h.get("kind") == "maintenance")
    assert "自动归位（历史文件）" in joined and "tmp_probe.py" in joined, joined[-4:]
    doms = (task.rounds[0].get("pending_org") or {}).get("domains") or []
    assert doms, "产物必须暂存（不再中止）"
    # tmp_probe.py 是顶层文件、谁也不沾边 → 新建 Runtime 节点接住它
    holders = [d for d in doms if "tmp_probe.py" in (d.get("history_files") or [])]
    assert holders and all(not d.get("main_agent") for d in holders)


def test_split_accepts_tree_with_history_attached(monkeypatch, tmp_path):
    """正例：非 LIVE 文件挂到叶子的 history_files 下 → 通过并暂存。"""
    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path)
    domains_args = json.dumps({
        "domains": [{
            "name": "后端", "file": "src/a.py",
            "description": "服务端逻辑", "history_files": ["tmp_probe.py"],
        }],
        "unassigned": {"block_ids": [], "reason": ""},
    }, ensure_ascii=False)
    chunk = _chunk(_delta(tool_calls=[
        _fragment(0, id="d1", name="submit_domains", arguments=domains_args),
    ]))
    task = Task.create(goal="目标")
    task.rounds = [
        _mk_file_round(1, "写文件", ["src/a.py"]),
        _mk_read_round(2, "读临时脚本", ["tmp_probe.py"]),
    ]
    for r in task.rounds:
        r["org_state"] = ""
    agent = Agent(
        llm=_StubLLM([[_chunk(_delta(content=_org_json() or ""))]],
                     split_responses=[[chunk]]),
        tools=[], task=task, org_watermark=0, org_grace_rounds=0,
        org_cooldown_rounds=0,
    )
    agent.last_context_estimate = 5000
    agent._maybe_organize_batch()
    staged = [r for r in task.rounds if r.get("pending_org")]
    assert staged, "产物应已暂存"
    dom = (staged[0]["pending_org"].get("domains") or [])[0]
    assert dom.get("history_files") == ["tmp_probe.py"]


def test_dedupe_domains_cross_domain_blocks():
    """代码层兜底：同一块跨域重复（模型偶发）按先到先留去重。"""
    domains = [
        {"name": "域A", "block_ids": ["R7-B1", "R5-B1"], "file_domains": []},
        {"name": "域B", "block_ids": ["R5-B1", "R9-B1"], "file_domains": []},
    ]
    out = Agent._dedupe_domains(domains)
    assert out[0]["block_ids"] == ["R7-B1", "R5-B1"]
    assert out[1]["block_ids"] == ["R9-B1"]   # 跨域重复被移除
    assert len(out) == 2                       # 域本身保留


def test_split_orphans_auto_go_to_main_agent(monkeypatch, tmp_path):
    """主 agent 兜底：没被任何域认领的块自动归 unassigned（不丢块）。"""
    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path)
    domains_args = json.dumps({
        "domains": [{
            "name": "某域", "description": "d", "file_domains": [],
            "block_ids": ["R1-B1"],
        }],
        "split_assessment": {"splittable": False, "reason": "r"},
    }, ensure_ascii=False)
    domains_chunk = _chunk(_delta(tool_calls=[
        _fragment(0, id="d1", name="submit_domains", arguments=domains_args),
    ]))
    org_json = json.dumps({
        "rounds": [{"seq": 1, "normalized_user_input": "意图",
                    "key_constraints": "", "block_summaries": []}],
        "state_patch": {},
    }, ensure_ascii=False)
    task = Task.create(goal="目标")
    task.rounds = [_round(1, "第一轮", "答案")]
    task.rounds[0]["org_state"] = ""
    agent = Agent(
        llm=_StubLLM([[_chunk(_delta(content=org_json))]],
                     split_responses=[[domains_chunk]]),
        tools=[], task=task, org_watermark=0, org_grace_rounds=0,
        org_cooldown_rounds=0,
    )
    agent.last_context_estimate = 5000
    agent._maybe_organize_batch()
    pending = task.rounds[0]["pending_org"]
    # R1-B1 进了域；没有其它块，无孤儿 → unassigned 不产生
    assert "R1-B1" in pending["domains"][0]["block_ids"]
    assert "unassigned" not in pending or not pending.get("unassigned")


def _dangling(msgs: list[dict]) -> list[str]:
    """协议校验助手：返回未被回复的 tool_call id（含被 user 消息打断的）。

    整理调用是"整段装配 + 尾部追加指令（user）"的追加式对话——尾部一旦
    挂着未回复的 tool_calls，追加的 user 指令就是非法序列（严格端点 400）。
    """
    pending: set = set()
    bad: set = set()
    for m in msgs:
        role = m.get("role")
        if role == "tool":
            pending.discard(m.get("tool_call_id"))
        elif role == "assistant":
            for call in m.get("tool_calls") or []:
                if call.get("id"):
                    pending.add(call["id"])
        elif role == "user" and pending:
            bad |= pending
            pending = set()
    return sorted(bad | pending)


def test_maint_snapshot_gate_defers_on_dangling_tail(monkeypatch, tmp_path):
    """协议闸门：装配尾悬空时**不取快照**，置 deferred，结果落盘后补做。

    原始触发场景（2026-09-11 400 实测的修法）：检查点切轮会在**工具方法体内**
    闭合轮（todo→verify_milestone→close_round），此刻调用方的 tool 结果尚未
    落盘，装配尾部是未回复的 tool_calls——取快照追加整理指令会被严格端点
    直接 400（实测 21:27 那批 1 秒失败、未计费）。

    **§53（2026-09-12）之后检查点改为轮内标记、不再切轮**，内置路径不再在
    工具方法体内闭合轮，故闸门成为**防御性**路径（\\c 续跑、中断重放、未来
    任何"工具体内闭合"的实现仍需要它）。本用例直接构造那个现场，钉住闸门
    本身与 deferred 的补做回路。
    """
    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path)
    task = Task.create(goal="g")
    agent = Agent(
        llm=_StubLLM([[_chunk(_delta(content=_batch_org_json([1])))]]),
        tools=[], task=task, org_watermark=0, org_grace_rounds=0,
        org_cooldown_rounds=0, async_organization=False,
    )
    agent._open_or_reuse_round("干活")
    agent._record_event("user", {"role": "user", "content": "干活"})
    # 模拟"轮已在工具方法体内闭合"（§53 之前由检查点触发）——不走 close_round，
    # 免得它顺手把这次水位检查做掉（那样就测不到闸门了）
    r = agent.rounds[-1]
    r["end_state"] = "completed"
    r["org_state"] = ""
    agent.current_round = None

    # 闭合那一刻的装配尾：一条未回复的 tool_calls（调用方结果尚未落盘）
    args = json.dumps({"action": "verify_stage", "evidence": "测试全绿"})
    r["events"].append({
        "id": "R1-E99", "type": "tool_call", "status": "",
        "truncated": "todo(verify_stage)",
        "message": {"role": "assistant", "content": "",
                    "tool_calls": [{"id": "call_1", "type": "function",
                                    "function": {"name": "todo", "arguments": args}}]},
    })
    agent.last_context_estimate = 5000
    agent._tools_running = True   # 真实路径=工具批次执行中
    assert agent._maint_snapshot() is None

    agent._maybe_organize_batch()          # 水位到线，但尾部悬空 → 推迟

    assert agent._maint_deferred is True   # 推迟，不冒 400 的风险
    assert agent.llm.calls == []           # 一条整理调用都没发
    details = [e["detail"] for e in task.history if e["kind"] == "maintenance"]
    assert any("水位检查推迟" in d for d in details), details

    # 结果落盘（补进同一轮）→ 补做那条被推迟的检查
    r["events"].append({
        "id": "R1-E100", "type": "tool_result", "status": "",
        "truncated": "todo -> OK",
        "message": {"role": "tool", "tool_call_id": "call_1", "content": "OK"},
    })
    agent._finish_tool_result("call_1", "todo", args, "OK")

    assert agent._maint_deferred is False  # 已补做，标记复位
    org_calls = [c for c in agent.llm.calls
                 if any("[整理指令]" in str(m.get("content") or "")
                        for m in c["messages"])]
    assert len(org_calls) == 1, "结果落盘后应补发起整理（不吞掉这次触发）"
    assert _dangling(org_calls[0]["messages"]) == []  # 补做时输入协议完整


def test_maint_snapshot_taken_when_protocol_complete(monkeypatch, tmp_path):
    """反向对照：协议完整时不推迟——水位检查照常在轮闭合那一刻发起。

    闸门的代价必须是零：正常轮闭合（尾部无悬空 tool_calls）不容许多等
    半拍，否则就是把一个协议修复变成全量的延迟。
    """
    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path)
    monkeypatch.setattr("wovra.tokens.estimate", lambda text: len(text or "") // 4)
    task = Task.create(goal="x")
    task.rounds = [_round(1, "甲" * 2400, "甲" * 2400)]
    task.rounds[0]["org_state"] = ""
    agent = Agent(
        llm=_StubLLM([[_chunk(_delta(content=_batch_org_json([1])))]]),
        tools=[], task=task, org_watermark=2000, org_grace_rounds=0,
        org_cooldown_rounds=0,
    )
    agent.last_context_estimate = 5000
    agent._maybe_organize_batch()

    assert agent._maint_deferred is False  # 未推迟
    assert len(agent.llm.calls) == 1       # 即时发起


def test_organize_backlog_respects_watermark_unless_forced(monkeypatch, tmp_path):
    """收尾整理默认走**水位闸门**；`force=True`（run 模式退出）才无视水位。

    2026-09-12 实测缺陷（会话 20260912-151325-22671f）：C4 网页输入在**每一轮**
    之后调 `organize_backlog()`，而旧实现无视水位/宽限/冷却 → 网页里"只聊一轮
    就被整理并压缩"（上下文峰值 1.7 万、水位线 10 万，org+split 照发：org
    prompt 22,776 + split 26,310 tok）。CLI 侧两道门都挡得住 R1，故触发只能来自
    这个调用点——默认走闸门后，`serve` 的既有调用无需改动即变安全。
    """
    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path)
    task = Task.create(goal="g")
    task.rounds = [_round(1, "甲", "乙")]
    task.rounds[0]["org_state"] = ""
    agent = Agent(
        llm=_StubLLM([[_chunk(_delta(content=_batch_org_json([1])))]]),
        tools=[], task=task, async_organization=False,
        org_grace_rounds=0, org_cooldown_rounds=0,   # 只留水位这一道门
    )
    agent.last_context_estimate = 17_000             # 远低于默认水位 100,000

    agent.organize_backlog()                         # 默认：走闸门

    assert agent.llm.calls == []                     # 未到线：一次调用都不发
    assert task.rounds[0]["org_state"] == ""

    agent.organize_backlog(force=True)               # run 模式退出：无视水位

    assert task.rounds[0]["org_state"] == "done"
    assert len(agent.llm.calls) == 1


def test_product_settles_at_round_close_before_next_round(monkeypatch, tmp_path):
    """§50：产物在「分裂之后、下一轮对话之前」生效。

    用户口径：「把重组上下文、子 agent 都放到分裂后面、下一轮对话前面……
    下一轮对话开始，基本就只需要追求对话」。故轮闭合时（同步维护已经跑完、
    没有开放轮）就把产物落地——注册表长出子 agent、历史轮补判归位，下一轮
    开场不必再建账。
    """
    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path)
    task = Task.create(goal="目标")
    agent = Agent(
        llm=_StubLLM(
            [[_chunk(_delta(content="干完了"))], [_chunk(_delta(content=_org_json()))]],
            split_responses=[[_domains_chunk()]],
        ),
        tools=[], task=task,
        org_watermark=0, org_grace_rounds=0, org_cooldown_rounds=0,
    )

    agent.run("问")                      # 轮闭合 → 水位检查（同步）→ 即时生效

    assert [e["id"] for e in task.registry] == ["Main", "A"]   # 子 agent 已建好
    assert task.rounds[0]["domains"][0]["name"] == "web 演示"   # 域树已落档
    assert "pending_org" not in task.rounds[0]                 # 暂存区已清空
    # 注册表可能已由 A 步提前落实（"提前落实"/"落实…注册表条目"两可），但必须有账
    assert any(
        "registry：" in str(h.get("detail")) and "子 agent" in str(h.get("detail"))
        or "registry：分裂产物落实为注册表条目" in str(h.get("detail"))
        for h in task.history if h.get("kind") == "maintenance"
    )


def test_product_settle_defers_while_a_round_is_open(monkeypatch, tmp_path):
    """有开放轮时不生效（缓存前缀纪律：轮内不许改写历史字节）。

    异步维护跑完时用户可能已经进了下一轮——那时产物照旧暂存，等下一次轮开启
    生效；与 `_open_or_reuse_round` 共用 `_view_lock`，两者不会交错。
    """
    agent, task = _split_fixture(
        monkeypatch, tmp_path,
        org_pool=[[_chunk(_delta(content=_org_json()))]],
        split_pool=[[_domains_chunk()]],
    )
    agent._maybe_organize_batch()         # 同步跑完，产物先暂存

    assert task.rounds[0]["pending_org"]["domains"]      # 暂存区有产物
    # A 步（2026-09-15）：注册表（子 agent）已提前落；域树（上下文替换）仍等边界
    assert [e["id"] for e in task.registry] == ["Main", "A"]

    # 模拟"用户已经在下一轮里"：**上下文替换**不生效（域树仍暂存，不许改轮内字节）
    agent.current_round = {
        "seq": 2, "user_input": {"original": "继续", "normalized": ""},
        "events": [], "refined_index": {}, "end_state": "open",
        "org_state": "", "active_view": "", "route_hops": 0,
    }
    agent._settle_after_maintenance()
    assert task.rounds[0].get("pending_org")             # 域树仍暂存
    assert not task.rounds[0].get("domains"), "轮内不许落域树（装配路径会中途切换）"

    # 轮闭合（无开放轮）→ 下一次结算即落地
    agent.current_round = None
    agent._settle_after_maintenance()
    assert [e["id"] for e in task.registry] == ["Main", "A"]
    assert "pending_org" not in task.rounds[0]


def test_backlog_skips_incomplete_protocol(monkeypatch, tmp_path):
    """收尾整理（run 模式退出）同走协议闸门：装配不完整时宁可不整理，
    也不发出必然 400 的请求——收尾整理失败不该由协议问题引起。"""
    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path)
    task = Task.create(goal="x")
    task.rounds = [{
        "seq": 1, "user_input": {"original": "干活", "normalized": ""},
        "events": [
            {"id": "R1-E01", "type": "user",
             "message": {"role": "user", "content": "干活"}},
            {"id": "R1-E02", "type": "tool_call", "message": {
                "role": "assistant", "content": "",
                "tool_calls": [{"id": "call_9", "type": "function",
                                "function": {"name": "todo", "arguments": "{}"}}]}},
        ],
        "end_state": "completed", "org_state": "",
    }]
    agent = Agent(llm=_StubLLM(), tools=[], task=task)

    agent.organize_backlog()

    assert agent.llm.calls == []              # 未发出必然 400 的请求
    assert agent.rounds[0]["org_state"] == ""  # 也未标记成 pending/failed


# ---- 维护输入口径 / 视图体量口径（2026-09-12，worklog §44）--------------------


def _two_domain_task(monkeypatch, tmp_path):
    """两域两文件的会话：装配分流后"当前视图那一桶"明显小于全量材料。"""
    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path)
    task = Task.create(goal="g")
    task.rounds = [
        _mk_file_round(1, "写 a", ["a.txt"]),
        _mk_file_round(2, "写 b", ["b.txt"]),
    ]
    for r in task.rounds:
        r["org_state"] = ""
    task.rounds[0]["domains"] = [
        {"name": "甲", "file_domains": ["a.txt"]},
        {"name": "乙", "file_domains": ["b.txt"]},
    ]
    agent = Agent(llm=_StubLLM(), tools=[], task=task)
    return agent, task


def test_maint_snapshot_uses_full_material_not_active_view(monkeypatch, tmp_path):
    """维护输入 = **全量材料**，不随 `active_view` 变瘦（2026-09-12，worklog §44.3-1）。

    现场：域产物一生效，`_maint_snapshot` 复用的 `_assemble_messages` 就成了
    "当前视图那一桶"——同一会话实测 333 条 → **9 条**，整理模型看不到料，
    R7-R13 连续两次交不出产物（org=False split=False），7 轮 185,787 tok 原文
    永远未整理。整理/分裂面对的是所有未整理轮的全部材料，故快照走全量装配。
    """
    agent, task = _two_domain_task(monkeypatch, tmp_path)
    full = agent._assemble_full_messages()

    # 当前轮标记为窄域「甲」：普通装配只给甲那份料（分流生效）
    agent.current_round = {
        "seq": 3, "user_input": {"original": "接着干", "normalized": ""},
        "events": [], "refined_index": {}, "end_state": "open",
        "org_state": "", "active_view": "甲",
    }
    agent.rounds.append(agent.current_round)
    view_msgs = agent._assemble_messages()
    snapshot = agent._maint_snapshot()

    assert snapshot is not None
    assert len(view_msgs) < len(full), "视图装配应比全量材料瘦（分流生效的前提）"
    assert len(snapshot) == len(agent._assemble_full_messages())
    assert len(snapshot) > len(view_msgs), "维护输入不许跟着 active_view 变瘦"


def test_view_assembly_watermarks_uses_assembly_caliber(monkeypatch, tmp_path):
    """体量口径订正：水位与经济判据用**装配口径**，材料口径只作对照。

    同一域实测 材料 5,144 tok vs 装配 90,979 tok（17.7×，worklog §44.3-2/3）；
    混用会让 `(B − B′) × N − C` 得出与事实相反的符号（仪器报"值得拆"而按
    装配口径应为负）。故运行时以装配口径为准，并把材料口径一并留下。
    """
    agent, _task = _two_domain_task(monkeypatch, tmp_path)
    domains = registry_module.latest_domains(agent.rounds)
    marks = agent._view_assembly_watermarks(domains, watermark=1)

    assert set(marks) >= {"甲", "乙"}
    jia = marks["甲"]
    assert jia["degraded"] is False            # 装配口径派生成功
    assert jia["material_tokens"] > 0          # 材料口径留存供对照
    assert jia["tokens"] >= jia["material_tokens"]  # 装配口径含块内事件全文
    assert jia["over"] is True                 # 到线判定用的是装配口径
    assert int(jia["material_tokens"]) == int(
        views_module.view_watermarks(
            agent.rounds, agent.task.get_state(), domains=domains,
            registry=agent.task.registry,
        )["甲"]["tokens"]
    )


def test_split_coverage_lines_feed_gap_and_overlap(monkeypatch, tmp_path):
    """覆盖缺口硬数据进分裂分析（2026-09-12，§40.4 待办乙 + §44.3-5）：

    未覆盖文件 = 本批分域的漏项（掉主 agent 兜底桶）；多域共命轮 = 父子域语义
    重叠/分域过细的信号（抬高视图切换频率、压低粘滞率）。两条都由 Runtime
    机械现算，判定仍归模型。
    """
    agent, _task = _two_domain_task(monkeypatch, tmp_path)
    # 甲乙共命 R1；R2 只有谁也不认领的 c.txt
    agent.rounds[0]["events"].extend([
        {"id": "R1-E20", "type": "tool_call", "message": {
            "role": "assistant", "content": "",
            "tool_calls": [{"id": "cx", "type": "function", "function": {
                "name": "write_file",
                "arguments": json.dumps({"path": "b.txt", "content": "x"})}}]}},
    ])
    agent.rounds.append(_mk_file_round(2, "写 c", ["c.txt"]))
    agent.rounds[0]["domains"] = [
        {"name": "甲", "file_domains": ["a.txt"]},
        {"name": "乙", "file_domains": ["b.txt"]},
    ]

    joined = "\n".join(agent._split_coverage_lines(agent.rounds))
    assert "分裂覆盖缺口：1 个文件" in joined and "c.txt" in joined
    assert "多域共命轮：1 轮" in joined and "R1（2 域）" in joined


def test_resolve_file_refs_translates_ids_and_reports_defects():
    """编号是模型侧编码：L→活性路径、H→历史路径；未知编号/用错节报缺陷。"""
    live = {"L00": "a.py", "L01": "b.py"}
    hist = {"H00": "old.tmp"}
    domains = [
        {"name": "甲", "file": "L00", "history_files": ["H00"]},
        {"name": "乙", "history_files": ["h00"]},      # 大小写不敏感：h00 → H00
        {"name": "丙", "file": "H00"},                 # 错节：H 当叶子
        {"name": "丁", "history_files": ["L01"]},      # 错节：L 当历史
        {"name": "戊", "history_files": ["H99"]},      # 未知编号
        {"name": "己", "file": "src/plain.py"},        # 手写路径：放行
    ]
    defects, _normalized = Agent._resolve_file_refs(domains, None, live, hist)
    assert domains[0]["file"] == "a.py" and domains[0]["history_files"] == ["old.tmp"]
    assert domains[1]["history_files"] == ["old.tmp"]  # h00 → H00 → 路径
    assert domains[5]["file"] == "src/plain.py"
    assert any("H00" in d and "活性" in d for d in defects)      # 错节一
    assert any("L01" in d and "非 LIVE" in d for d in defects)   # 错节二
    assert any("H99" in d for d in defects)                      # 未知编号


def test_split_product_with_file_ids_is_translated(monkeypatch, tmp_path):
    """端到端：产物里只写编号 → 暂存里的 file/history_files 已是真路径。"""
    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path)
    domains_args = json.dumps({
        "domains": [{
            "name": "后端", "file": "L00",
            "description": "服务端逻辑", "history_files": ["H00"],
        }],
        "unassigned": {"block_ids": [], "reason": ""},
    }, ensure_ascii=False)
    chunk = _chunk(_delta(tool_calls=[
        _fragment(0, id="d1", name="submit_domains", arguments=domains_args),
    ]))
    task = Task.create(goal="目标")
    task.rounds = [
        _mk_file_round(1, "写文件", ["src/a.py"]),
        _mk_read_round(2, "读临时脚本", ["tmp_probe.py"]),
    ]
    for r in task.rounds:
        r["org_state"] = ""
    agent = Agent(
        llm=_StubLLM([[_chunk(_delta(content=_org_json() or ""))]],
                     split_responses=[[chunk]]),
        tools=[], task=task, org_watermark=0, org_grace_rounds=0,
        org_cooldown_rounds=0,
    )
    agent.last_context_estimate = 5000
    agent._maybe_organize_batch()
    staged = [r for r in task.rounds if r.get("pending_org")]
    assert staged, "产物应已暂存"
    dom = (staged[0]["pending_org"].get("domains") or [])[0]
    assert dom["file"] == "src/a.py"                   # 编号已翻回真路径
    assert dom["history_files"] == ["tmp_probe.py"]


def test_validate_block_refs_reports_unknown_ids():
    """块 ID 校验：地图里没有的 ID 一律报缺陷（含 unassigned）。"""
    all_ids = {"R1-B1", "R19-24-B1"}
    domains = [
        {"name": "甲", "block_ids": ["R1-B1"]},                       # 合法
        {"name": "乙", "block_ids": ["R2-B1", "R19-24-B1"]},          # R2-B1 未知
        {"name": "丙", "chat_block_ids": ["R99-B1"]},                 # 未知
        {"name": "丁", "user_block_ids": ["R25-B1"]},                 # 未知
    ]
    unassigned = {"block_ids": ["R1-B1", "R77-B2"], "reason": ""}
    defects = Agent._validate_block_refs(domains, unassigned, all_ids)
    assert any("R2-B1" in d and "乙" in d for d in defects)
    assert any("R99-B1" in d and "chat_block_ids" in d for d in defects)
    assert any("R25-B1" in d and "user_block_ids" in d for d in defects)
    assert any("R77-B2" in d and "unassigned" in d for d in defects)
    assert not any("R1-B1" in d for d in defects)        # 合法 ID 不报
    assert len(defects) == 4


def _split_fixture_with_round(monkeypatch, tmp_path, split_pool):
    """单轮文件轮 + 水位触发：用于块 ID 校验的重发/中止路。"""
    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path)
    task = Task.create(goal="目标")
    task.rounds = [_mk_file_round(1, "写文件", ["src/a.py"])]
    for r in task.rounds:
        r["org_state"] = ""
    agent = Agent(
        llm=_StubLLM([[_chunk(_delta(content=_org_json() or ""))]],
                     split_responses=split_pool),
        tools=[], task=task, org_watermark=0, org_grace_rounds=0,
        org_cooldown_rounds=0,
    )
    agent.last_context_estimate = 5000
    return agent, task


def _split_args(chat_ids):
    return json.dumps({
        "domains": [{"name": "后端", "file": "L00", "chat_block_ids": chat_ids}],
        "unassigned": {"block_ids": [], "reason": ""},
    }, ensure_ascii=False)


def _split_chunk(args):
    return _chunk(_delta(tool_calls=[
        _fragment(0, id="d1", name="submit_domains", arguments=args),
    ]))


def test_split_repairs_unknown_block_id_once(monkeypatch, tmp_path):
    """块 ID 抄错 → 带诊断重发一次 → 修好则继续（产物照常暂存）。"""
    agent, task = _split_fixture_with_round(
        monkeypatch, tmp_path,
        split_pool=[[_split_chunk(_split_args(["R1-B9"]))],
                    [_split_chunk(_split_args(["R1-B1"]))]],
    )
    agent._maybe_organize_batch()
    staged = [r for r in task.rounds if r.get("pending_org")]
    assert staged, "重发修好后应照常暂存"
    dom = (staged[0]["pending_org"].get("domains") or [])[0]
    assert dom["chat_block_ids"] == ["R1-B1"]
    joined = "\n".join(str(h.get("detail")) for h in task.history)
    assert "不存在的块 ID R1-B9" in joined          # 缺陷现场可查
    assert "带诊断重发一次" in joined


def test_split_aborts_when_block_id_stays_unknown(monkeypatch, tmp_path):
    """两次都抄错 → 整批中止回入水位（与文件编号同一套硬闸门）。"""
    agent, task = _split_fixture_with_round(
        monkeypatch, tmp_path,
        split_pool=[[_split_chunk(_split_args(["R1-B9"]))],
                    [_split_chunk(_split_args(["R1-B9"]))]],
    )
    agent._maybe_organize_batch()
    assert all(r.get("org_state") == "failed" for r in task.rounds)
    assert not any(r.get("pending_org") for r in task.rounds)   # 不 promote
    joined = "\n".join(str(h.get("detail")) for h in task.history)
    assert "中止" in joined and "R1-B9" in joined


def test_chat_bucket_is_materialized_as_top_level_node(monkeypatch, tmp_path):
    """闲聊/未归属物化成顶层节点（用户口径：改名成闲聊的主题，归主 agent）。"""
    from wovra import blocks as blocks_module

    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path)
    task0 = Task.create(goal="目标")          # 先造轮拿到聊天块 ID
    task0.rounds = [_round(1, "闲聊", "好"), _mk_file_round(2, "写文件", ["src/a.py"])]
    chat_bid = blocks_module.segment_round_by_file(task0.rounds[0])[0]["id"]
    args = json.dumps({
        "domains": [{"name": "后端", "file": "L00"}],
        "unassigned": {"block_ids": [chat_bid], "topic": "工具吐槽与前端灵感",
                       "reason": "闲聊"},
    }, ensure_ascii=False)
    chunk = _chunk(_delta(tool_calls=[
        _fragment(0, id="d1", name="submit_domains", arguments=args),
    ]))
    task = Task.create(goal="目标")
    task.rounds = [_round(1, "闲聊", "好"), _mk_file_round(2, "写文件", ["src/a.py"])]
    for r in task.rounds:
        r["org_state"] = ""
    agent = Agent(
        llm=_StubLLM([[_chunk(_delta(content=_org_json() or ""))]],
                     split_responses=[[chunk]]),
        tools=[], task=task, org_watermark=0, org_grace_rounds=0,
        org_cooldown_rounds=0,
    )
    agent.last_context_estimate = 5000
    agent._maybe_organize_batch()
    staged = [r for r in task.rounds if r.get("pending_org")]
    assert staged
    doms = staged[0]["pending_org"]["domains"]
    bucket = [d for d in doms if d.get("main_agent")]
    assert len(bucket) == 1
    assert bucket[0]["name"] == "工具吐槽与前端灵感"
    assert bucket[0]["chat_block_ids"] == [chat_bid]
    assert "unassigned" not in staged[0]["pending_org"]     # 已并入桶节点


def test_stale_split_product_requeues_instead_of_hard_reject(monkeypatch, tmp_path):
    """**材料过期 ≠ 根基缺陷**（2026-09-15 修，会话 20260914-181519-0d3875 实测）。

    产物是对某一批材料做的；之后又有轮闭合（新文件）时必然覆盖不全。旧行为按
    "根基缺陷"拒收，而那批轮留在 `org_state=done` → 再也不会被选进整理批次 →
    会话永久没有可用域树（实测卡死）。新行为：缺陷**全是"未覆盖"**且存在更新轮次
    → 本批回入水位（`org_state=failed`）＋`split_state=stale`＋记 `split_stale`。
    """
    from wovra import registry as registry_module

    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path)
    task = Task.create(goal="g")
    # 本批：R1 写 a.py（产物只覆盖 a.py）；其后 R2 又写了 b.py（材料变了）
    task.rounds = [
        _mk_file_round(1, "写 a.py", ["a.py"]),
        _mk_file_round(2, "写 b.py", ["b.py"]),
    ]
    for r in task.rounds:
        r["org_state"] = ""
    product = [{"name": "A 域", "description": "a.py 这条线", "file": "a.py"}]
    # 材料过期的信号（2026-09-15 起）：`_bind_files_by_path` 记下"节点声明的
    # 范围覆盖不到的文件"——它不再触发拒收（Runtime 会机械归位），但配合
    # "有更新轮次"仍是"这份产物对旧材料做的"的可靠证据。
    task.rounds[0]["pending_org"] = {"domains": product,
                                     "uncovered_by_scope": ["b.py"]}
    task.rounds[0]["org_state"] = "done"
    task.rounds[0]["org_generation"] = 1
    task.rounds[0]["end_state"] = "completed"
    task.rounds[1]["end_state"] = "completed"     # R2 已闭合但未整理（材料更新）
    agent = Agent(llm=_StubLLM(), tools=[], task=task,
                  org_watermark=10**9)            # 不让水位在本测试里另起批次

    agent._promote_org_results()

    assert task.rounds[0]["org_state"] == "failed", "过期产物应把本批回入水位"
    assert task.rounds[0]["split_state"] == "stale"
    assert any(e.get("kind") == "split_stale" for e in task.history), "缺少 split_stale 留痕"
    assert [str(e.get("id")) for e in task.registry] == ["Main"], "过期产物不许落进注册表"


def test_hard_split_defect_still_rejected_and_marked(monkeypatch, tmp_path):
    """根基缺陷（文件重叠）仍按原口径**拒收 + 停 + 响亮留痕**，但状态要落到轮上。

    与"过期"的区别：不是覆盖不全（未覆盖），而是结构错（重叠/空域）——重做也修不了，
    必须人工查；此时**不回入水位**（避免反复烧钱重做同一个错）。
    """
    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path)
    task = Task.create(goal="g")
    task.rounds = [_mk_file_round(1, "写 a.py", ["a.py"])]
    task.rounds[0]["org_state"] = ""
    product = [
        {"name": "A 域", "description": "…", "file": "a.py"},
        {"name": "B 域", "description": "…", "file": "a.py"},     # 重叠：根基缺陷
    ]
    task.rounds[0]["pending_org"] = {"domains": product}
    task.rounds[0]["org_state"] = "done"
    task.rounds[0]["org_generation"] = 1
    task.rounds[0]["end_state"] = "completed"
    agent = Agent(llm=_StubLLM(), tools=[], task=task, org_watermark=10**9)

    agent._promote_org_results()

    assert task.rounds[0]["org_state"] == "done", "根基缺陷不自动回入水位（人工介入）"
    assert task.rounds[0]["split_state"] == "rejected"
    assert any(e.get("kind") == "split_defect" for e in task.history)
    assert [str(e.get("id")) for e in task.registry] == ["Main"]


def _early_fixture(monkeypatch, tmp_path, *, prior_tree=False, newer_round=False):
    """A 步夹具：一批已整理（产物暂存）+ 一个**开放轮**（模拟用户正在对话）。"""
    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path)
    task = Task.create(goal="g")
    if prior_tree:
        # 已有旧域树生效（开放轮因此走**视图路径**）
        task.rounds = [_mk_file_round(1, "写 a.py", ["a.py"]),
                       _mk_file_round(2, "写 b.py", ["b.py"])]
        task.rounds[0]["domains"] = [{"name": "旧域", "description": "…", "file": "a.py"}]
        task.rounds[0]["end_state"] = "completed"
        task.rounds[0]["org_state"] = "done"
        task.rounds[1]["end_state"] = "completed"
        task.rounds[1]["org_state"] = "done"
        product = [{"name": "B 域", "description": "b.py 这条线", "file": "b.py"}]
        task.rounds[1]["pending_org"] = {"domains": product}
        task.rounds[1]["org_generation"] = 2
    else:
        task.rounds = [_mk_file_round(1, "写 a.py", ["a.py"])]
        task.rounds[0]["end_state"] = "completed"
        task.rounds[0]["org_state"] = "done"
        task.rounds[0]["org_generation"] = 1
        product = [{"name": "A 域", "description": "a.py 这条线", "file": "a.py"}]
        task.rounds[0]["pending_org"] = {"domains": product}
    if newer_round:
        # 之后又有一轮闭合（写了新文件）→ 材料变了（过期判据的一半）
        nr = _mk_file_round(3, "写 c.py", ["c.py"])
        nr["end_state"] = "completed"
        nr["org_state"] = ""
        task.rounds.append(nr)
        # 另一半：产物声明的**范围覆盖不到**新文件（2026-09-15 起材料过期的
        # 信号是 `uncovered_by_scope`，不再是"未覆盖"缺陷——那个已不拒收）
        for rr in task.rounds:
            pending = rr.get("pending_org")
            if pending:
                pending["uncovered_by_scope"] = ["c.py"]
    # 开放轮：用户正在说话
    open_round = _mk_read_round(9, "正在聊", [])
    open_round["end_state"] = "open"
    open_round["org_state"] = ""
    open_round["events"].append({
        "id": "R9-E99", "type": "user", "message": {"role": "user", "content": "正在聊"}})
    task.rounds.append(open_round)
    agent = Agent(llm=_StubLLM(), tools=[], task=task, org_watermark=10**9)
    agent.current_round = None          # 另一个实例的轮：本实例只是维护线程
    return agent, task


def test_early_publish_lands_registry_while_round_open(monkeypatch, tmp_path):
    """A 步：全量路径下，产物就绪就落**注册表**（子 agent 即时可见），不等轮边界。

    用户口径更正（2026-09-15）：「下一轮闭合生效」只指**上下文替换**。全量路径的装配
    不读注册表（职责表只在视图路径的系统段），所以子 agent 出现不必等。
    """
    agent, task = _early_fixture(monkeypatch, tmp_path)
    landed = agent._publish_product_early()

    assert landed == 1
    assert [str(e.get("id")) for e in task.registry] == ["Main", "A"], "子 agent 应立即出现"
    assert task.rounds[0]["pending_org"].get("registry_landed") is True
    assert not task.rounds[0].get("domains"), "域树（上下文替换）仍必须等轮边界"
    assert task.rounds[0].get("split_state") == "ready"
    assert any("提前落实" in str(e.get("detail")) for e in task.history)
    # 开放轮没被碰：字节相关字段一律未写
    assert task.rounds[-1].get("domains") is None


def test_early_publish_skips_registry_on_view_path(monkeypatch, tmp_path):
    """开放轮**已在视图路径**（之前已有域树）时，职责表在它的系统段里 → 注册表等边界。"""
    agent, task = _early_fixture(monkeypatch, tmp_path, prior_tree=True)
    before = [str(e.get("id")) for e in task.registry]

    agent._publish_product_early()

    assert [str(e.get("id")) for e in task.registry] == before, \
        "视图路径下提前落注册表会改开放轮的系统段（前缀断裂）"


def test_promote_after_early_publish_is_idempotent(monkeypatch, tmp_path):
    """A 步先落注册表、B 步（轮闭合）落地域树：不许重复落、不许丢 `domains`。"""
    agent, task = _early_fixture(monkeypatch, tmp_path)
    agent._publish_product_early()
    assert task.rounds[0]["pending_org"].get("registry_landed") is True

    # 轮闭合 → B 步
    agent.rounds[-1]["end_state"] = "completed"
    agent._settle_after_maintenance()

    ids = [str(e.get("id")) for e in task.registry]
    assert ids == ["Main", "A"], f"注册表重复或缺失：{ids}"
    assert task.rounds[0].get("domains"), "轮边界必须把域树落下来"
    assert task.rounds[0].get("split_state") == "done"


def test_stale_requeues_immediately_without_boundary(monkeypatch, tmp_path):
    """过期产物**立刻**回入水位（纯账目），不必等轮闭合——这是卡死会话的自愈路径。"""
    # 夹具注意：新轮必须在构造 Agent **之前**挂上（Agent 构造时浅拷贝轮列表）
    agent, task = _early_fixture(monkeypatch, tmp_path, newer_round=True)

    landed = agent._publish_product_early()

    assert landed == 1
    assert task.rounds[0]["org_state"] == "failed", "过期批次应立即回入水位"
    assert task.rounds[0].get("split_state") == "stale"
    assert not task.rounds[0].get("pending_org"), "过期产物应丢弃（下批重做）"
    assert any(e.get("kind") == "split_stale" for e in task.history)
    assert [str(e.get("id")) for e in task.registry] == ["Main"], "过期产物不许落注册表"


def test_early_publish_lands_envelope_and_display_fields(monkeypatch, tmp_path):
    """A 步（2026-09-15 用户裁定"能提前的都提前"）：信封类/展示类字段立刻落。

    * `state_patch` → 账本只进**尾部信封**（每步重算，不是历史，动它不破前缀）；
    * `unassigned`/`split_assessment` → 纯展示（装配不读）。
    仍留在暂存区的只有**上下文替换**（`domains`）。
    """
    agent, task = _early_fixture(monkeypatch, tmp_path)
    po = task.rounds[0]["pending_org"]
    po["state_patch"] = {"current_status": "提前落的现状"}
    po["unassigned"] = {"block_ids": ["R1-B9"], "reason": "闲聊"}
    po["split_assessment"] = {"splittable": True, "reason": "两条线"}

    agent._publish_product_early()

    assert task.rounds[0].get("unassigned") == {"block_ids": ["R1-B9"], "reason": "闲聊"}
    assert task.rounds[0].get("split_assessment") == {"splittable": True, "reason": "两条线"}
    assert "提前落的现状" in str(task.get_state().current_status)
    left = task.rounds[0].get("pending_org") or {}
    assert "state_patch" not in left and "unassigned" not in left
    assert left.get("domains"), "上下文替换（domains）仍必须等轮闭合"


def test_early_publish_applies_attribution_on_full_path_only(monkeypatch, tmp_path):
    """闭合轮归属（预备值）：全量路径立刻生效；视图路径（开放轮读它）仍等边界。"""
    from wovra import registry as registry_module

    agent, task = _early_fixture(monkeypatch, tmp_path)
    # 夹具注意：Agent 构造时浅拷贝轮列表，`pending_view` 要挂在 **agent 手上那份**
    # （真实流程里预备值是维护线程写进自己的 rounds、再随落盘并集合并）
    product = agent.rounds[0]["pending_org"]["domains"]
    agent.rounds[0]["pending_view"] = {
        "view": "A 域", "reason": "文件命中",
        "domains": registry_module.domains_digest(product),
    }
    agent._publish_product_early()
    assert agent.rounds[0]["active_view"] == "A 域", "全量路径下归属应立刻生效"
    assert "pending_view" not in agent.rounds[0]

    # 视图路径：开放轮之前已有域树 → 归属在它的历史里被读 → 等边界
    agent2, task2 = _early_fixture(monkeypatch, tmp_path, prior_tree=True)
    product2 = agent2.rounds[1]["pending_org"]["domains"]
    agent2.rounds[1]["pending_view"] = {
        "view": "B 域", "reason": "文件命中",
        "domains": registry_module.domains_digest(product2),
    }
    agent2._publish_product_early()
    assert agent2.rounds[1].get("active_view") in (None, "", "Main"), \
        "视图路径下提前写归属会改开放轮历史字节"
    assert agent2.rounds[1].get("pending_view"), "预备值应留到轮边界"


def test_split_binds_files_by_declared_path_scopes(monkeypatch, tmp_path):
    """**文件归属由代码算**（2026-09-15 用户："分裂只需要写结构树和每个 agent 的
    职责，其它都不需要搞了"）：节点用 `path`/`paths` 声明范围 → 每个活性文件按
    **最深前缀**机械归属；精确文件优先于目录前缀；没声明到的走 `_auto_claim`
    （同目录/最近/机械桶），**绝不落主 agent**。

    这条钉住"归属不再依赖模型逐文件填编号"——那正是产物被截断、被整批拒收的
    头号来源（实测：一次截断只抢救出 22 个节点；另一次两个历史文件落点把整批
    作废，4 轮全 rejected、注册表只剩 Main）。
    """
    agent = Agent(llm=_StubLLM(), tools=[])
    live = ["src/wovra/tools/web.py", "src/wovra/tools/eyes.py",
            "src/wovra/agent/core.py", "docs/a.md", "README.md"]
    monkeypatch.setattr(agent, "_live_files", lambda: live)
    doms = [
        {"name": "工具层", "path": "src/wovra/tools/"},
        {"name": "运行时", "path": "src/wovra/agent/"},
        {"name": "精确定位", "path": "src/wovra/tools/web.py"},   # 精确文件优先
        {"name": "文档", "paths": ["docs/"]},
    ]

    _notes, uncovered = agent._bind_files_by_path(doms)

    by_name = {d["name"]: d for d in doms}
    assert by_name["精确定位"]["files"] == ["src/wovra/tools/web.py"]
    assert by_name["工具层"]["files"] == ["src/wovra/tools/eyes.py"]
    assert by_name["运行时"]["files"] == ["src/wovra/agent/core.py"]
    assert by_name["文档"]["files"] == ["docs/a.md"]
    # `README.md` 没有任何范围命中 → 机械桶接住（不是主 agent）
    assert uncovered == ["README.md"]
    buckets = [d for d in doms if d.get("runtime_auto")]
    assert buckets and buckets[0]["files"] == ["README.md"]
    # 范围声明用完即清：归宿只有 `files` 一处真源（否则前缀会与清单重叠）
    assert not any(d.get("file_domains") for d in doms)


def test_split_binding_fills_description_from_file_head(monkeypatch, tmp_path):
    """没有描述的节点：描述**取它第一份文件的开头**（首行注释/标题）——
    用户口径"文件描述可以窃取文件开头一部分"，永远新鲜、不用模型抄。"""
    from wovra import task as task_module2
    from wovra.tools import safety as safety_module

    monkeypatch.setattr(task_module2, "TASKS_ROOT", tmp_path / "tasks")
    ws = tmp_path / "ws"
    (ws / "src" / "demo").mkdir(parents=True)
    (ws / "src" / "demo" / "a.py").write_text(
        "# 演示模块：把输入转成输出\nimport os\n", encoding="utf-8")
    monkeypatch.setattr(safety_module, "PROJECT_ROOT", ws)
    agent = Agent(llm=_StubLLM(), tools=[])
    monkeypatch.setattr(agent, "_live_files", lambda: ["src/demo/a.py"])
    doms = [{"name": "演示线", "path": "src/demo/"}]

    agent._bind_files_by_path(doms)

    assert doms[0]["files"] == ["src/demo/a.py"]
    assert doms[0]["description"].startswith("演示模块")   # 首行注释被"窃取"
