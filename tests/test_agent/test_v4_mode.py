"""V4 行为开关（`WOVRA_V4`，默认开）：取消重组 ＋ 整理停用 ＋ 分裂按活性文件。

本套测试**显式打开**它（conftest 默认关，让其余测试跑旧链路）。
"""

import json

from wovra import task as task_module
from wovra.agent import Agent
from wovra.task import Task
from wovra.truncate import make_event

from ._helpers import _StubLLM


def _agent(monkeypatch, tmp_path, rounds, **agent_kw):
    monkeypatch.setenv("WOVRA_V4", "1")
    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path)
    task = Task.create(goal="g")
    task.rounds = rounds
    agent = Agent(llm=_StubLLM(), tools=[], task=task, **agent_kw)
    agent.rounds = rounds
    return agent, task


def _rounds_with_file(path: str, seq: int = 1) -> list[dict]:
    """一轮：写了一个文件（活性文件的机械来源是 FileLedger 的写操作）。"""
    return [{
        "seq": seq,
        "user_input": {"original": "写个文件", "normalized": ""},
        "events": [
            make_event(f"R{seq}-E01", "user", {"role": "user", "content": "写个文件"}),
            make_event(f"R{seq}-E02", "tool_call", {
                "role": "assistant", "content": "",
                "tool_calls": [{"id": f"w{seq}", "type": "function",
                                "function": {"name": "write_file",
                                             "arguments": json.dumps({"path": path,
                                                                      "content": "x"})}}],
            }),
            make_event(f"R{seq}-E03", "tool_result",
                       {"role": "tool", "tool_call_id": f"w{seq}",
                        "content": f"已写入 {path}"}, tool_name="write_file"),
            make_event(f"R{seq}-E04", "final_answer",
                       {"role": "assistant", "content": "写好了"}),
        ],
        "refined_index": {}, "end_state": "completed", "org_state": "",
    }]


def test_v4_skips_view_divergence(monkeypatch, tmp_path):
    """取消重组：V4 下不再走视图分化装配（所有 agent 看同一份共享历史）。"""
    agent, task = _agent(monkeypatch, tmp_path, _rounds_with_file("a.py"))
    monkeypatch.setattr(agent, "_active_view", lambda: "A")

    def boom(*_args, **_kwargs):
        raise AssertionError("V4 下不该走视图分化装配")

    monkeypatch.setattr(agent, "_assemble_view_messages", boom)
    text = "\n".join(str(m.get("content")) for m in agent._assemble_messages())
    assert "写好了" in text          # 共享历史在


def test_v4_does_not_run_organization_but_does_split(monkeypatch, tmp_path):
    """整理停用（叙事由每轮一段话承担）；水位只驱动结构树。"""
    agent, _task = _agent(monkeypatch, tmp_path, _rounds_with_file("a.py"),
                          org_watermark=0, org_grace_rounds=0)
    agent.last_context_estimate = 10 ** 6
    calls: list[str] = []

    def boom(*_args, **_kwargs):
        raise AssertionError("V4 下不该再跑整理（org）")

    monkeypatch.setattr(agent, "_organize_rounds", boom)
    monkeypatch.setattr(agent, "_maybe_split_v4", lambda: calls.append("split"))
    agent._maybe_organize_batch()
    assert calls == ["split"]


def test_v4_split_reads_live_files_not_block_map(monkeypatch, tmp_path):
    """分裂的输入只有活性文件（路径 + 首行）；没有块地图、没有块归属。"""
    agent, task = _agent(monkeypatch, tmp_path, _rounds_with_file("gaia_bench/run.py"),
                         org_watermark=0, org_grace_rounds=0)
    seen: dict = {}

    def fake_stream(messages, tools=None, purpose="working"):
        seen["text"] = "\n".join(str(m.get("content")) for m in messages)
        seen["tools"] = tools
        return "", [{
            "id": "c1", "name": "submit_domains",
            "arguments": json.dumps({
                "domains": [{"name": "A", "parent": "", "path": "gaia_bench/"}],
                "responsibilities": {"A": "跑评测"},
            }, ensure_ascii=False),
        }], None

    monkeypatch.setattr(agent, "_stream_call", fake_stream)
    agent._maybe_split_v4()

    assert "[活性文件]" in seen["text"]
    assert "gaia_bench/run.py" in seen["text"]          # 活性文件进了输入
    assert "[分块地图]" not in seen["text"]              # 块地图不再进分裂输入
    assert task.rounds[0]["split_state"] == "ready"      # 批次标记
    assert any(str(e.get("name")) == "A" for e in (task.registry or []))   # 产物落注册表


