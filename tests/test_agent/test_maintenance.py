"""Agent 运行时测试（自 test_agent.py 拆分，2026-09-11）。

本模块：test_maintenance。"""

import json
from wovra import task as task_module
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


def test_organization_no_retry_on_invalid_json(monkeypatch, tmp_path):
    """整理输出非法 JSON 时整批失败，不重试（2026-09-10 用户拍板：
    重试只是再付一遍完整生成，不能确定解决失败）。"""
    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path)
    responses = [
        [_chunk(_delta(content="干完了"))],
        [_chunk(_delta(content="我觉得应该这样：blahblah"))],  # 非法产物
    ]
    task = Task.create(goal="目标")
    agent = Agent(llm=_StubLLM(responses), tools=[], task=task, org_watermark=0, org_grace_rounds=0, org_cooldown_rounds=0)

    agent.run("问")

    assert len(agent.llm.calls) == 2          # 只有干活 + 整理各一次，无重试
    assert task.rounds[-1]["org_state"] == "failed"   # 整批失败，回入水位
    assert task.task_state == {}              # 补丁未应用
    assert task.rounds[-1]["user_input"]["normalized"] == ""


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
    # 工具契约（2026-09-10 用户拍板）：org 收窄为只留 submit_organization
    # ——关思考后模型会把整理指令当普通工作对话乱调工具（实测去调
    # write_file），收窄工具集是硬约束；代价是 org 路前缀缓存不再可骑
    # （维护调用低频，可接受）
    assert [t["function"]["name"] for t in agent.llm.calls[1]["tools"]] == [
        "submit_organization"
    ]
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
    # 复刻真凶：长中文叙述里混进未转义的双引号 → JSON 提前断串
    broken = (
        '{"rounds": [{"seq": 1, "normalized_user_input": "读源码", '
        '"key_constraints": "", "block_summaries": [{"id": "R1-B1", '
        '"summary": "于是"读到含\'不存在\'的文件"整块被打成幽灵"}]}], '
        '"state_patch": {}}'
    )
    org_chunk = _chunk(_delta(tool_calls=[
        _fragment(0, id="c9", name="submit_organization", arguments=broken),
    ]))
    task = Task.create(goal="目标")
    agent = Agent(llm=_StubLLM([[_chunk(_delta(content="好"))], [org_chunk]]),
                  tools=[], task=task, org_watermark=0, org_grace_rounds=0,
                  org_cooldown_rounds=0)

    agent.run("问")

    assert task.rounds[-1]["org_state"] == "failed"
    records = [e["detail"] for e in task.history if e["kind"] == "maintenance"]
    evidence = [d for d in records if "org：无可用产物" in d]
    assert evidence, f"失败现场未落 history：{records}"
    text = evidence[0]
    assert "submit_organization 参数" in text
    assert "JSON 解析失败" in text and "位置" in text
    assert "现场" in text  # 出错位置附近的原文片段


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
    """保护机制：会话前 N 轮硬豁免维护（宽限），两次维护之间最小轮距
    （冷却）——适配大项目起点，窗口保底紧急折叠不受豁免（另一条线）。"""
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
        [_chunk(_delta(content="R5"))],                    # R5（冷却 1<3）
        [_chunk(_delta(content="R6"))],                    # R6（冷却 2<3）
        [_chunk(_delta(content="R7"))],                    # R7
        [_chunk(_delta(content=org_json(5, 6, 7)))],       # R7 触发（间隔 3 达标）
    ]
    task = Task.create(goal="目标")
    agent = Agent(llm=_StubLLM(responses), tools=[], task=task,
                  org_watermark=0, org_grace_rounds=3, org_cooldown_rounds=3)

    for i in range(1, 8):
        agent.run(f"轮{i}")

    org_calls = [c for c in agent.llm.calls if c.get("lane") == "org"
                 and "[整理指令]" in str(c["messages"][-1].get("content", ""))]
    assert len(org_calls) == 2                       # R4、R7 两次
    # 宽限期的证据是调用时点（R1-R3 闭合时零维护调用）；R4 批次把它们
    # 一并收编属正常语义，故只断言 R1 无产物
    assert task.rounds[0]["user_input"]["normalized"] == ""
    assert task.rounds[3]["org_state"] == "done"     # R4 整理
    assert task.rounds[4]["org_state"] == "done"     # R7 批次补齐 R5-R7
    assert task.rounds[4]["pending_org"]["normalized"] == "R5 意图"


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

    # 工具契约（2026-09-10 用户拍板）：org/split 各自收窄为唯一出口
    # 工具（submit_organization / submit_domains），不再与工作共用数组
    org_calls = [c for c in agent.llm.calls if c.get("lane") == "org"]
    split_calls = [c for c in agent.llm.calls if c.get("lane") == "split"]
    assert org_calls and split_calls
    assert [t["function"]["name"] for t in org_calls[0]["tools"]] == [
        "submit_organization"
    ]
    assert [t["function"]["name"] for t in split_calls[0]["tools"]] == [
        "submit_domains"
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
    """submit_domains 参数解析失败：不静默落空——保守"不可分"落档 + 留痕。"""
    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path)
    bad = _chunk(_delta(tool_calls=[
        _fragment(0, id="d1", name="submit_domains",
                  arguments='{"domains": [{"name": "x"'),
    ]))
    agent, task = _split_fixture(
        monkeypatch, tmp_path,
        org_pool=[[_chunk(_delta(content=_org_json()))]],
        split_pool=[[bad]],
    )
    agent._maybe_organize_batch()
    sa = task.rounds[0]["pending_org"]["split_assessment"]
    assert sa["splittable"] is False
    assert "无可用产物" in sa["reason"]
    assert any(
        "无可用产物" in e.get("detail", "") for e in task.history
        if e.get("kind") == "maintenance"
    )


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


def test_split_skipped_when_org_fails(monkeypatch, tmp_path):
    """org 失败则 split 跳过（分裂依赖整理质量，失败批次不产出）。"""
    agent, task = _split_fixture(
        monkeypatch, tmp_path, [], [[_domains_chunk()]],  # org 池空 → 失败
    )
    agent._maybe_organize_batch()

    assert task.rounds[0]["org_state"] == "failed"
    assert not any(c.get("lane") == "split" for c in agent.llm.calls)


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
