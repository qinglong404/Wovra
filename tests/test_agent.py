"""Agent 纯逻辑部分的测试：schema 生成、错误回传、路径安全。

不发起任何真实模型调用——Agent 只有 run() 会碰 llm.chat，
构造时传一个什么都不做的 stub 即可。
"""

import json
import re
import sys
from types import SimpleNamespace

import pytest

from wovra import task as task_module
from wovra.agent import Agent, _schema_of, read_file
from wovra.task import Task, TaskState


class _StubLLM:
    """替身 LLM：按消息内容标记路由到整理/分裂/consult 三个响应池。

    池为空时先抛错再记 calls——失败路的空池不产生调用记录，既有
    断言（calls[0] 是整理路）保持确定性。
    """

    model = "stub"

    def __init__(self, responses: list | None = None,
                 split_responses: list | None = None,
                 consult_responses: list | None = None):
        self.responses = list(responses or [])
        self.split_responses = list(split_responses or [])
        self.consult_responses = list(consult_responses or [])
        self.calls: list[dict] = []

    def chat(self, messages, tools=None, stream=False, **kwargs):
        texts = [str(m.get("content") or "") for m in messages]
        if any("[分裂分析指令]" in t for t in texts):
            pool, lane = self.split_responses, "split"
        elif any("主对话正就以下问题" in t for t in texts):
            pool, lane = self.consult_responses, "consult"
        else:
            pool, lane = self.responses, "org"
        if not pool:
            raise IndexError(f"无预备响应（{lane}路）")
        self.calls.append({
            "messages": messages, "stream": stream, "tools": tools, "lane": lane,
        })
        return iter(pool.pop(0))


def _agent_with(tools, responses=None) -> Agent:
    return Agent(llm=_StubLLM(responses), tools=tools)


# ---- 流式协议分块的替身构造 -------------------------------------------------


def _delta(content=None, tool_calls=None, reasoning=None):
    return SimpleNamespace(
        content=content,
        tool_calls=tool_calls,
        reasoning_content=reasoning,
        model_extra=None,
    )


def _fragment(index, id=None, name=None, arguments=None):
    """一个 tool_call 分片：流式下 name 和 arguments 是分次到达的。"""
    return SimpleNamespace(index=index, id=id, function=SimpleNamespace(name=name, arguments=arguments))


def _chunk(delta=None, usage=None, finish_reason=None):
    choices = [] if delta is None else [
        SimpleNamespace(delta=delta, finish_reason=finish_reason)
    ]
    return SimpleNamespace(choices=choices, usage=usage)


def _usage(prompt, completion, total, reasoning=None, cached=None):
    completion_details = SimpleNamespace(reasoning_tokens=reasoning) if reasoning else None
    # cached=None 时不提供 prompt_tokens_details，模拟服务端不支持缓存统计
    prompt_details = (
        SimpleNamespace(cached_tokens=cached) if cached is not None else None
    )
    return SimpleNamespace(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=total,
        completion_tokens_details=completion_details,
        prompt_tokens_details=prompt_details,
    )


# ---- schema 生成 ---------------------------------------------------------


def test_schema_from_type_hints():
    def demo_tool(count: int, label: str = "x"):
        """工具的一句话描述。"""

    schema = _schema_of(demo_tool)
    fn = schema["function"]
    assert fn["name"] == "demo_tool"
    assert fn["description"] == "工具的一句话描述。"
    assert fn["parameters"]["properties"]["count"] == {"type": "integer"}
    assert fn["parameters"]["properties"]["label"] == {"type": "string"}
    # label 有默认值 → 非必填
    assert fn["parameters"]["required"] == ["count"]


def test_schema_without_annotation_falls_back_to_string():
    def loose_tool(anything):
        """无注解参数。"""

    props = _schema_of(loose_tool)["function"]["parameters"]["properties"]
    assert props["anything"] == {"type": "string"}


# ---- 工具执行的三种失败路径：错误都回传给模型，而不是抛出 --------------------


def test_unknown_tool_returns_error_text():
    agent = _agent_with([])
    agent._execute("call_1", "不存在的工具", "{}")

    last = agent.messages[-1]
    assert last["role"] == "tool"
    assert "未知工具" in last["content"]


def test_invalid_json_arguments_returns_error_text():
    def ok_tool(a: int):
        """参数一个。"""

    agent = _agent_with([ok_tool])
    agent._execute("call_1", "ok_tool", "{不是json")

    assert "合法 JSON" in agent.messages[-1]["content"]


def test_tool_exception_returns_error_text():
    def boom():
        """必然抛错。"""
        raise ValueError("炸了")

    agent = _agent_with([boom])
    agent._execute("call_1", "boom", "{}")

    assert "工具执行出错" in agent.messages[-1]["content"]
    assert "炸了" in agent.messages[-1]["content"]


def test_non_string_result_is_serialized():
    def make_list():
        """返回 list。"""
        return ["a", "b"]

    agent = _agent_with([make_list])
    agent._execute("call_1", "make_list", "{}")

    assert json.loads(agent.messages[-1]["content"]) == ["a", "b"]


# ---- 流式循环：分片聚合、回调、用量统计 -------------------------------------


