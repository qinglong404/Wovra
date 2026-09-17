"""每轮一段话（V4 §3.2 第一步）：轮闭合处的**同步**结算。

覆盖：产物落地与执行者盖章、账本增量（去重 + 忽略标量）、失败只留痕不重试、
硬门只看结构（seq/空句）、软档不拦、尾部追加形态与 tools 数组恒定、开关、守卫。
（默认档由 conftest 关掉——它会在每次轮闭合多打一次 LLM，本文件显式打开。）
"""

import json

from wovra import task as task_module
from wovra.agent import Agent
from wovra.task import Task
from wovra.truncate import make_event

from ._helpers import _StubLLM, _chunk, _delta, _fragment


def _note_chunk(note: dict):
    return _chunk(_delta(tool_calls=[
        _fragment(0, id="n1", name="submit_round_note",
                  arguments=json.dumps(note, ensure_ascii=False)),
    ]))


def _agent(monkeypatch, tmp_path, note=None, enabled=True):
    """响应池：第一跳工作回答，第二跳结算（note=None → 只回正文、不提交）。"""
    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path)
    monkeypatch.setenv("WOVRA_ROUND_NOTE", "1" if enabled else "0")
    second = [_note_chunk(note)] if note is not None else [_chunk(_delta(content="（没提交）"))]
    task = Task.create(goal="g")
    agent = Agent(llm=_StubLLM([[_chunk(_delta(content="答一句"))], second]),
                  tools=[], task=task)
    return agent, task


def _note_records(task: Task) -> list[str]:
    return [str(h.get("detail")) for h in task.history if h.get("kind") == "note"]


def test_round_close_writes_note_with_code_stamped_executor(monkeypatch, tmp_path):
    """产物落 `round["note"]`；执行者由代码盖章（不取模型自报）。"""
    agent, task = _agent(monkeypatch, tmp_path, note={
        "seq": 1, "sentence": "查了 GAIA：466 题分三档", "failures": [],
        "executor": "我自己说我是谁",          # 模型自报的字段应被忽略
        "ledger_append": {"decisions": ["只评离线子集"]},
    })
    agent.run("问一句")

    round_ = task.rounds[-1]
    assert round_["note_state"] == "done"
    assert round_["note"]["sentence"] == "查了 GAIA：466 题分三档"
    assert round_["note"]["executor"] == "Main"
    assert "只评离线子集" in task.get_state().decisions


def test_note_ledger_append_dedupes_and_ignores_scalars(monkeypatch, tmp_path):
    """账本只增不减、追加去重；标量（现状/目标）不由模型写。"""
    agent, task = _agent(monkeypatch, tmp_path, note={
        "seq": 1, "sentence": "一句话", "failures": [],
        "ledger_append": {
            "current_status": "模型想改现状", "goal": "模型想改目标",
            "known_issues": ["已有问题", "新问题"],
        },
    })
    task.task_state = {"goal": "g", "current_status": "旧现状",
                       "known_issues": ["已有问题"]}
    agent.task = task
    agent.run("问一句")

    state = task.get_state()
    assert state.current_status == "旧现状"           # 标量被忽略
    assert state.goal == "g"
    assert state.known_issues == ["已有问题", "新问题"]  # 去重后追加


def test_note_without_product_fails_locally_without_retry(monkeypatch, tmp_path):
    """没有产出：判 failed、只留痕、**不重发**（调用数就是 1 次结算）。"""
    agent, task = _agent(monkeypatch, tmp_path, note=None)
    agent.run("问一句")

    round_ = task.rounds[-1]
    assert round_["note_state"] == "failed"
    assert "note" not in round_
    assert len(agent.llm.calls) == 2                  # 工作 1 + 结算 1，没重发
    assert any("没有" in line for line in _note_records(task))


def test_note_seq_mismatch_and_empty_sentence_fail(monkeypatch, tmp_path):
    """硬门只看结构：seq 对不上 / 句子为空 → failed（内容问题不在此列）。"""
    agent, task = _agent(monkeypatch, tmp_path, note={
        "seq": 99, "sentence": "写的是别的轮", "failures": [], "ledger_append": {},
    })
    agent.run("问一句")
    assert task.rounds[-1]["note_state"] == "failed"
    assert any("seq 对不上" in line for line in _note_records(task))

    agent2, task2 = _agent(monkeypatch, tmp_path, note={
        "seq": 1, "sentence": "   ", "failures": [], "ledger_append": {},
    })
    agent2.run("问一句")
    assert task2.rounds[-1]["note_state"] == "failed"
    assert any("句子为空" in line for line in _note_records(task2))


