"""每轮一段话（V4 §3.2 ＋ §6.1）：**水位处攒批**结算——一批轮一次调用。

覆盖：一批一次调用、锚逐轮给（轮内用户发言/失败候选/结论草稿）、产物按 seq 落地、
缺哪轮只有哪轮顶着（下一批再收它）、账本整批一次（戳在末轮、结案只结更早批次）、
批次上限切分、调用异常只留痕不重试、软档不拦、尾部追加与 tools 恒定、开关与守卫，
以及"写完就在同一次轮闭合里折档"。
（默认档由 conftest 关掉——它会在水位处多打一次 LLM，本文件显式打开。）
"""

import json

from wovra import task as task_module
from wovra.agent import Agent
from wovra.agent import note as note_module
from wovra.task import Task
from wovra.truncate import make_event

from ._helpers import _StubLLM, _chunk, _delta, _fragment

_WATERMARK_OFF = 10 ** 9        # 闸门关着（跑轮期间不结算）


def _batch_chunk(notes, ledger=None):
    payload: dict = {"notes": notes}
    if ledger is not None:
        payload["ledger_append"] = ledger
    return [_chunk(_delta(tool_calls=[
        _fragment(0, id="n1", name="submit_round_notes",
                  arguments=json.dumps(payload, ensure_ascii=False)),
    ]))]


def _note(seq, sentence=None, failures=None):
    return {"seq": seq, "sentence": sentence or f"第{seq}轮结论",
            "failures": failures or []}


def _plain(text="答一句"):
    return [_chunk(_delta(content=text))]


def _batches(agent) -> list[dict]:
    """所有结算调用（尾部带结算指令的那些）。"""
    return [
        c for c in agent.llm.calls
        if any("[结算指令]" in str(m.get("content") or "") for m in c["messages"])
    ]


def _records(task: Task) -> str:
    return "\n".join(str(h.get("detail")) for h in task.history if h.get("kind") == "note")


def _agent(monkeypatch, tmp_path, pool, *, batch_max=None, enabled=True, **kw):
    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path)
    monkeypatch.setenv("WOVRA_ROUND_NOTE", "1" if enabled else "0")
    # 攒批结算的落点在 V4 那条路（旧链路不看 note，也不折档）
    monkeypatch.setenv("WOVRA_V4", "1")
    task = Task.create(goal="g")
    agent = Agent(llm=_StubLLM(pool), tools=[], task=task, **kw)
    if batch_max is not None:
        agent._note_batch_max = batch_max
    return agent, task


def _run(agent, count: int) -> None:
    for i in range(1, count + 1):
        agent.run(f"第 {i} 问")


def _trigger(agent, size: int = _WATERMARK_OFF) -> None:
    """到水位（闸门放行）→ 攒批结算 ＋ 分裂分析。"""
    agent.last_context_estimate = size
    agent._maybe_organize_batch()


def _fold(agent, size: int = _WATERMARK_OFF) -> None:
    """推进折档线（体量要重新注入：结算自己那次装配会刷新 `last_context_estimate`）。"""
    agent.last_context_estimate = size
    agent._advance_fold_line()


def _assembled(agent) -> str:
    return "\n".join(str(m.get("content")) for m in agent._assemble_messages())


# ---- 攒批：一批轮一次调用 ----


def test_watermark_writes_whole_batch_in_one_call(monkeypatch, tmp_path):
    """水位到了：这批几轮就一次调用写完（不是一个轮一次）。"""
    agent, task = _agent(
        monkeypatch, tmp_path,
        [_plain(), _plain(), _plain(),
         _batch_chunk([_note(1), _note(2), _note(3)])],
        org_watermark=_WATERMARK_OFF, org_grace_rounds=0,
    )
    _run(agent, 3)
    assert _batches(agent) == []                    # 没到水位：不结算
    _trigger(agent)
    agent._maybe_organize_batch()                   # 幂等：已结算的轮不再收

    assert len(_batches(agent)) == 1                # 3 轮 = 1 次调用
    assert [r["note_state"] for r in task.rounds] == ["done"] * 3
    assert [r["note"]["sentence"] for r in task.rounds] == \
        ["第1轮结论", "第2轮结论", "第3轮结论"]      # 按 seq 各归各轮
    assert all(r["note"]["executor"] == "Main" for r in task.rounds)
    assert "R1–R3 结算完成：3/3 条" in _records(task)