def test_streaming_accumulates_fragmented_tool_call():
    """同一工具调用的参数 JSON 分两个分片到达，必须聚合后执行。"""

    def echo(text: str):
        """原样返回。"""
        return text

    responses = [
        # 第一次调用：工具调用分两片到达
        [
            _chunk(_delta(tool_calls=[_fragment(0, id="c1", name="echo", arguments='{"text"')])),
            _chunk(_delta(tool_calls=[_fragment(0, arguments=':"你好"}')])),
        ],
        # 第二次调用：拿到工具结果后给出最终回答
        [_chunk(_delta(content="完成"))],
    ]
    agent = _agent_with([echo], responses)

    answer = agent.run("开始")

    assert answer == "完成"
    # 回传给模型的工具结果正是聚合后的参数执行所得
    tool_msg = next(m for m in agent.messages if m["role"] == "tool")
    assert tool_msg["content"] == "你好"


def test_streaming_forwards_thinking_and_answer_deltas():
    thinking_seen, answer_seen = [], []

    responses = [[
        _chunk(_delta(reasoning="先想一下")),
        _chunk(_delta(content="最终")),
        _chunk(_delta(content="回答")),
        _chunk(usage=_usage(10, 20, 30, reasoning=8)),
    ]]
    agent = _agent_with([], responses)

    answer = agent.run(
        "问", on_thinking=thinking_seen.append, on_answer_delta=answer_seen.append
    )

    assert answer == "最终回答"
    assert thinking_seen == ["先想一下"]
    assert answer_seen == ["最终", "回答"]
    # 用量跨分块聚合，思考 token 单独记录；提示词分类各字段齐备
    assert agent.last_stats["prompt_tokens"] == 10
    assert agent.last_stats["completion_tokens"] == 20
    assert agent.last_stats["reasoning_tokens"] == 8
    assert agent.last_stats["total_tokens"] == 30
    assert set(agent.last_stats["prompt_breakdown"]) == {
        "system", "context", "tools", "user", "assistant", "tool",
    }


def test_usage_accumulates_across_turns():
    """一次 run 内多次 LLM 调用（工具轮 + 回答轮）的用量要累加。"""

    def noop():
        """什么也不做。"""

    responses = [
        # 工具轮：分片 + 该轮流量的 usage 分块（choices 为空）
        [
            _chunk(_delta(tool_calls=[_fragment(0, id="c1", name="noop", arguments="{}")])),
            _chunk(usage=_usage(10, 5, 15)),
        ],
        # 回答轮：内容 + 流量
        [_chunk(_delta(content="ok")), _chunk(usage=_usage(20, 10, 30))],
    ]
    agent = _agent_with([noop], responses)

    agent.run("go")

    assert agent.last_stats["prompt_tokens"] == 30
    assert agent.last_stats["completion_tokens"] == 15
    assert agent.last_stats["total_tokens"] == 45


def test_turns_steps_tool_calls_and_cache_accounting():
    def noop():
        """什么也不做。"""

    responses = [
        [
            _chunk(_delta(tool_calls=[_fragment(0, id="c1", name="noop", arguments="{}")])),
            _chunk(usage=_usage(10, 2, 12, cached=4)),
        ],
        [_chunk(_delta(content="ok")), _chunk(usage=_usage(20, 8, 28, cached=6))],
        # 第二次 run：服务端这次没返回 prompt_tokens_details
        [_chunk(_delta(content="done")), _chunk(usage=_usage(5, 1, 6))],
    ]
    agent = _agent_with([noop], responses)

    agent.run("第一轮")
    stats = agent.last_stats
    # 一次 run = 2 步（工具轮 + 回答轮），1 次工具调用
    assert stats["turn"] == 1
    assert stats["llm_calls"] == 2
    assert stats["tool_calls"] == 1
    # 缓存命中累加：4 + 6；未命中 = 各轮 prompt - 命中
    assert stats["cached_tokens"] == 10
    assert stats["cache_miss_tokens"] == (10 - 4) + (20 - 6)

    agent.run("第二轮")
    assert agent.last_stats["turn"] == 2  # 轮次跨 run 累计
    assert agent.last_stats["llm_calls"] == 1
    # 服务端没返回缓存明细 → 按 0 命中计入未命中（保守口径）
    assert agent.last_stats["cached_tokens"] == 0
    assert agent.last_stats["cache_miss_tokens"] == 5


def test_run_always_uses_streaming():
    """run() 统一走流式——这是成本核算和实时展示的前提。"""
    responses = [[_chunk(_delta(content="hi"))]]
    agent = _agent_with([], responses)
    agent.run("问")
    assert agent.llm.calls[0]["stream"] is True


def test_empty_stream_retries_then_keeps_round_open():
    """流被端点掐断（只有思考，正文与 usage 均未到）→ 空串不是最终回答。

    自动重试至多 2 次；仍空则 raise 且轮保持开放（end_state 缺省，\\c 可续）。
    """
    responses = [
        [_chunk(_delta(reasoning="想了一半")), _chunk(_delta())],
        [_chunk(_delta(reasoning="又想了一半"))],
        [_chunk(_delta(reasoning="还是空"))],
    ]
    agent = _agent_with([], responses)
    with pytest.raises(RuntimeError, match="保持开放"):
        agent.run("第一轮")
    assert len(agent.llm.calls) == 3  # 空响应自动重试了两次
    # 轮未闭合：没有 final_answer 事件，end_state 保持 open（\c 可续）
    assert not any(e["type"] == "final_answer" for e in agent.rounds[-1]["events"])
    assert agent.rounds[-1].get("end_state") == "open"
    # 中断通知记为持久轮事件，且每轮只记一次（重试/续跑时模型都知道失败过）
    notes = [e for e in agent.rounds[-1]["events"] if e["type"] == "runtime_note"]
    assert len(notes) == 1
    assert "异常终止" in notes[0]["message"]["content"]