def test_same_name_nodes_merge_into_one_agent(monkeypatch, tmp_path):
    """同名同父的顶层节点**机械归并**（实测缺陷：同名会被落地成两个同名 agent）。

    名字是路由与执行者索引的唯一身份——产物里同一个职责写成两个节点时，
    代码把它们合成一个（路径取并集），而不是造两个分不清的 agent。
    """
    domains = [
        {"name": "上下文装配与附件注入", "path": "src/wovra/attachments.py"},
        {"name": "上下文装配与附件注入",
         "paths": ["src/wovra/agent/assembly.py", "tests/test_attachments.py"]},
        {"name": "上下文装配与附件注入", "parent": "别的父", "path": "x.py"},
        {"name": "serve 接口层", "path": "src/wovra/serve.py"},
    ]
    notes = Agent._merge_same_name_domains(domains)

    assert len(domains) == 3                                   # 顶层两条合成一条
    merged = domains[0]
    assert merged["name"] == "上下文装配与附件注入"
    assert merged["paths"] == ["src/wovra/attachments.py",
                               "src/wovra/agent/assembly.py",
                               "tests/test_attachments.py"]
    assert "path" not in merged
    assert domains[1]["name"] == "上下文装配与附件注入"          # 父不同的同名节点不并
    assert domains[1]["parent"] == "别的父"
    assert "同名节点归并" in notes[0] and "×2" in notes[0]


def test_same_name_merge_reaches_registry_as_one_agent(monkeypatch, tmp_path):
    """归并后的产物落地 → 注册表里这个名字只出现一次。"""
    from wovra import registry as registry_module

    domains = [
        {"name": "域X", "path": "a/x.py", "description": "短"},
        {"name": "域X", "path": "b/x.py", "description": "更长的描述，落地时留下这条"},
    ]
    Agent._merge_same_name_domains(domains)
    assert domains[0]["paths"] == ["a/x.py", "b/x.py"]
    entries = registry_module.build_entries(domains, "")
    assert [e["name"] for e in entries] == ["域X"]              # 一个名字一个 agent
    assert entries[0]["description"].startswith("更长的描述")   # 描述取信息多的那条


def test_split_failure_leaves_reason_on_round(monkeypatch, tmp_path):
    """分裂失败必留痕：原因同时写**轮字段**（抗并发写丢 history）与 history。"""
    agent, task = _agent(monkeypatch, tmp_path, _rounds_with_file("a.py"))
    monkeypatch.setattr(agent, "_split_rounds", lambda *a, **k: False)
    agent._maybe_split_v4()

    assert task.rounds[0]["split_state"] == "failed"
    assert "分裂未返回可用产物" in task.rounds[0]["split_note"]
    assert any(h.get("kind") == "split" and "V4 分裂失败" in str(h.get("detail"))
               for h in task.history)


def test_split_failure_uses_pending_reason_and_exception(monkeypatch, tmp_path):
    """原因优先取暂存里的（产物不可用），异常路则带上异常原文。"""
    agent, task = _agent(monkeypatch, tmp_path, _rounds_with_file("a.py"))
    task.rounds[0]["pending_org"] = {"split_assessment": {"reason": "产物被截断"}}
    monkeypatch.setattr(agent, "_split_rounds", lambda *a, **k: False)
    agent._maybe_split_v4()
    assert task.rounds[0]["split_note"] == "产物被截断"

    agent2, task2 = _agent(monkeypatch, tmp_path, _rounds_with_file("a.py", seq=1))

    def boom(*_args, **_kwargs):
        raise RuntimeError("端点 400：产物结构非法")

    monkeypatch.setattr(agent2, "_split_rounds", boom)
    agent2._maybe_split_v4()
    assert "端点 400" in task2.rounds[0]["split_note"]
    assert task2.rounds[0]["split_state"] == "failed"


def test_split_instruction_forbids_duplicate_names():
    """指令里必须留着"别写两个同名节点"那条（防复发；代码归并是保证，这条是减压）。"""
    from wovra.agent.prompts import _SPLIT_LIVE_INSTRUCTIONS as text

    assert "别写成两个同名节点" in text


# ---- 过分裂的机械兜底（2026-09-17：用户"A-F 有些过分分裂了"）----