def test_no_settlement_before_watermark(monkeypatch, tmp_path):
    """水位没到：一次结算调用都不发，轮上不留 note 字段。"""
    agent, task = _agent(monkeypatch, tmp_path, [_plain(), _plain()],
                         org_watermark=_WATERMARK_OFF, org_grace_rounds=0)
    _run(agent, 2)

    assert len(agent.llm.calls) == 2
    assert all("note_state" not in r for r in task.rounds)
    assert _records(task) == ""


def test_batch_is_tail_append_with_per_round_anchor(monkeypatch, tmp_path):
    """形态：装配快照 ＋ 尾部一条指令（锚逐轮给）；tools 数组仍是完整那份。"""
    agent, task = _agent(
        monkeypatch, tmp_path,
        [_plain(), _plain(), _batch_chunk([_note(1), _note(2)])],
        org_watermark=_WATERMARK_OFF, org_grace_rounds=0,
    )
    _run(agent, 2)
    _trigger(agent)

    tail = _batches(agent)[-1]["messages"][-1]
    assert tail["role"] == "user"
    assert tail["content"].startswith("[结算指令]")
    assert "[锚]" in tail["content"]
    assert "这批 2 轮：R1–R2" in tail["content"]
    assert "R1（执行者：Main）" in tail["content"]      # 逐轮
    assert "R2（执行者：Main）" in tail["content"]
    call = _batches(agent)[-1]
    assert len(call["tools"]) == len(agent._stage_schemas())     # 与工作调用同序列化
    assert any(t["function"]["name"] == "submit_round_notes" for t in call["tools"])
    # 分裂前不广告身份类工具（没有别的 agent 可用；2026-09-17 用户口径）
    names = {t["function"]["name"] for t in call["tools"]}
    assert not names & {"route_to", "consult", "join_with", "notify",
                        "list_agents", "update_responsibility"}


def test_anchor_carries_user_turns_candidates_and_draft(monkeypatch, tmp_path):
    """锚由代码给：轮内用户追加（逐字）、失败候选（带事件 ID）、结论草稿。"""
    agent, _task = _agent(
        monkeypatch, tmp_path,
        [_batch_chunk([_note(1, "一句话")])],
        org_watermark=_WATERMARK_OFF, org_grace_rounds=0,
    )
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
    agent._settle_round_notes()

    anchor = _batches(agent)[-1]["messages"][-1]["content"]
    assert "含网络检索的先不做" in anchor               # 轮内追加逐字进锚
    assert "〔R1-E04〕" in anchor                       # 失败候选带事件 ID
    assert "结论草稿" in anchor and "做完了，结论是 X" in anchor


def test_batch_cap_splits_into_several_calls(monkeypatch, tmp_path):
    """批次超上限就切几次调用（前缀相同、缓存照样命中），产物仍按轮落地。"""
    agent, task = _agent(
        monkeypatch, tmp_path,
        [_plain() for _ in range(5)] + [
            _batch_chunk([_note(1), _note(2)]),
            _batch_chunk([_note(3), _note(4)]),
            _batch_chunk([_note(5)]),
        ],
        batch_max=2, org_watermark=_WATERMARK_OFF, org_grace_rounds=0,
    )
    _run(agent, 5)
    _trigger(agent)

    assert len(_batches(agent)) == 3
    assert [r["note_state"] for r in task.rounds] == ["done"] * 5


# ---- 失败与缺轮：只留痕、原文顶着、下一批再收 ----


def test_missing_round_leaves_only_it_raw(monkeypatch, tmp_path):
    """产物里少了哪一轮，就只有那一轮没产物（原文顶着），下一批再收它。"""
    agent, task = _agent(
        monkeypatch, tmp_path,
        [_plain() for _ in range(3)]
        + [_batch_chunk([_note(1), _note(3)])],
        org_watermark=_WATERMARK_OFF, org_grace_rounds=0,
    )
    _run(agent, 3)
    _trigger(agent)

    assert [r["note_state"] for r in task.rounds] == ["done", "failed", "done"]
    assert "R2 结算失败" in _records(task)
    assert [int(r["seq"]) for r in agent._note_pending_rounds()] == [2]