def test_resume_after_empty_stream_sees_interruption_note():
    """重试耗尽 → \\c 续跑：中断通知在轮事件里，续跑调用的输入带 steering，
    模型不再盲目从头重想 13 分钟。"""
    responses = [
        [_chunk(_delta(reasoning="想了一半"))],
        [_chunk(_delta(reasoning="又想了一半"))],
        [_chunk(_delta(reasoning="还是空"))],
    ]
    agent = _agent_with([], responses)
    with pytest.raises(RuntimeError, match="保持开放"):
        agent.run("第一轮")
    agent.llm.responses = [[_chunk(_delta(content="续上了"))]]
    assert agent.resume() == "续上了"
    last_messages = agent.llm.calls[-1]["messages"]
    assert any(
        "runtime-reminder" in str(m.get("content"))
        and "压缩思考" in str(m.get("content"))
        for m in last_messages
    )


def test_empty_stream_retry_recovers_and_closes_round():
    """第一次空响应重试后拿到正文 → 正常闭合轮次。"""
    responses = [
        [_chunk(_delta(reasoning="断了"))],
        [_chunk(_delta(content="好了"))],
    ]
    agent = _agent_with([], responses)
    assert agent.run("问") == "好了"
    assert agent.rounds[-1]["end_state"] == "completed"


def test_empty_stream_with_length_finish_raises_without_retry():
    """finish_reason=length（输出上限）：重试必再撞上限，不重试直接上报。"""
    responses = [[_chunk(_delta(reasoning="想多了"), finish_reason="length")]]
    agent = _agent_with([], responses)
    with pytest.raises(RuntimeError, match="length"):
        agent.run("第一轮")
    assert len(agent.llm.calls) == 1


def _boom_stream():
    """流中途抛服务端错误（实测 2026-09-08：internal error 打断思考流）。"""
    import openai

    yield _chunk(_delta(reasoning="想了一半"))
    raise openai.APIError(
        "The service encountered an unexpected internal error. Request id: 0217",
        None,
        body=None,
    )


def test_midstream_api_error_retries_then_keeps_round_open():
    """服务端流中途报错（APIError 非 RuntimeError 家族）→ 转 LLMStreamError
    并入空响应护栏：重试至多 2 次，仍错则 raise 且轮保持开放，进程不崩。"""
    agent = _agent_with([], [_boom_stream(), _boom_stream(), _boom_stream()])
    with pytest.raises(RuntimeError, match="保持开放"):
        agent.run("第一轮")
    assert len(agent.llm.calls) == 3
    assert not any(e["type"] == "final_answer" for e in agent.rounds[-1]["events"])
    assert agent.rounds[-1].get("end_state") == "open"


def test_midstream_api_error_retry_recovers():
    """第一次流中途报错、重试拿到正文 → 正常闭合轮次。"""
    responses = [_boom_stream(), [_chunk(_delta(content="恢复"))]]
    agent = _agent_with([], responses)
    assert agent.run("问") == "恢复"
    assert agent.rounds[-1]["end_state"] == "completed"


# ---- D 组实证驱动的机制修正：grace 双条件 + 里程碑驱动轮 ----------------


def _round_agent(monkeypatch, tmp_path, n_events, async_org=True):
    """构造一个带 task 的 agent，当前轮塞 n_events 个事件。"""
    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path)
    task = Task.create(goal="g")
    agent = Agent(llm=_StubLLM(), tools=[], task=task, async_organization=async_org)
    monkeypatch.setattr(agent, "_ensure_worker", lambda: None)  # 队列不入消费
    agent._org_watermark = 10
    agent.last_context_estimate = 100
    agent._open_or_reuse_round("干活")
    for _ in range(n_events):
        agent._record_event("tool_call", {"role": "assistant", "content": ""})
    return agent


def test_grace_exempts_light_round_but_not_mega_round(monkeypatch, tmp_path):
    """宽限期双条件（D 组实证：229 步巨型轮全程豁免、水位全场未出力）：
    前 grace 轮内的轻轮照旧豁免；巨型轮（事件数超限）达水位照常整理。"""
    light = _round_agent(monkeypatch, tmp_path, n_events=5)
    light.close_round()
    assert light._org_queue.qsize() == 0  # 轻轮：宽限期豁免照旧生效

    mega = _round_agent(monkeypatch, tmp_path, n_events=130)
    mega.close_round()
    assert mega._org_queue.qsize() == 1  # 巨型轮：不豁免，照常入队整理