def _round_writing(seq: int, *paths: str) -> dict:
    """一轮：写若干个文件（活性文件＝被写过的文件，机械判据用）。"""
    events = [make_event(f"R{seq}-E00", "user", {"role": "user", "content": "干活"})]
    for i, path in enumerate(paths, 1):
        events.append(make_event(f"R{seq}-E{i:02d}-C", "tool_call", {
            "role": "assistant", "content": "",
            "tool_calls": [{"id": f"w{seq}-{i}", "type": "function",
                            "function": {"name": "write_file",
                                         "arguments": json.dumps({"path": path,
                                                                  "content": "x"})}}],
        }))
        events.append(make_event(f"R{seq}-E{i:02d}-R", "tool_result",
                                 {"role": "tool", "tool_call_id": f"w{seq}-{i}",
                                  "content": f"已写入 {path}"}, tool_name="write_file"))
    events.append(make_event(f"R{seq}-EF", "final_answer",
                             {"role": "assistant", "content": "写好了"}))
    return {"seq": seq, "user_input": {"original": "干活", "normalized": ""},
            "events": events, "refined_index": {}, "end_state": "completed",
            "org_state": ""}


def test_domains_never_apart_are_merged(monkeypatch, tmp_path):
    """**从未分开过**的两个域并成一个（活跃轮被包含，且共现 ≥2 轮）。"""
    rounds = [_round_writing(7, "a/one.py", "b/two.py"),
              _round_writing(8, "a/one.py", "b/two.py"),
              _round_writing(9, "a/one.py"),
              _round_writing(10, "c/three.py")]
    agent, _task = _agent(monkeypatch, tmp_path, rounds)
    domains = [
        # 活跃轮 {7,8,9}
        {"name": "附件与装配", "files": ["a/one.py", "b/two.py"], "description": "附件通道"},
        # 活跃轮 {7,8} ⊆ 上面 → 从没单独出现过 → 并
        {"name": "前端", "files": ["b/two.py"], "description": "前端"},
        # 活跃轮 {10} → 分开过 → 是另一条活
        {"name": "工具层", "files": ["c/three.py"], "description": "工具"},
    ]
    notes = agent._merge_never_apart_domains(domains, rounds)

    assert [d["name"] for d in domains] == ["附件与装配", "工具层"]
    kept = next(d for d in domains if d["name"] == "附件与装配")
    assert "b/two.py" in kept["files"]                 # 文件不丢
    assert notes and "前端" in notes[0]


def test_domains_with_identical_active_rounds_merge(monkeypatch, tmp_path):
    """活跃轮**完全相同**的两个域是一摊活（谁都没单独出现过）。"""
    rounds = [_round_writing(7, "a/one.py", "b/two.py")]
    agent, _task = _agent(monkeypatch, tmp_path, rounds)
    domains = [{"name": "甲", "files": ["a/one.py"]},
               {"name": "乙", "files": ["b/two.py"]}]

    assert agent._merge_never_apart_domains(domains, rounds)
    assert len(domains) == 1


def test_single_shared_round_with_exclusive_rounds_does_not_merge(monkeypatch, tmp_path):
    """只在一轮里同现、但各自都有独占轮 → 不并（否则一晚的活跃轮就把整棵树并成一个）。"""
    rounds = [_round_writing(7, "a/one.py", "b/two.py", "c/three.py"),
              _round_writing(8, "a/one.py"),
              _round_writing(9, "b/two.py"),
              _round_writing(10, "c/three.py")]
    agent, _task = _agent(monkeypatch, tmp_path, rounds)
    domains = [{"name": "甲", "files": ["a/one.py"]},
               {"name": "乙", "files": ["b/two.py"]},
               {"name": "丙", "files": ["c/three.py"]}]

    assert agent._merge_never_apart_domains(domains, rounds) == []
    assert len(domains) == 3


def test_bookkeeping_only_domain_does_not_become_an_agent(monkeypatch, tmp_path):
    """只由留痕/配置文件组成的顶层域 → 不建 agent（归主 agent 的横切事务）。"""
    from wovra import registry as registry_module

    agent, _task = _agent(monkeypatch, tmp_path, _rounds_with_file("a.py"))
    domains = [
        {"name": "留痕与自检", "files": ["docs/worklog.md", ".gitignore"],
         "description": "记录"},
        {"name": "真活", "files": ["src/wovra/attachments.py"]},
    ]
    notes = agent._demote_bookkeeping_domains(domains)

    assert domains[0].get("main_agent") is True          # 降级：不建 agent
    assert "留痕/配置类域不建 agent" in notes[0]
    assert not domains[1].get("main_agent")              # 有真活的域不受影响
    entries = registry_module.build_entries(domains, "")
    assert [e["name"] for e in entries] == ["真活"]