def test_foreign_seq_and_empty_sentence_are_dropped(monkeypatch, tmp_path):
    """硬门只看结构：seq 不在这批 / 句子为空 → 那一轮没产物，诊断只留痕。"""
    agent, task = _agent(
        monkeypatch, tmp_path,
        [_plain(), _plain(),
         _batch_chunk([_note(99), _note(2, sentence="   ")])],
        org_watermark=_WATERMARK_OFF, org_grace_rounds=0,
    )
    _run(agent, 2)
    _trigger(agent)

    assert [r["note_state"] for r in task.rounds] == ["failed", "failed"]
    detail = _records(task)
    assert "不在这批轮里" in detail and "句子为空" in detail


def test_soft_defects_still_land(monkeypatch, tmp_path):
    """内容正确性问题一律软档：产物照样落地，只在留痕里记一笔。"""
    agent, task = _agent(
        monkeypatch, tmp_path,
        [_plain(),
         _batch_chunk([_note(1, "改动落在 docs/prompt-review-2026-09-16.md，"
                              "并用 list_background 收了尾",
                              [{"text": "越界被拦", "evidence": "R1-E99"}])])],
        org_watermark=_WATERMARK_OFF, org_grace_rounds=0,
    )
    _run(agent, 1)
    _trigger(agent)

    assert task.rounds[-1]["note_state"] == "done"      # 一律落地
    assert "软档" in _records(task)


def test_call_exception_is_reported_and_not_retried(monkeypatch, tmp_path):
    """结算调用抛异常（如 429）：整批留痕写清异常；不重发。"""
    agent, task = _agent(
        monkeypatch, tmp_path,
        [_plain(), _plain(), _batch_chunk([_note(1), _note(2)])],
        org_watermark=_WATERMARK_OFF, org_grace_rounds=0,
    )
    original = agent._stream_call
    attempts: list[int] = []

    def boom(messages, **kwargs):
        if any("[结算指令]" in str(m.get("content") or "") for m in messages):
            attempts.append(1)
            raise RuntimeError("Error code: 429 - weekly usage limit")
        return original(messages, **kwargs)

    monkeypatch.setattr(agent, "_stream_call", boom)
    _run(agent, 2)
    _trigger(agent)

    assert [r["note_state"] for r in task.rounds] == ["failed", "failed"]
    assert "结算调用异常" in _records(task) and "429" in _records(task)
    assert len(attempts) == 1                            # 整批只发这一次，不重发


def test_switch_off_skips_settlement(monkeypatch, tmp_path):
    """开关关掉时：到水位也不结算、不打额外调用。"""
    agent, task = _agent(monkeypatch, tmp_path, [_plain()],
                         enabled=False, org_watermark=0, org_grace_rounds=0)
    _run(agent, 1)

    assert len(agent.llm.calls) == 1
    assert "note_state" not in task.rounds[-1]


def test_submit_round_notes_guard_is_noop_in_work(monkeypatch, tmp_path):
    """工作期误调用只回说明文本，无副作用。"""
    agent, _task = _agent(monkeypatch, tmp_path, [], enabled=False)
    out = agent.submit_round_notes(notes=[{"seq": 1, "sentence": "x"}])
    assert "仅由水位处的结算调用消费" in out


# ---- 账本：整批一次、戳在末轮、结案只结更早批次 ----