def test_verify_milestone_closes_round_and_opens_checkpoint(monkeypatch, tmp_path):
    """里程碑驱动轮：verify_milestone = 检查点 = 轮边界——闭合当前轮、
    开新轮续写同一回合（新轮 user_input 带运行时说明）。"""
    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path)
    task = Task.create(goal="g")
    agent = Agent(llm=_StubLLM(), tools=[], task=task)
    agent._open_or_reuse_round("干活")
    agent.todo(action="start_milestone", goal="大步一", acceptance=["可跑"])
    agent.todo(action="verify_milestone", evidence="测试全绿")

    assert len(agent.rounds) == 2
    assert agent.rounds[0]["end_state"] == "completed"
    assert agent.current_round is agent.rounds[1]
    assert agent.rounds[1]["end_state"] == "open"
    assert "大步验收通过" in agent.rounds[1]["user_input"]["original"]
    assert agent.task.todo["milestone"] is None
    # 验收证据进了 TaskState 账本
    assert any("大步一" in c for c in task.get_state().completed)


def test_close_round_computes_blocks(monkeypatch, tmp_path):
    """轮闭合时零 LLM 计算 Block 结构并随轮次落盘（机制一挂钩）。"""
    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path)
    responses = [
        [_chunk(_delta(tool_calls=[_fragment(0, id="c1", name="edit_file",
                                             arguments=json.dumps({"path": "app.py",
                                                                   "old_text": "a",
                                                                   "new_text": "b"}))]))],
        [_chunk(_delta(content="修好了"))],
    ]
    task = Task.create(goal="目标")
    agent = Agent(llm=_StubLLM(responses), tools=[], task=task)  # 默认水位，不触发整理

    agent.run("修一下")

    bs = task.rounds[-1]["blocks"]
    assert len(bs) == 1
    assert bs[0]["wrote_files"] == ["app.py"]
    assert bs[0]["kind"] == "work"
    assert "_idx" not in bs[0]
    assert len(agent.llm.calls) == 2  # 干活本身 2 次（工具轮+回答轮）；分块零 LLM 不增加


# ---- 任务绑定 ------------------------------------------------------------


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


# ---- V3 水位批量整理 --------------------------------------------------------


def _batch_org_json(rounds: list[int], goal: str = "批量目标") -> str:
    """构造一次批量整理调用的合法输出。"""
    return json.dumps({
        "rounds": [
            {"seq": seq, "normalized_user_input": f"R{seq} 意图", "refined_index": []}
            for seq in rounds
        ],
        "state_patch": {"goal": goal, "completed": ["一次搞定"]},
    }, ensure_ascii=False)


def test_watermark_defers_organization_below_threshold(monkeypatch, tmp_path):
    """当前上下文窗口体量未达水位 → 不发起任何整理调用（小会话成本归零）。"""
    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path)
    monkeypatch.setattr("wovra.tokens.estimate", lambda text: len(text or "") // 4)
    task = Task.create(goal="x")
    task.rounds = [_round(1, "甲" * 2400, "甲" * 2400)]
    agent = Agent(
        llm=_StubLLM(), tools=[], task=task,
        org_watermark=5000, org_batch_max=12,
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
        tools=[], task=task, org_watermark=2000, org_batch_max=12, org_grace_rounds=0, org_cooldown_rounds=0,
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


def test_watermark_respects_batch_cap(monkeypatch, tmp_path):
    """超过批量上限：每次触发只收编最老的一批，剩余留给下次闭合。"""
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
        llm=_StubLLM([
            [_chunk(_delta(content=_batch_org_json([1, 2])))],
            [_chunk(_delta(content=_batch_org_json([3])))],
        ]),
        tools=[], task=task, org_watermark=2000, org_batch_max=2, org_grace_rounds=0, org_cooldown_rounds=0,
    )
    agent.last_context_estimate = 5000

    agent._maybe_organize_batch()

    assert task.rounds[0]["org_state"] == "done"
    assert task.rounds[1]["org_state"] == "done"
    assert task.rounds[2]["org_state"] == ""  # 第三轮未收编
    prompt = agent.llm.calls[0]["messages"][0]["content"]
    assert "Round 3" not in prompt

    # 收尾补整理（run 模式退出路径）：不问水位，把剩余轮消化掉
    agent.organize_backlog()
    assert task.rounds[2]["org_state"] == "done"
    assert len(agent.llm.calls) == 2


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
        tools=[], task=task, org_watermark=1200, org_batch_max=12,
        org_grace_rounds=0, org_cooldown_rounds=0,
    )
    agent.last_context_estimate = 1200  # 达到水位 → 触发

    agent._maybe_organize_batch()

    assert len(agent.llm.calls) == 1
    assert task.rounds[0]["org_state"] == "done"


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


# ---- V1 Context Runtime：加载视图 / 展开 / baseline 对照 ---------------------


def _round(seq: int, user: str, answer: str) -> dict:
    """构造一个已整理的 Round（V2 结构：refined_index + 事件双份信息）。

    truncated 模拟 Runtime 规则：只保留前面 ~120 字符。
    """
    return {
        "seq": seq,
        "user_input": {"original": user, "normalized": f"澄清：{user}"},
        "events": [
            {"id": f"R{seq}-E01", "type": "user", "status": "", "truncated": user[:120],
             "message": {"role": "user", "content": user}},
            {"id": f"R{seq}-E02", "type": "final_answer", "status": "", "truncated": answer[:120],
             "message": {"role": "assistant", "content": answer}},
        ],
        "refined_index": {
            f"R{seq}-E01": f"{user}（精修）",
            f"R{seq}-E02": f"{answer[:20]}（精修）",
        },
        "end_state": "completed",
        "org_state": "done",
    }


