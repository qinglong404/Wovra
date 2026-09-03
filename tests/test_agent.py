"""Agent 纯逻辑部分的测试：schema 生成、错误回传、路径安全。

不发起任何真实模型调用——Agent 只有 run() 会碰 llm.chat，
构造时传一个什么都不做的 stub 即可。
"""

import json
from types import SimpleNamespace

import pytest

from wovra import task as task_module
from wovra.agent import Agent, _schema_of, read_file
from wovra.task import Task


class _StubLLM:
    """替身 LLM：构造时收到"每次 chat 调用应返回的分块序列"列表。"""

    model = "stub"

    def __init__(self, responses: list | None = None):
        self.responses = list(responses or [])
        self.calls: list[dict] = []

    def chat(self, messages, tools=None, stream=False, **kwargs):
        self.calls.append({"messages": messages, "stream": stream})
        return iter(self.responses.pop(0))


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


def _chunk(delta=None, usage=None):
    choices = [] if delta is None else [SimpleNamespace(delta=delta)]
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


# ---- 任务绑定 ------------------------------------------------------------


def test_organization_updates_state_and_refined_index(monkeypatch, tmp_path):
    """Round 闭合后 Organization 更新状态补丁、Normalized 意图与精修索引。"""
    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path)
    org_json = json.dumps({
        "normalized_user_input": "用户想搞清楚项目的测试覆盖情况",
        "refined_index": [
            {"id": "R1-E01", "line": "询问测试覆盖"},
            {"id": "R1-E02", "line": "给出覆盖结论"},
        ],
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
    agent = Agent(llm=_StubLLM(responses), tools=[], task=task)

    answer = agent.run("把活干完")

    assert answer == "干完了"
    # State Patch 增量合并进任务状态
    assert task.task_state["goal"] == "搞清测试覆盖"
    assert task.task_state["is_done"] is True
    assert task.task_state["completed"] == ["梳理测试覆盖"]
    # Round 结构持久化：Normalized 意图与精修索引写回
    assert task.rounds[-1]["user_input"]["normalized"] == "用户想搞清楚项目的测试覆盖情况"
    assert task.rounds[-1]["refined_index"]["R1-E02"] == "给出覆盖结论"
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
    agent = Agent(llm=_StubLLM(responses), tools=[], task=task)

    agent.run("问")

    assert task.task_state == {}  # 原状态未被破坏
    assert task.rounds[-1]["refined_index"] == {}
    assert task.rounds[-1]["user_input"]["normalized"] == ""


def test_organization_patch_ignores_invalid_fields(monkeypatch, tmp_path):
    """state_patch 里的非法字段被忽略，合法字段照常合并。"""
    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path)
    org_json = json.dumps({
        "normalized_user_input": "x",
        "round_summary": "s",
        "state_patch": {"completed": "不是列表", "current_status": "进行中"},
    }, ensure_ascii=False)
    responses = [
        [_chunk(_delta(content="回答"))],
        [_chunk(_delta(content=org_json))],
    ]
    task = Task.create(goal="目标")
    agent = Agent(llm=_StubLLM(responses), tools=[], task=task)

    agent.run("问")

    assert task.task_state.get("completed", []) == []  # 非法列表被忽略
    assert task.task_state.get("current_status") == "进行中"


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
    """加载视图：近 1 轮全量；更早轮次 = 用户原文 + 意图 + 精修索引。

    长回答的尾部细节不进上下文，头部与精修索引进入。
    """
    long_answer = "很长的回答开头。" + "细节" * 100 + "很长的回答结尾。"
    task = Task.create(goal="x")
    task.rounds = [
        _round(1, "第一轮原始提问", long_answer),
        _round(2, "第二轮 UI 修改", "按钮改好了"),
        _round(3, "第三轮闲聊", "哈哈"),
        _round(4, "第四轮 ICP 调试", "误差降低了"),
    ]
    agent = Agent(llm=_StubLLM(), tools=[], task=task, max_recent_rounds=1)
    _make_open_round(agent, 5, "继续")

    msgs = agent._assemble_messages()
    bodies = [m.get("content", "") for m in msgs]

    assert any("误差降低了" in b for b in bodies)            # 近 1 轮全量
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
    """对照组：累计输入达 80% × 窗口时压缩旧轮次（市面惯例的常规处理）。"""
    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path)
    task = Task.create(goal="x")
    task.rounds = [_round(1, "第一轮", "第一轮回答"), _round(2, "第二轮", "第二轮回答")]
    summary_text = "前两轮的压缩摘要：完成了若干工作。"
    responses = [
        [_chunk(_delta(content="r3")), _chunk(usage=_usage(500, 10, 510))],
        [_chunk(_delta(content="r4")), _chunk(usage=_usage(500, 10, 510))],
        [_chunk(_delta(content=summary_text))],  # 阈值触发后的压缩调用
    ]
    agent = Agent(
        llm=_StubLLM(responses), tools=[], task=task,
        context_mode="baseline", context_limit=1000,  # 80% = 800
    )

    agent.run("第三轮")
    agent.run("第四轮")  # 累计输入 1000 ≥ 800 → 触发压缩

    assert task.baseline_summary == summary_text
    assert task.rounds[0].get("compacted") is True
    assert task.rounds[1].get("compacted") is True
    assert not task.rounds[2].get("compacted", False)
    assert not task.rounds[3].get("compacted", False)

    # 装配：压缩摘要进入上下文，被压缩轮次的原文退出
    agent.current_round = None
    msgs = agent._assemble_messages()
    bodies = "\n".join(m.get("content", "") for m in msgs)
    assert "前两轮的压缩摘要" in bodies
    assert "第一轮回答" not in bodies


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
        "normalized_user_input": "澄清的意图",
        "refined_index": [{"id": "R1-E02", "line": "给出结论"}],
        "state_patch": {"completed": ["完成项"]},
    }, ensure_ascii=False)
    responses = [
        [_chunk(_delta(content="干完了"))],
        [_chunk(_delta(content="我觉得应该这样：blahblah"))],  # 第一次：夹带说明文字
        [_chunk(_delta(content=org_json))],                     # 重试：合法 JSON
    ]
    task = Task.create(goal="目标")
    agent = Agent(llm=_StubLLM(responses), tools=[], task=task)

    agent.run("问")

    assert task.task_state.get("completed") == ["完成项"]
    assert task.rounds[-1]["refined_index"]["R1-E02"] == "给出结论"


