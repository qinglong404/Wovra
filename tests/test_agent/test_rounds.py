"""Agent 运行时测试（自 test_agent.py 拆分，2026-09-11）。

本模块：test_rounds。"""

import json
import pytest
from wovra import task as task_module
from wovra.agent import Agent
from wovra.task import Task

# 共享夹具/工具（_helpers.py 是原文件的公共头部）
from ._helpers import *  # noqa: F401,F403

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


def test_step_count_continues_across_resume(monkeypatch, tmp_path):
    """同一轮被打断后 \c 续跑：步数按轮累计续上（预算属于轮不属于段），
    超限按累计数报。"""
    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path)
    task = Task.create(goal="目标")
    agent = Agent(llm=_StubLLM([
        [_chunk(_delta(content="R1"))],
        [_chunk(_delta(content="续跑完成"))],
    ]), tools=[], task=task, max_turns=5)

    agent.run("开始")                          # R1：1 步，闭合
    assert task.rounds[0]["steps_used"] == 1

    agent._open_or_reuse_round("继续干")        # 新开放轮，模拟已用 4 步被打断
    agent.current_round["steps_used"] = 4
    agent.resume()                             # 续跑只剩 1 步预算

    assert task.rounds[-1]["end_state"] == "completed"
    assert task.rounds[-1]["steps_used"] == 5  # 4+1 续上，没有重置

    # 已达上限再续：按累计数报超限
    agent._open_or_reuse_round("再续")
    agent.current_round["steps_used"] = 5
    with pytest.raises(RuntimeError) as excinfo:
        agent.resume()
    assert "累计工作 5 步" in str(excinfo.value)


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
    # 信封绝对尾部（D 组实证）：状态高频变化只作废信封本身（~1-2K），
    # 不作废其前的事件历史——位置保证是缓存纪律的一部分
    assert msgs[-1]["content"].startswith("<runtime-reminder>")