def _make_open_round(agent: Agent, seq: int, user: str):
    """在 agent 上挂一个开放 Round（模拟中断后未闭合的场景）。"""
    agent.current_round = {
        "seq": seq, "user_input": {"original": user, "normalized": ""},
        "events": [], "refined_index": {}, "end_state": "open", "org_state": "",
    }
    agent.rounds.append(agent.current_round)
    agent.messages = []
    agent._record_event("user", {"role": "user", "content": user})


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


def test_organization_retries_after_invalid_json(monkeypatch, tmp_path):
    """整理输出非法 JSON 时重试一次，重试成功则正常应用。"""
    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path)
    org_json = json.dumps({
        "rounds": [{
            "seq": 1,
            "normalized_user_input": "澄清的意图",
            "refined_index": [{"id": "R1-E02", "line": "给出结论"}],
        }],
        "state_patch": {"completed": ["完成项"]},
    }, ensure_ascii=False)
    responses = [
        [_chunk(_delta(content="干完了"))],
        [_chunk(_delta(content="我觉得应该这样：blahblah"))],  # 第一次：夹带说明文字
        [_chunk(_delta(content=org_json))],                     # 重试：合法 JSON
    ]
    task = Task.create(goal="目标")
    agent = Agent(llm=_StubLLM(responses), tools=[], task=task, org_watermark=0, org_grace_rounds=0, org_cooldown_rounds=0)

    agent.run("问")

    agent._promote_org_results()  # 模拟下一轮开启：暂存产物生效
    assert task.task_state.get("completed") == ["完成项"]
    assert task.rounds[-1]["refined_index"]["R1-E02"] == "给出结论"


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
    # 缓存契约：整理调用的 tools 与工作调用完全一致（序列化恒定）
    assert agent.llm.calls[0]["tools"] == agent.llm.calls[1]["tools"]
    assert any(
        t["function"]["name"] == "submit_organization"
        for t in agent.llm.calls[1]["tools"]
    )
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

    # 两路共用同一 tools 数组（缓存契约：序列化恒定）
    org_calls = [c for c in agent.llm.calls if c.get("lane") == "org"]
    split_calls = [c for c in agent.llm.calls if c.get("lane") == "split"]
    assert org_calls and split_calls
    assert org_calls[0]["tools"] == split_calls[0]["tools"]
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


def test_submit_organization_guard_is_noop_in_work_dialog():
    """工作对话误调用提交工具：只返回说明文本，无副作用。"""
    task = Task.create(goal="x")
    agent = Agent(llm=_StubLLM(), tools=[], task=task)

    out = agent.submit_organization(rounds=[{"seq": 1}], state_patch={"is_done": True})
    out2 = agent.submit_domains(domains=[], split_assessment={"splittable": True})

    assert "忽略" in out and "忽略" in out2
    assert task.task_state == {}
    assert all("pending_org" not in r for r in task.rounds)


def test_todo_milestone_lifecycle(monkeypatch, tmp_path):
    """大步/小步账本：深度恒 1、证据闸门、非阻塞人工验收不搁置。"""
    task = Task.create(goal="演示页")
    agent = Agent(llm=_StubLLM(), tools=[], task=task)

    # 深度恒 1：无大步时 verify/drop 拒绝；开大步必须带验收标准
    assert "无开启中的大步" in agent.todo(action="verify_milestone", evidence="x")
    assert "acceptance" in agent.todo(action="start_milestone", goal="多会话版")

    out = agent.todo(
        action="start_milestone", goal="多会话版",
        acceptance=["刷新后会话保留", "删除当前会话回落"],
    )
    assert "多会话版" in out
    # 已有开启中的大步 → 拒绝再开（深度恒 1）
    assert "深度恒 1" in agent.todo(action="start_milestone", goal="另一个")

    assert "OK" in agent.todo(action="add_step", text="会话数据结构")
    assert "OK" in agent.todo(action="check_step", text="会话数据结构")
    assert "OK" in agent.todo(action="add_step", text="切换/删除交互")

    # 非阻塞人工验收：挂起继续干，verify 时一次性呈交并转 experiments
    out = agent.todo(action="defer_check", text="浅色主题配色是否刺眼（主观，最后统一验收）")
    assert "挂起" in out
    out = agent.todo(
        action="verify_milestone",
        evidence="node tests/run.js 全绿（5 套件）",
    )
    assert "大步已验收" in out and "待办实验" in out
    assert task.get_state().completed[-1].startswith("[大步] 多会话版")
    assert any("浅色主题配色" in e for e in task.get_state().experiments)
    # 关大步即清：小步与挂起项清空，可以开下一大步
    out = agent.todo(action="start_milestone", goal="搜索功能",
                     acceptance=["关键词命中高亮"])
    assert "OK" in out
    assert task.todo["milestone"]["goal"] == "搜索功能"
    assert task.todo["steps"] == []

    # 证据闸门：无证据 verify 拒绝
    assert "evidence" in agent.todo(action="verify_milestone")