def test_note_with_soft_defects_still_lands(monkeypatch, tmp_path):
    """内容正确性问题一律软档：产物照样落地，只在留痕里记一笔。"""
    agent, task = _agent(monkeypatch, tmp_path, note={
        "seq": 1,
        "sentence": "改动落在 docs/prompt-review-2026-09-16.md，并用 list_background 收了尾",
        "failures": [{"text": "越界被拦", "evidence": "R9-E99"}],   # 本轮没这事件
        "ledger_append": {},
    })
    agent.run("问一句")

    assert task.rounds[-1]["note_state"] == "done"      # 一律落地
    assert any("软档" in line for line in _note_records(task))


def test_note_call_is_tail_append_with_full_tools(monkeypatch, tmp_path):
    """形态：装配快照 ＋ 尾部一条指令；tools 数组仍是完整那份（不因结算收窄）。"""
    agent, task = _agent(monkeypatch, tmp_path, note={
        "seq": 1, "sentence": "一句话", "failures": [], "ledger_append": {},
    })
    agent.run("问一句")

    call = agent.llm.calls[-1]
    assert call["messages"][-1]["role"] == "user"
    assert "[结算指令]" in call["messages"][-1]["content"]
    assert "[锚]" in call["messages"][-1]["content"]
    assert len(call["tools"]) == len(agent._schemas)
    assert any(t["function"]["name"] == "submit_round_note" for t in call["tools"])


def test_note_anchor_carries_user_turns_candidates_and_draft(monkeypatch, tmp_path):
    """锚由代码给：轮内用户追加（逐字）、失败候选（带事件 ID）、结论草稿。"""
    agent, task = _agent(monkeypatch, tmp_path, note={
        "seq": 1, "sentence": "一句话", "failures": [], "ledger_append": {},
    })
    round_ = {
        "seq": 1,
        "user_input": {"original": "先做 A", "normalized": ""},
        "events": [
            make_event("R1-E01", "user", {"role": "user", "content": "先做 A"}),
            make_event("R1-E02", "user", {"role": "user", "content": "含网络检索的先不做"}),
            make_event("R1-E03", "tool_call", {
                "role": "assistant", "content": "",
                "tool_calls": [{"id": "c1", "type": "function",
                                "function": {"name": "run_command",
                                             "arguments": json.dumps({"command": "pytest"})}}],
            }),
            make_event("R1-E04", "tool_result", {
                "role": "tool", "tool_call_id": "c1",
                "content": "命令执行失败（超时 30 秒被强制终止）exit_code=1",
            }, tool_name="run_command"),
            make_event("R1-E05", "final_answer",
                       {"role": "assistant", "content": "做完了，结论是 X"}),
        ],
        "refined_index": {}, "end_state": "", "org_state": "",
    }
    agent.rounds = [round_]
    agent.current_round = round_
    agent.close_round()

    anchor = agent.llm.calls[-1]["messages"][-1]["content"]
    assert "含网络检索的先不做" in anchor               # 轮内追加逐字进锚
    assert "〔R1-E04〕" in anchor                       # 失败候选带事件 ID
    assert "结论草稿" in anchor and "做完了，结论是 X" in anchor


def test_note_switch_off_skips_settlement(monkeypatch, tmp_path):
    """开关关掉时：不结算、不打额外调用。"""
    agent, task = _agent(monkeypatch, tmp_path,
                         note={"seq": 1, "sentence": "x", "failures": [], "ledger_append": {}},
                         enabled=False)
    agent.run("问一句")
    assert "note_state" not in task.rounds[-1]
    assert len(agent.llm.calls) == 1


def test_submit_round_note_guard_is_noop_in_work(monkeypatch, tmp_path):
    """工作期误调用只回说明文本，无副作用。"""
    agent, _task = _agent(monkeypatch, tmp_path, note=None, enabled=False)
    out = agent.submit_round_note(seq=1, sentence="x")
    assert "仅由轮闭合" in out


def test_note_call_exception_is_reported_not_swallowed(monkeypatch, tmp_path):
    """结算调用抛异常（如 429）：留痕要写清是什么异常，不能只说"没有返回"。"""
    agent, task = _agent(monkeypatch, tmp_path, note={
        "seq": 1, "sentence": "x", "failures": [], "ledger_append": {},
    })

    original = agent._stream_call

    def boom(messages, **kwargs):
        if any("[结算指令]" in str(m.get("content") or "") for m in messages):
            raise RuntimeError("Error code: 429 - weekly usage limit")
        return original(messages, **kwargs)

    monkeypatch.setattr(agent, "_stream_call", boom)
    agent.run("问一句")

    assert task.rounds[-1]["note_state"] == "failed"
    assert any("结算调用异常" in line and "429" in line for line in _note_records(task))