def test_ledger_batch_is_stamped_at_last_round_and_closes_only_earlier(monkeypatch, tmp_path):
    """账本：整批一次增量；戳写这批的末轮；只能结更早批次的条目。"""
    agent, task = _agent(
        monkeypatch, tmp_path,
        [_plain(), _plain(),
         _batch_chunk([_note(1), _note(2)], ledger={
             "known_issues": ["旧的坑", "新的坑"],
             "closed": [{"field": "known_issues", "match": "旧的坑"},
                        {"field": "known_issues", "match": "新的坑"}],
         })],
        org_watermark=_WATERMARK_OFF, org_grace_rounds=0,
    )
    task.task_state = {"goal": "g", "known_issues": ["旧的坑（R1·Main）"]}
    _run(agent, 2)
    _trigger(agent)

    issues = task.get_state().known_issues
    # 旧的坑被结掉；"新的坑"只进一次、带末轮戳；同批新增的结不掉
    assert [x.split("（")[0] for x in issues] == ["新的坑"]
    assert issues[0].endswith("（R2·Main）")
    assert "结案被拒" in _records(task)


# ---- 换档：写完就在同一次轮闭合里折档（§6.2/§6.3） ----


def test_batch_then_fold_in_the_same_close(monkeypatch, tmp_path):
    """水位到了：先攒批写产物，紧接着折档——装配里老轮成段落、最近一轮仍是原文。"""
    agent, task = _agent(
        monkeypatch, tmp_path,
        [_plain(), _plain(), _plain(),
         _batch_chunk([_note(1), _note(2), _note(3)])],
        org_watermark=_WATERMARK_OFF, org_grace_rounds=0,
    )
    _run(agent, 3)
    _trigger(agent)
    _fold(agent)

    # 范围就是这批（含刚闭合那一轮）：R1–R3 全折
    assert [bool(r.get("folded")) for r in task.rounds] == [True, True, True]
    assembled = _assembled(agent)
    for seq in (1, 2, 3):
        assert f"第{seq}轮结论" in assembled          # 每轮成了段落
        assert f"👤 第 {seq} 问" in assembled         # 用户原话逐字仍在
    assert "答一句" not in assembled                 # 原文不再占位
    assert any(h.get("kind") == "fold" for h in task.history)


def test_round_without_note_stays_raw(monkeypatch, tmp_path):
    """没有产物的轮不折档：原文继续顶着（不重发、不跨轮）。"""
    agent, task = _agent(
        monkeypatch, tmp_path,
        [_plain(), _plain(),
         _batch_chunk([_note(2)])],                  # 产物里没有 R1
        org_watermark=_WATERMARK_OFF, org_grace_rounds=0,
    )
    _run(agent, 2)
    _trigger(agent)
    _fold(agent)

    assert task.rounds[0].get("note_state") == "failed"
    assert not task.rounds[0].get("folded")          # 没产物 → 不折档（fail-safe）
    assert task.rounds[1].get("folded")              # 有产物的照折（含刚闭合那轮）
    assembled = _assembled(agent)
    assert "第2轮结论" in assembled and "答一句" in assembled


def test_paragraph_carries_in_round_user_turn_verbatim(monkeypatch, tmp_path):
    """段落槽的用户侧：轮头 ＋ 轮内追加**逐字**（前提档不能丢）。"""
    agent, _task = _agent(
        monkeypatch, tmp_path,
        [_batch_chunk([_note(1, "一句话")])],
        org_watermark=_WATERMARK_OFF, org_grace_rounds=0,
    )
    round_ = {
        "seq": 1,
        "user_input": {"original": "先做 A", "normalized": ""},
        "events": [
            make_event("R1-E01", "user", {"role": "user", "content": "先做 A"}),
            make_event("R1-E02", "user", {"role": "user", "content": "含网络检索的先不做"}),
            make_event("R1-E03", "final_answer", {"role": "assistant", "content": "做完了"}),
        ],
        "refined_index": {}, "end_state": "", "org_state": "",
    }
    agent.rounds = [round_]
    agent.current_round = round_
    agent.close_round()
    agent._settle_round_notes()
    round_["folded"] = True          # 单轮夹具：手工标成折叠，验段落槽的渲染

    assembled = _assembled(agent)
    assert "👤 先做 A" in assembled
    assert "👤（轮内追加）含网络检索的先不做" in assembled