def test_todo_tail_lines_shown_in_reminder(monkeypatch):
    """当前大步/小步进进 runtime-reminder 尾部——跨轮续跑的工作记忆。"""
    task = Task.create(goal="分层回归")
    agent = Agent(llm=_StubLLM(), tools=[], task=task)
    agent.todo(action="start_milestone", goal="ICP 配准", acceptance=["误差 < 1px"])
    agent.todo(action="add_step", text="读取标定参数")
    _make_open_round(agent, 1, "继续")

    msgs = agent._assemble_messages()
    body = "\n".join(m.get("content", "") for m in msgs)

    assert "[当前大步] ICP 配准（小步 0/1）" in body


def test_registry_default_and_comm_guards():
    """注册表默认主 agent；自咨询拒绝；未知 agent 列出现存条目。"""
    task = Task.create(goal="x")
    assert task.registry[0]["id"] == "A"
    agent = Agent(llm=_StubLLM(), tools=[], task=task)
    assert "不要 consult 主 agent" in agent.consult(agent="A", question="?")
    assert "未找到 agent：Z" in agent.notify(agent="Z", message="m")
    assert "A(主agent)" in agent.notify(agent="Z", message="m")


def test_notify_and_consult_direct_to_user():
    """单向 notify 落收件箱；双向 consult 以目标视角回答、回复打标签
    直达用户窗口并返回调用方；收件箱随激活送达。"""
    collected = []
    task = Task.create(goal="演示项目")
    task.registry.append({
        "id": "B", "name": "前端agent",
        "description": "负责 index.html 与 css/ 界面层",
        "file_domains": ["index.html", "css/"],
        "status": "dormant", "inbox": [],
    })
    consult_resp = [_chunk(_delta(content="界面层归我，按钮色值用 #0a84ff"))]
    agent = Agent(llm=_StubLLM(consult_responses=[consult_resp]), tools=[], task=task)
    agent._stream_cbs = {"thinking": None, "answer": collected.append}

    out = agent.notify(agent="B", message="接口定了：getStats() 返回 {clicks}")
    assert "已单向送达" in out
    assert task.registry[1]["inbox"][0]["message"].startswith("接口定了")

    reply = agent.consult(agent="前端agent", question="UI 改动走谁的文件域？")
    assert "前端agent 的回复" in reply and "#0a84ff" in reply
    assert task.registry[1]["inbox"] == []              # 收件箱随激活送达
    assert collected[0] == "\n[前端agent] "             # 打标签直达用户窗口
    assert any("#0a84ff" in c for c in collected)


def test_thinking_head_single_line():
    """思考单行化：折叠空白取尾部，单行展示。"""
    from wovra import ui

    long = "思路" * 200
    head = ui.thinking_head(long)
    assert head.startswith("…") and len(head) <= 102
    assert ui.thinking_line("x").startswith("💭")


def test_task_state_wrapped_in_runtime_reminder_envelope(monkeypatch):
    """运行时通道（1.1）：任务状态/文件地图以 <runtime-reminder> 信封注入，
    与用户发言语义分离——模型分得清"机制给的"和"用户要的"。"""
    task = Task.create(goal="分层回归")
    task.rounds = [_round(1, "第一轮", "答案一")]
    task.task_state["goal"] = "存档测试目标"
    agent = Agent(llm=_StubLLM(), tools=[], task=task)
    _make_open_round(agent, 2, "继续")

    msgs = agent._assemble_messages()
    bodies = [m.get("content", "") for m in msgs]

    envelope = [b for b in bodies if b.startswith("<runtime-reminder>")]
    assert envelope, "任务状态必须走运行时信封"
    assert "存档测试目标" in envelope[0]
    assert envelope[0].rstrip().endswith("</runtime-reminder>")


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


def test_resume_continues_open_round_without_new_user_message(monkeypatch, tmp_path):
    """\\c = resume()：不注入新用户消息，直接续上开放轮干到闭合。"""
    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path)
    responses = [
        [_chunk(_delta(content="干完了"))],
    ]
    task = Task.create(goal="目标")
    agent = Agent(llm=_StubLLM(responses), tools=[], task=task)

    # 第一轮被打断（如步数超限/Ctrl+C）：轮保持开放
    agent._open_or_reuse_round("开始干活")
    agent._record_event("user", {"role": "user", "content": "开始干活"})
    agent.finalize_round("open")

    answer = agent.resume()

    assert answer == "干完了"
    assert task.rounds[-1]["end_state"] == "completed"
    # 关键断言：历史里只有最初那一条用户消息，resume 没有制造"继续"噪音
    user_events = [e for e in task.rounds[-1]["events"] if e["type"] == "user"]
    assert len(user_events) == 1


def test_resume_without_open_round_raises(monkeypatch, tmp_path):
    """没有开放轮时 resume 报错，且不产生任何 LLM 调用。"""
    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path)
    task = Task.create(goal="目标")
    agent = Agent(llm=_StubLLM(), tools=[], task=task)

    try:
        agent.resume()
        raised = False
    except RuntimeError as error:
        raised = True
        assert "没有可继续的开放轮次" in str(error)
    assert raised
    assert agent.llm.calls == []


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


def test_default_max_turns_is_200():
    """默认步数上限 200（安全网而非配额；60 时代用户实测两次撞顶）。"""
    task = Task.create(goal="x")
    agent = Agent(llm=_StubLLM(), tools=[], task=task)
    assert agent.max_turns == 200


