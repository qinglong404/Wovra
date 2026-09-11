"""共享测试夹具与工具（自 test_agent.py 拆分）。"""

import json
import re
import sys
from types import SimpleNamespace
import pytest
from wovra import task as task_module
from wovra.agent import Agent, _schema_of
from wovra.tools import read_file
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
def _boom_stream():
    """流中途抛服务端错误（实测 2026-09-08：internal error 打断思考流）。"""
    import openai

    yield _chunk(_delta(reasoning="想了一半"))
    raise openai.APIError(
        "The service encountered an unexpected internal error. Request id: 0217",
        None,
        body=None,
    )
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
def _batch_org_json(rounds: list[int], goal: str = "批量目标") -> str:
    """构造一次批量整理调用的合法输出。"""
    return json.dumps({
        "rounds": [
            {"seq": seq, "normalized_user_input": f"R{seq} 意图", "refined_index": []}
            for seq in rounds
        ],
        "state_patch": {"goal": goal, "completed": ["一次搞定"]},
    }, ensure_ascii=False)
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
def _mk_file_round(seq: int, user_text: str, paths: list[str]) -> dict:
    """构造一轮：user + 每路径一个 write_file 调用/结果 + final。"""
    events = [
        {"id": f"R{seq}-E01", "type": "user",
         "message": {"role": "user", "content": user_text}},
    ]
    eid = 2
    for i, path in enumerate(paths):
        events.append({
            "id": f"R{seq}-E0{eid}", "type": "tool_call",
            "message": {"role": "assistant", "content": "",
                        "tool_calls": [{"id": f"c{seq}-{i}",
                                        "function": {"name": "write_file",
                                                     "arguments": json.dumps({"path": path})}}]},
        })
        eid += 1
        events.append({
            "id": f"R{seq}-E0{eid}", "type": "tool_result",
            "message": {"role": "tool", "tool_call_id": f"c{seq}-{i}", "content": "ok"},
        })
        eid += 1
    events.append({"id": f"R{seq}-E0{eid}", "type": "final_answer",
                   "message": {"role": "assistant", "content": "done"}})
    return {
        "seq": seq,
        "user_input": {"original": user_text, "normalized": ""},
        "events": events,
        "end_state": "completed",
    }
def _split_fixture(monkeypatch, tmp_path, org_pool, split_pool):
    """构造带水位触发的 agent：org 池 / split 池分别给响应。"""
    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path)
    task = Task.create(goal="目标")
    task.rounds = [_round(1, "第一轮", "答案")]
    task.rounds[0]["org_state"] = ""
    agent = Agent(
        llm=_StubLLM(org_pool, split_responses=split_pool),
        tools=[], task=task, org_watermark=0, org_grace_rounds=0,
        org_cooldown_rounds=0,
    )
    agent.last_context_estimate = 5000
    return agent, task
def _domains_chunk():
    return _chunk(_delta(tool_calls=[
        _fragment(0, id="d1", name="submit_domains", arguments=_DOMAINS_ARGS),
    ]))
def _org_json():
    return json.dumps({
        "rounds": [{"seq": 1, "normalized_user_input": "意图",
                    "key_constraints": "", "block_summaries": []}],
        "state_patch": {},
    }, ensure_ascii=False)

_DOMAINS_ARGS = json.dumps({
    "thoughts": [{"block_id": "R1-B1", "related_domain": "",
                  "reason": "寒暄，独立思想归主 agent"}],
    "domains": [{
        "name": "web 演示",
        "description": "纯 HTML 演示页，产出可视化灵感",
        "file_domains": ["index.html"],
        "block_ids": ["R1-B1"],
    }],
    "split_assessment": {"splittable": False, "reason": "单一活性文件域"},
}, ensure_ascii=False)

__all__ = [
    "_StubLLM",
    "_agent_with",
    "_delta",
    "_fragment",
    "_chunk",
    "_usage",
    "_boom_stream",
    "_round_agent",
    "_batch_org_json",
    "_round",
    "_make_open_round",
    "_mk_file_round",
    "_split_fixture",
    "_domains_chunk",
    "_org_json",
    "_DOMAINS_ARGS",
]
