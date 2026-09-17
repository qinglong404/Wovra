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