def test_open_round_merges_interrupted_runs(monkeypatch, tmp_path):
    """轮闭合规则：中断/异常不闭合 Round，多条用户输入并入同一开放轮。"""
    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path)
    org_json = json.dumps({
        "rounds": [{
            "seq": 1,
            "normalized_user_input": "用户想把 ICP 调试完（合并了两条输入的意图）",
            "refined_index": [],
        }],
        "state_patch": {},
    }, ensure_ascii=False)
    responses = [
        [_chunk(_delta(content="继续干"))],
        [_chunk(_delta(content=org_json))],
    ]
    task = Task.create(goal="ICP 调试")
    agent = Agent(llm=_StubLLM(responses), tools=[], task=task, org_watermark=0, org_grace_rounds=0, org_cooldown_rounds=0)

    # 第一次 run 模拟被中断：不闭合（仅持久化，轮保持开放）
    agent._open_or_reuse_round("开始调试 ICP")
    agent._record_event("user", {"role": "user", "content": "开始调试 ICP"})
    agent.finalize_round("open")
    # 第二次 run 续上同一个开放轮直到最终回答
    agent.run("继续，把 ICP 调试完")

    assert len(task.rounds) == 1  # 两条输入属于同一个 Round
    user_events = [e for e in task.rounds[0]["events"] if e["type"] == "user"]
    assert len(user_events) == 2  # 两条原始输入都保留为事件
    # 整理产物：合并澄清后的意图覆盖整个开放轮（暂存生效后可见）
    agent._promote_org_results()
    assert task.rounds[0]["user_input"]["normalized"] == "用户想把 ICP 调试完（合并了两条输入的意图）"
    assert task.rounds[0]["end_state"] == "completed"


def test_agent_records_events_into_task(monkeypatch, tmp_path):
    from wovra import task as task_module
    from wovra.task import Task

    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path)

    def echo(text: str):
        """原样返回。"""
        return text

    task = Task.create(goal="记录测试")
    agent = Agent(llm=_StubLLM(), tools=[echo], task=task)
    agent._execute("call_1", "echo", '{"text": "hi"}')

    kinds = [e["kind"] for e in task.history]
    assert "tool_call" in kinds and "tool_result" in kinds
    assert (tmp_path / task.id / "task.json").exists()  # 每次执行后都落盘


# ---- 路径安全 -------------------------------------------------------------


def test_read_file_blocks_escape_from_project_root():
    with pytest.raises(ValueError, match="路径越界"):
        read_file("../../etc/passwd")


def test_step_count_excludes_organization_calls(monkeypatch, tmp_path):
    """步数只统计干活的步；整理成本走维护账本，随 usage 行落盘不漏记。"""
    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path)
    org_json = json.dumps({
        "rounds": [{"seq": 1, "normalized_user_input": "意图", "refined_index": []}],
        "state_patch": {"completed": ["完成"]},
    }, ensure_ascii=False)
    responses = [
        [_chunk(_delta(content="干完了"))],
        # 整理调用（水位触发时同步执行），带 usage 验证分账
        [_chunk(_delta(content=org_json)), _chunk(usage=_usage(60, 30, 90))],
    ]
    task = Task.create(goal="目标")
    agent = Agent(llm=_StubLLM(responses), tools=[], task=task, org_watermark=0, org_grace_rounds=0, org_cooldown_rounds=0)

    agent.run("问")

    assert agent.last_stats["llm_calls"] == 1  # 只有干活的 1 步
    assert agent.last_stats["total_tokens"] == 0  # 整理成本不混进干活的账
    assert agent.last_maint["organization"]["total"] == 90  # 记账时取走的快照（展示层用）
    recorded = [e for e in task.history if e["kind"] == "usage"][-1]["detail"]
    assert "org=90" in recorded  # usage 记账发生在 drain 之后，成本落盘
    assert agent.drain_maintenance_usage()["organization"]["total"] == 0  # 账已清零不重复记


def test_open_round_records_usage_on_finalize(monkeypatch, tmp_path):
    """超限/中断的开放轮也要记账——失败尝试的成本不能在账本上隐身。"""
    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path)

    def noop():
        """什么也不做。"""

    responses = [
        [
            _chunk(_delta(tool_calls=[_fragment(0, id="c1", name="noop", arguments="{}")])),
            _chunk(usage=_usage(10, 5, 15)),
        ]
    ]
    task = Task.create(goal="目标")
    agent = Agent(llm=_StubLLM(responses), tools=[noop], task=task, max_turns=1)

    with pytest.raises(RuntimeError):
        agent.run("问")
    agent.finalize_round("open")

    usage_events = [e for e in task.history if e["kind"] == "usage"]
    assert usage_events, "开放轮也要有 usage 记账"
    detail = usage_events[-1]["detail"]
    assert "total=15" in detail
    assert "轮未闭合" in detail