def test_tier_degradation_under_budget(monkeypatch):
    """预算吃紧时按"最老的先降档"逐级降档：档1 → 档2 → 档3。

    回归：此前轮数超过 max_recent_rounds 时选择路径炸过一次
    （_relevance 误挂到 self 上），本测试覆盖多轮装配路径。
    """
    task = Task.create(goal="分层回归")
    task.rounds = [
        _round(1, "处理 ICP 配准误差问题", "ICP 误差分析完成"),
        _round(2, "修改界面按钮颜色", "按钮改好了"),
        _round(3, "调整界面布局间距", "布局调整完毕"),
        _round(4, "继续处理 ICP 配准", "ICP 参数已更新"),
    ]
    agent = Agent(
        llm=_StubLLM(), tools=[], task=task,
        max_recent_rounds=1, history_budget=200,  # 极小预算，强制逐级降档
    )
    _make_open_round(agent, 5, "ICP 误差为什么还是这么大")

    msgs = agent._assemble_messages()
    bodies = "\n".join(m.get("content", "") for m in msgs)

    # 近 1 轮（R4）全量；R3 预算内保留档 1；R1/R2 被降到档 3 一行索引
    assert "ICP 参数已更新" in bodies
    assert "布局调整完毕" in bodies or "调整界面布局间距" in bodies
    assert "[R1]" in bodies and "[R2]" in bodies  # 档 3 的一行话题行
    assert "ICP 误差分析完成" not in bodies  # 档 3 不再携带事件索引


def test_open_round_merges_interrupted_runs(monkeypatch, tmp_path):
    """轮闭合规则：中断/异常不闭合 Round，多条用户输入并入同一开放轮。"""
    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path)
    org_json = json.dumps({
        "normalized_user_input": "用户想把 ICP 调试完（合并了两条输入的意图）",
        "refined_index": [],
        "state_patch": {},
    }, ensure_ascii=False)
    responses = [
        [_chunk(_delta(content="继续干"))],
        [_chunk(_delta(content=org_json))],
    ]
    task = Task.create(goal="ICP 调试")
    agent = Agent(llm=_StubLLM(responses), tools=[], task=task)

    # 第一次 run 模拟被中断：不闭合（仅持久化，轮保持开放）
    agent._open_or_reuse_round("开始调试 ICP")
    agent._record_event("user", {"role": "user", "content": "开始调试 ICP"})
    agent.finalize_round("open")
    # 第二次 run 续上同一个开放轮直到最终回答
    agent.run("继续，把 ICP 调试完")

    assert len(task.rounds) == 1  # 两条输入属于同一个 Round
    user_events = [e for e in task.rounds[0]["events"] if e["type"] == "user"]
    assert len(user_events) == 2  # 两条原始输入都保留为事件
    # 整理产物：合并澄清后的意图覆盖整个开放轮
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