def _big_round(seq: int, chars: int = 2400) -> dict:
    """一个大轮（原文体量够撑起上下文）：一进一出 ＋ 一段长回答 ＋ 现成产物。"""
    return {
        "seq": seq,
        "user_input": {"original": f"第 {seq} 问", "normalized": ""},
        "events": [
            make_event(f"R{seq}-E01", "user", {"role": "user", "content": f"第 {seq} 问"}),
            make_event(f"R{seq}-E02", "final_answer",
                       {"role": "assistant", "content": "结论" * (chars // 2)}),
        ],
        "refined_index": {}, "end_state": "completed", "org_state": "",
        "note_state": "done",
        "note": {"seq": seq, "sentence": f"第{seq}轮结论", "failures": [],
                 "executor": "Main", "ledger_append": {}},
    }




def test_fold_goes_all_the_way_below_watermark_then_waits(monkeypatch, tmp_path):
    """换档**一次折够**（折到水位以下）→ 上下文没重新长上来之前不再折。

    实测动因：按"逐轮推进"会让水位悬在线上时**每轮断一次前缀**（每个工作调用白付
    200~260 tok，大会话里这笔等于尾部体量）。
    """
    agent, task = _agent(
        monkeypatch, tmp_path,
        [_plain() for _ in range(5)]
        + [_batch_chunk([_note(i) for i in range(1, 6)])],
        org_watermark=5000, org_grace_rounds=0,
    )
    _run(agent, 5)
    agent._fold_target = 0.6
    _trigger(agent, size=9000)                       # 到线（>5000）
    _fold(agent, size=9000)

    folded = [int(r["seq"]) for r in task.rounds if r.get("folded")]
    assert len(folded) > 1, f"一次要到水位以下，而不是只折一轮：{folded}"
    detail = [str(h.get("detail")) for h in task.history if h.get("kind") == "fold"][-1]
    assert "折到水位" in detail and "60%" in detail

    # 滞后：体量已经落到水位之下 → 再调也不折
    agent.last_context_estimate = 3000
    agent._advance_fold_line()
    assert [int(r["seq"]) for r in task.rounds if r.get("folded")] == folded


# ---- 分裂成多 agent 之后：一轮一整理，一轮多 agent 就每段一整理 ----


def _sub_agents(agent, *ids):
    """把注册表改成长出子 agent 的样子（分裂落过地的机械信号）。"""
    agent.task.registry = [{"id": "Main", "name": "主agent"}] + [
        {"id": i, "name": i, "status": "active"} for i in ids
    ]


def _two_executor_round(agent) -> dict:
    """一轮：主 agent 转交（route_to）→ A 干活回答。事件流自带交接锚点。"""
    events = [
        make_event("R1-E01", "user", {"role": "user", "content": "这个活你做"}),
        make_event("R1-E02", "tool_call", {
            "role": "assistant", "content": "",
            "tool_calls": [{"id": "c0", "type": "function",
                            "function": {"name": "route_to",
                                         "arguments": json.dumps({"agent": "A",
                                                                  "message": "接手"})}}],
        }),
        make_event("R1-E03", "tool_result", {
            "role": "tool", "tool_call_id": "c0", "content": "已转交 A"},
            tool_name="route_to"),
        make_event("R1-E04", "final_answer", {"role": "assistant", "content": "A 做完了"}),
    ]
    return {
        "seq": 1, "user_input": {"original": "这个活你做", "normalized": ""},
        "events": events, "refined_index": {}, "end_state": "completed",
        "org_state": "", "active_view": "A", "route_hops": 1, "blocks": [],
    }


def test_per_round_close_after_split_settles_every_segment(monkeypatch, tmp_path):
    """分裂后：轮闭合即结算；一轮多 agent → **每段一次**调用，各盖各的执行者。"""
    agent, task = _agent(
        monkeypatch, tmp_path,
        [_batch_chunk([_note(1, "主 agent 把活转给了 A")]),
         _batch_chunk([_note(1, "A 接手并做完")])],
        org_watermark=_WATERMARK_OFF, org_grace_rounds=0,
    )
    _sub_agents(agent, "A")
    round_ = _two_executor_round(agent)
    agent.rounds = [round_]
    agent.current_round = round_
    agent.close_round()

    assert len(_batches(agent)) == 2                       # 两段 = 两次调用
    assert round_["note_state"] == "done"
    assert [(s["executor"], s["sentence"]) for s in round_["note_segments"]] == [
        ("Main", "主 agent 把活转给了 A"), ("A", "A 接手并做完"),
    ]
    anchored = [_batches(agent)[0]["messages"][-1]["content"],
                _batches(agent)[1]["messages"][-1]["content"]]
    assert "第 1/2 段" in anchored[0] and "只写这一段" in anchored[0]
    assert "第 2/2 段" in anchored[1]
    assert "R1·Main第1/2段" in _records(task) and "R1·A第2/2段" in _records(task)
    # 分段渲染：一行一段（执行者非 Main 才挂名字）
    rendered = note_module.render_note(round_)
    assert "[R1] 主 agent 把活转给了 A" in rendered
    assert "[R1]〔A〕 A 接手并做完" in rendered


def test_split_phase_does_not_wait_for_watermark(monkeypatch, tmp_path):
    """分裂后不再攒批：水位远在天边也照写在轮闭合处（攒批只管分裂前那一段）。"""
    agent, task = _agent(
        monkeypatch, tmp_path,
        [_plain(), _batch_chunk([_note(1)])],
        org_watermark=_WATERMARK_OFF, org_grace_rounds=0,
    )
    _sub_agents(agent, "A")
    _run(agent, 1)

    assert [r["note_state"] for r in task.rounds] == ["done"]
    assert len(_batches(agent)) == 1


def test_partial_segment_failure_keeps_round_unfolded(monkeypatch, tmp_path):
    """一段没写成 → 整轮不算齐（note_state 不是 done）→ 不折档，原文顶着。"""
    agent, task = _agent(
        monkeypatch, tmp_path,
        [_batch_chunk([_note(1, "主 agent 转交")]),
         [_chunk(_delta(content="（没提交）"))]],
        org_watermark=_WATERMARK_OFF, org_grace_rounds=0,
    )
    _sub_agents(agent, "A")
    round_ = _two_executor_round(agent)
    agent.rounds = [round_]
    agent.current_round = round_
    agent.close_round()

    assert [s["executor"] for s in round_["note_segments"]] == ["Main"]   # 只落了第一段
    assert round_["note_state"] == "failed"        # 没齐 → 不许折档
    agent.last_context_estimate = _WATERMARK_OFF
    agent._advance_fold_line()
    assert not round_.get("folded")
    # 补漏：下一批（水位处）只捡缺的那一段
    assert [(r["seq"], seg["executor"]) for r, seg in agent._note_jobs([round_])] == [(1, "A")]


def test_backlog_sweep_after_split_batches_whole_rounds(monkeypatch, tmp_path):
    """分裂后补漏：**整轮一条都没写成的**合成一批补（补账不是运行节奏，攒着写）。"""
    agent, task = _agent(
        monkeypatch, tmp_path,
        [_plain(), _batch_chunk([_note(1)]),        # 每轮：工作 + 轮闭合处的一次结算
         _plain(), _batch_chunk([_note(2)]),
         _plain(), _batch_chunk([_note(3)]),
         _batch_chunk([_note(1), _note(2), _note(3)])],   # 水位处补漏：攒成一批
        org_watermark=_WATERMARK_OFF, org_grace_rounds=0,
    )
    _sub_agents(agent, "A")
    _run(agent, 3)                                  # 分裂后的运行：每轮各一次（3 次）
    assert len(_batches(agent)) == 3
    for r in task.rounds:                           # 把三段产物抹掉，模拟"上一批没写成"
        r.pop("note_segments", None)
        r.pop("note", None)
        r["note_state"] = "failed"
    _trigger(agent)                                 # 水位处的补漏

    assert len(_batches(agent)) == 4                # 3 轮积压 = **一批**（不是 3 次）
    assert [r["note_state"] for r in task.rounds] == ["done"] * 3
    assert "R1–R3 结算完成：3/3 条" in _records(task)


def test_multi_executor_backlog_goes_segment_by_segment(monkeypatch, tmp_path):
    """补漏时**一轮多 agent 的轮**按段补：不攒批（攒批会把两家写成一条、执行者挂错）。"""
    agent, task = _agent(
        monkeypatch, tmp_path,
        [_batch_chunk([_note(1, "主 agent 转交")]),
         _batch_chunk([_note(1, "A 做完")])],
        org_watermark=_WATERMARK_OFF, org_grace_rounds=0,
    )
    _sub_agents(agent, "A")
    round_ = _two_executor_round(agent)
    round_["note_state"] = "failed"          # 上批整轮没写成 → 进补漏
    agent.rounds = [round_]
    _trigger(agent)

    assert len(_batches(agent)) == 2                      # 两段 = 两次（不是一批）
    assert [(s["executor"], s["sentence"]) for s in round_["note_segments"]] == [
        ("Main", "主 agent 转交"), ("A", "A 做完"),
    ]
    assert "note" not in round_                           # 整轮那条不会混进来
    assert round_["note_state"] == "done"




def test_note_carries_awaiting_user_and_the_tail(monkeypatch, tmp_path):
    """**等你答复**：段落里单列一行，锚里给"结尾原话"（结论草稿只取开头，取不到它）。"""
    agent, task = _agent(
        monkeypatch, tmp_path,
        [_plain(), _batch_chunk([{
            "seq": 1, "sentence": "给出八条不顺手项",
            "failures": [], "awaiting_user": "要不要让 B 把第 5/6/8 条合进报告",
        }])],
        org_watermark=_WATERMARK_OFF, org_grace_rounds=0,
    )
    _run(agent, 1)
    _trigger(agent)

    note = task.rounds[0]["note"]
    assert note["awaiting_user"] == "要不要让 B 把第 5/6/8 条合进报告"
    assert "⏳ 等你答复：要不要让 B 把第 5/6/8 条合进报告" in note_module.render_note(task.rounds[0])


def test_anchor_carries_the_final_answer_tail():
    """锚里给结尾原话——"我在等用户拍板"的话在结尾，草稿（开头）里没有。"""
    round_ = {
        "seq": 3, "user_input": {"original": "看下", "normalized": ""},
        "events": [
            make_event("R3-E01", "user", {"role": "user", "content": "看下"}),
            make_event("R3-E02", "final_answer",
                       {"role": "assistant",
                        "content": "八条如下：①…（长正文）…要不要让 B 把这几条合进去？（我不动手，等你说。）"}),
        ],
        "refined_index": {}, "end_state": "", "org_state": "",
    }
    anchor = "\n".join(note_module.anchor_lines(round_))
    assert "结尾原话" in anchor and "我不动手，等你说" in anchor


# ---- 异步维护 ＋ 落地后阻塞补齐（2026-09-17 用户口径"异步吧"）----


def test_async_dispatch_does_not_block_the_close(monkeypatch, tmp_path):
    """异步：触发那一刻**批次冻结入队**，轮闭合处不等它（不结算任何东西）。"""
    agent, task = _agent(
        monkeypatch, tmp_path,
        [_plain(), _plain(), _batch_chunk([_note(1), _note(2)])],
        org_watermark=_WATERMARK_OFF, org_grace_rounds=0,
    )
    agent.async_organization = True
    # 不起真线程（否则它会在测试结束后继续跑，与本套其它用例抢时序）
    agent._ensure_worker = lambda: None
    _run(agent, 2)
    agent.last_context_estimate = _WATERMARK_OFF

    agent._maybe_organize_batch()

    assert agent._org_queue.qsize() == 1                    # 批次进了队列
    kind, batch, _snapshot = agent._org_queue.get_nowait()
    assert kind == "v4" and [int(r["seq"]) for r in batch] == [1, 2]
    assert _batches(agent) == []                            # 还没发结算调用（不阻塞）
    assert [r.get("note_state") for r in task.rounds] == [None, None]


def test_catch_up_settles_rounds_closed_during_maintenance(monkeypatch, tmp_path):
    """**阻塞补齐**：维护期间聊的轮（R9 那类）无论多小都整理掉，并把折档线推到目标。

    "触发整理期间聊的，无论多大，也将其整理了……阻塞式，防止整理期间又聊新的"
    """
    agent, task = _agent(
        monkeypatch, tmp_path,
        [_plain(), _plain(), _plain(),
         _batch_chunk([_note(1), _note(2), _note(3)])],
        org_watermark=_WATERMARK_OFF, org_grace_rounds=0,   # 跑轮期间闸门关着
    )
    _run(agent, 3)
    agent._open_round_on_disk = lambda: None           # 没有开放轮 → 可以折档

    agent._catch_up_after_maintenance()

    assert [r["note_state"] for r in task.rounds] == ["done"] * 3   # 全补齐
    detail = "\n".join(str(h.get("detail")) for h in task.history if h.get("kind") == "note")
    assert "维护期间闭合的轮已补齐：R1–R3" in detail
    agent._org_watermark = 1                           # 目标线落到体量之下 → 折档该推
    agent.last_context_estimate = 9000
    agent._catch_up_after_maintenance()
    assert any(r.get("folded") for r in task.rounds)                # 折档也推了


def test_catch_up_settles_but_does_not_fold_with_an_open_round(monkeypatch, tmp_path):
    """有开放轮时补齐**只结算、不折档**——改 `folded` 会改装配字节，轮中部动它撞 §2。"""
    agent, task = _agent(
        monkeypatch, tmp_path,
        [_plain(), _plain(), _batch_chunk([_note(1), _note(2)])],
        org_watermark=_WATERMARK_OFF, org_grace_rounds=0,
    )
    _run(agent, 2)
    agent._open_round_on_disk = lambda: {"seq": 3, "end_state": "open"}
    agent._org_watermark = 1                           # 该折的条件都成立，只差"没有开放轮"
    agent.last_context_estimate = 9000

    agent._catch_up_after_maintenance()

    assert [r["note_state"] for r in task.rounds] == ["done"] * 2
    assert not any(r.get("folded") for r in task.rounds)            # 一个都没折


def test_fold_is_decided_by_watermark_not_by_round_number(monkeypatch, tmp_path):
    """折档只看水位：会话早期（轮号很小）冲过线也要折得动（2026-09-18 事故）。

    实测会话 `20260918-095302-f8c67a`：4 轮顶着 261K，水位早过了，折档却一轮没折——
    选轮里那条"最近 N 轮不折"的窗口守卫在会话早期是恒真的（`current − 50 < 0`），
    触发一次、`staged` 空着静默返回。现在窗口与轮次上限都已删除（用户："将 50 轮
    那个机制彻底删了，没啥大用"）：到线就折，最老的先，折到目标线以下为止。
    """
    agent, task = _agent(
        monkeypatch, tmp_path,
        [_plain(), _plain(), _plain(),
         _batch_chunk([_note(1), _note(2), _note(3)])],
        org_watermark=_WATERMARK_OFF, org_grace_rounds=0,
    )
    _run(agent, 3)
    _trigger(agent)                                    # 到水位（R1–R3 都在窗口内也要折）
    _fold(agent)

    folded = [int(r["seq"]) for r in task.rounds if r.get("folded")]
    assert folded == [1, 2, 3], f"到线必须折得动（轮号不参与判定）：{folded}"
    detail = [str(h.get("detail")) for h in task.history if h.get("kind") == "fold"][-1]
    assert "换档" in detail and "折后" in detail

    # 没到水位：新来一轮有产物也照样留原文（轮号再大也不折——轮次上限已删）
    task.rounds.append(_big_round(4, chars=40))
    agent.rounds = task.rounds
    agent.last_context_estimate = 10
    agent._advance_fold_line()
    assert not task.rounds[3].get("folded")


def test_fold_without_candidates_leaves_a_trace(monkeypatch, tmp_path):
    """到线却一轮没折 → 留痕（否则"没折"与"没触发"在 history 里长得一样）。"""
    agent, task = _agent(monkeypatch, tmp_path, [], org_watermark=1000, org_grace_rounds=0)
    task.rounds = [_big_round(1, chars=40)]
    task.rounds[0]["note_state"] = "failed"            # 唯一的轮没有产物
    agent.rounds = task.rounds
    agent.last_context_estimate = 5000                 # 过线

    agent._advance_fold_line()

    assert not any(r.get("folded") for r in task.rounds)
    detail = [str(h.get("detail")) for h in task.history if h.get("kind") == "fold"][-1]
    assert "折档未折" in detail