def test_usage_record_includes_cache_fields(monkeypatch, tmp_path):
    """usage 落盘要带缓存命中/未命中/等效输入——缓存是成本差异的核心变量。"""
    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path)
    responses = [[_chunk(_delta(content="ok")), _chunk(usage=_usage(10, 2, 12, cached=4))]]
    task = Task.create(goal="g")
    # baseline：不触发整理调用，账目只来自这一次干活调用
    agent = Agent(llm=_StubLLM(responses), tools=[], task=task, context_mode="baseline")

    agent.run("问")

    detail = [e for e in task.history if e["kind"] == "usage"][-1]["detail"]
    assert "缓存命中 4 tok（40.0%）" in detail
    assert "等效输入 6 tok" in detail  # 未命中 6 + 命中 4/30 ≈ 6


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


def test_tool_args_with_unpaired_surrogates_are_sanitized(monkeypatch, tmp_path):
    """回归（实测 2026-09-05）：模型把 emoji 拆成不成对 \\uD83D 转义时，
    参数清洗保证工具执行与落盘都不崩。"""
    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path)
    seen = {}

    def edit_file(**kwargs):
        seen.update(kwargs)
        return "已修改"

    responses = [[_chunk(_delta(content="ok"))]]
    task = Task.create(goal="g")
    agent = Agent(llm=_StubLLM(responses), tools=[edit_file], task=task)
    raw = json.dumps({"path": "a.md", "old_text": "x", "new_text": "y"})
    raw = raw[:-2] + '\ud83d"}'  # 注入不成对代理转义（json.loads 合法、UTF-8 编码非法）

    agent._execute("c1", "edit_file", raw)

    assert "\ud83d" not in str(seen)
    assert "\ufffd" in seen["new_text"]
    task.save()  # 含该工具调用事件的会话照常落盘


def test_usage_record_includes_context_estimate(monkeypatch, tmp_path):
    """usage 落盘带 context=：上下文增长曲线可从 task.json 直接查得。"""
    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path)
    responses = [[_chunk(_delta(content="ok")), _chunk(usage=_usage(10, 2, 12))]]
    task = Task.create(goal="g")
    agent = Agent(llm=_StubLLM(responses), tools=[], task=task, context_mode="baseline")

    agent.run("问")

    assert agent.last_context_estimate > 0
    detail = [e for e in task.history if e["kind"] == "usage"][-1]["detail"]
    assert "context=" in detail


def test_readonly_batch_runs_in_order(monkeypatch, tmp_path):
    """纯只读批次并发执行，结果仍按调用顺序记录（顺序是正确性契约）。"""
    from wovra import tools as tools_module

    monkeypatch.setattr(tools_module, "PROJECT_ROOT", tmp_path)
    for name, text in (("a.txt", "内容A"), ("b.txt", "内容B"), ("c.txt", "内容C")):
        (tmp_path / name).write_text(text, encoding="utf-8")
    calls = [
        _fragment(i, id=f"c{i}", name="read_file",
                  arguments=json.dumps({"path": f"{name}.txt"}))
        for i, name in enumerate("abc")
    ]
    responses = [
        [_chunk(_delta(tool_calls=calls)), _chunk(usage=_usage(10, 5, 15))],
        [_chunk(_delta(content="ok"))],
    ]
    from wovra.tools import read_file

    # baseline：不触发整理调用，stub 响应序列刚好够用
    task = Task.create(goal="g")
    agent = Agent(llm=_StubLLM(responses), tools=[read_file], task=task,
                  context_mode="baseline")

    agent.run("读三个文件")

    results = [e["detail"] for e in task.history if e["kind"] == "tool_result"]
    assert len(results) == 3
    assert "内容A" in results[0] and "内容B" in results[1] and "内容C" in results[2]


def test_schema_unwraps_optional_annotation():
    """Optional[int] 参数在 schema 里应为 integer，而不是退化为 string。"""

    def demo(timeout: int | None = None):
        """带可选整数的工具。"""

    schema = _schema_of(demo)["function"]["parameters"]["properties"]
    assert schema["timeout"] == {"type": "integer"}


def test_turn_count_is_session_bound_across_restarts(monkeypatch, tmp_path):
    """轮次与会话绑定：退出重开（Agent 重建）后接着上一轮计数，不归零。"""
    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path)
    task = Task.create(goal="g")
    task.save()

    # 第一次进程：R1 完成后退出
    agent1 = Agent(llm=_StubLLM([[_chunk(_delta(content="答1"))]]),
                   tools=[], task=task, context_mode="baseline")
    agent1.run("问1")
    assert agent1.last_stats["turn"] == 1

    # 第二次进程（模拟退出重开）：从磁盘恢复会话
    agent2 = Agent(llm=_StubLLM([[_chunk(_delta(content="答2"))]]),
                   tools=[], task=Task.load(task.id), context_mode="baseline")
    agent2.run("问2")
    assert agent2.last_stats["turn"] == 2  # 修复前：进程内计数归零显示 1


def test_ttft_recorded_in_stats_and_usage_line(monkeypatch, tmp_path):
    """时间仪器：每步首字延迟进 stats（合计 + 峰值），终端账单行可见。"""
    from wovra import ui

    responses = [[_chunk(_delta(content="hi")), _chunk(usage=_usage(10, 2, 12, cached=4))]]
    agent = _agent_with([], responses)
    agent.run("问")

    assert agent.last_stats["ttft_seconds"] > 0
    assert agent.last_stats["ttft_max"] > 0
    line = ui.usage_line(agent.last_stats)
    assert "首字" in line and "合计" in line
