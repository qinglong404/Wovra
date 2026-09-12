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
    # 产物先暂存（本轮装配纹丝不动）：正式字段要等下一轮开启才替换
    assert task.rounds[-1]["pending_org"]["normalized"] == "用户想搞清楚项目的测试覆盖情况"
    assert task.rounds[-1]["pending_org"]["key_constraints"] == "不得修改现有测试用例"
    assert "R1-B1" in task.rounds[-1]["pending_org"]["block_summaries"]
    assert task.rounds[-1]["user_input"]["normalized"] == ""
    # 模拟下一轮开启：暂存生效
    agent._promote_org_results()
    # State Patch 增量合并进任务状态
    assert task.task_state["goal"] == "搞清测试覆盖"
    assert task.task_state["is_done"] is True
    assert task.task_state["completed"] == ["梳理测试覆盖"]
    # Round 结构持久化：意图 / 关键约束 / 逐块描述写回
    assert task.rounds[-1]["user_input"]["normalized"] == "用户想搞清楚项目的测试覆盖情况"
    assert task.rounds[-1]["user_input"]["key_constraints"] == "不得修改现有测试用例"
    assert task.rounds[-1]["block_summaries"]["R1-B1"].startswith("回应测试覆盖询问")
    assert task.rounds[-1]["org_state"] == "done"
    assert "pending_org" not in task.rounds[-1]  # 生效后暂存区清空


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
    assert (task.rounds[-1]["pending_org"]["normalized"]
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
    assert task.rounds[-1]["pending_org"]["normalized"] == "修正后的意图"

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

    assert task.rounds[-1]["pending_org"]["normalized"] == "位置匹配的意图"
    assert "R1-B1" in task.rounds[-1]["pending_org"]["block_summaries"]


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
    pending = task.rounds[-1]["pending_org"]
    assert pending["normalized"] == "意图'带单引号'"
    assert "第二行" in pending["block_summaries"]["R1-B1"]


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
    assert task.rounds[4]["pending_org"]["normalized"] == "R5 意图"
    # 中间三轮在 R5/R6/R7 闭合时都被最小间隔挡住——它们的产物只能来自
    # R8 那次批次（这正是"最少 3 轮内不触发"的直接证据：若沿用旧的 `<`，
    # R7 闭合就会触发第三批，org_calls 会是 3 次）
    for r in task.rounds[4:7]:
        assert r.get("user_input", {}).get("normalized", "") == ""


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
    assert r1["pending_org"]["split_assessment"]["splittable"] is False
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
    assert sa["splittable"] is False
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
    assert chat_bid in task.rounds[0]["pending_org"]["unassigned"]["block_ids"]


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
        assert "[分裂分析指令]" in str(appended[2]["content"])  # 分裂指令
    else:
        # 正文 JSON fallback：assistant 原文 + 分裂指令
        assert appended[0]["role"] == "assistant"
        assert "[分裂分析指令]" in str(appended[1]["content"])
    # 开思考（不传 thinking disabled）：语义判断需要推理（用户拍板）
    assert "extra_body" not in split_calls[0] or not split_calls[0].get("extra_body")
    # 产物：thoughts 与 domains 都落暂存
    assert task.rounds[0]["pending_org"]["domains"][0]["name"] == "web 演示"
    assert task.rounds[0]["pending_org"]["split_assessment"]["splittable"] is False


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
    assert [e["id"] for e in task.registry] == ["A"]   # promote 前只有主 agent
    agent._maybe_organize_batch()
    # 产物仍在暂存区——注册表此刻还不该动（原子生效协议）
    assert [e["id"] for e in task.registry] == ["A"]

    agent._promote_org_results()
    ids = [e["id"] for e in task.registry]
    assert ids == ["A", "A-1"]                          # 域树 → 路径 ID
    entry = task.registry[1]
    assert entry["name"] == "web 演示"
    assert entry["description"] == "纯 HTML 演示页，产出可视化灵感"
    assert entry["file_domains"] == ["index.html"]
    assert entry["status"] == "dormant"                 # 休眠是默认态
    # 留痕：注册表变更进 maintenance 账本，可查
    assert any(
        "registry：分裂产物落实为注册表条目" in str(h.get("detail"))
        for h in task.history if h.get("kind") == "maintenance"
    )

    # 幂等：重复 promote（模拟崩溃补做）不重复追加，条目数不变
    agent._promote_org_results()
    assert [e["id"] for e in task.registry] == ["A", "A-1"]


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
    assert "各视图自身体量" in prompt            # 硬数据行
    assert "工具层" in prompt
    assert "考虑在其内部再裂一层" in prompt      # 到水位的提示语在判据里
    assert "逐层分裂（正式机制" in prompt        # 判据第 4 条（不是可选项）


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
    assert [e["id"] for e in task.registry] == ["A", "A-1"]


def test_split_hard_data_lists_live_files(monkeypatch, tmp_path):
    """硬数据（零 LLM）：活性文件清单 + 数量；dead 文件不占上限。"""
    agent = Agent(llm=_StubLLM(), tools=[])
    agent.rounds = [_mk_file_round(1, "写文件", ["a.txt", "b.txt"])]
    lines, n = agent._split_hard_data(agent.rounds)
    assert n == 2
    joined = "\n".join(lines)
    assert "a.txt" in joined and "b.txt" in joined
    assert "活性文件数" not in joined  # 数量由调用方拼接


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


def test_maint_snapshot_defers_when_protocol_incomplete(monkeypatch, tmp_path):
    """协议闸门（2026-09-11 400 实测的修法）：闭合发生在**工具方法体内部**
    时（todo→verify_milestone→close_round），调用方的 tool 结果尚未落盘，
    装配尾部就是未回复的 tool_calls——此刻取快照、追加整理指令，严格端点
    直接 400（实测 21:27 那批 1 秒失败、未计费，产物只能等下一批补上）。

    修法：此刻不取快照，置 deferred；该调用的 tool 结果落盘后自动补做。
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
    agent.todo(action="start_milestone", goal="大步甲", acceptance=["可跑"])
    agent.todo(action="add_step", text="小步一")
    agent.todo(action="check_step", text="小步一")
    args = json.dumps({"action": "verify_milestone", "evidence": "测试全绿"})
    # 主循环的真实形态：先记 assistant(tool_calls)，再执行工具
    agent._record_event("tool_call", {
        "role": "assistant", "content": "",
        "tool_calls": [{"id": "call_1", "type": "function",
                        "function": {"name": "todo", "arguments": args}}],
    })
    agent.last_context_estimate = 5000
    # 闭合那一刻的装配：尾部正是这条未回复的 tool_call → 闸门拦下
    assert agent._maint_snapshot() is None
    assert _dangling(agent._assemble_messages()) == ["call_1"]

    agent._execute("call_1", "todo", args)  # 内部 close_round → 水位检查

    assert agent._maint_deferred is False  # 已补做，标记复位
    org_calls = [c for c in agent.llm.calls
                 if any("[整理指令]" in str(m.get("content") or "")
                        for m in c["messages"])]
    assert len(org_calls) == 1, "结果落盘后应补发起整理（不吞掉这次触发）"
    assert _dangling(org_calls[0]["messages"]) == []  # 补做时输入协议完整
    details = [e["detail"] for e in task.history if e["kind"] == "maintenance"]
    assert any("水位检查推迟" in d for d in details), details


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
