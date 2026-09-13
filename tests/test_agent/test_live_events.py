"""事件级直播（`Agent.on_event`）——worklog §92。

用户报（原话）："思考连一块，肯定是有问题，思考下面要么跟回答，要么调用工具，
我发现这把中间调用工具的块，没有渲染出来…不能老老实实的按时间顺序，出来一个事件
渲染一个消息块吗？"

病根在**信息通道**：直播流原先只推 `think`/`ans`/`status` 三种**文本增量**，
**根本不推事件**——工具调用只存在于轮事件里，而轮事件要等轮结束那次
`/rounds/{seq}` 拉取才到前端。所以运行中只能看到"思考、思考、思考…"，
中间的工具块在**服务端就没推**。

修法：`_record_event`（事件落账的**唯一入口**）在落盘后把事件的**直播副本**推给
`on_event`；副本形状与 `/rounds/{seq}` 完全一致（`serve.round_detail` 的扁平形状），
前端因此能用**同一套"事件 → 条目"映射**按时间顺序画。
"""
from wovra.agent import Agent
from wovra import task as task_module
from wovra.task import Task

from ._helpers import _StubLLM


def _agent(monkeypatch, tmp_path) -> Agent:
    (tmp_path / "tasks").mkdir(exist_ok=True)
    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path / "tasks")
    task = Task.create(goal="直播")
    agent = Agent(llm=_StubLLM(), tools=[], task=task)
    agent._open_or_reuse_round("干活")
    return agent


def test_record_event_pushes_flat_live_copy(monkeypatch, tmp_path):
    """每个事件落账时推一份**扁平副本**，形状与 `/rounds` 给前端的一致。"""
    agent = _agent(monkeypatch, tmp_path)
    got: list[dict] = []
    agent.on_event = got.append

    agent._record_event("user", {"role": "user", "content": "把 a.py 改掉"})
    ev = agent._record_event("tool_call", {
        "role": "assistant", "content": "先读一下",
        "tool_calls": [{"id": "c1", "type": "function",
                        "function": {"name": "read_file",
                                     "arguments": '{"path":"a.py"}'}}],
    })
    ev["thinking"] = "我得先看看这个文件"          # 思考挂在事件上（与真流程一致）
    agent.on_event(agent._live_event(ev))
    agent._record_event("tool_result", {"role": "tool", "tool_call_id": "c1",
                                        "content": "文件内容"})
    agent._record_event("final_answer", {"role": "assistant", "content": "改好了"})

    # ① 事件数对齐（每落一个推一个）
    assert [e["type"] for e in got] == [
        "user", "tool_call", "tool_call", "tool_result", "final_answer"]
    # ② 形状与 `/rounds` 的扁平事件一致：前端两边共用同一个"事件 → 条目"映射
    keys = {"id", "type", "agent", "time", "thinking", "status",
            "role", "content", "tool_calls", "tool_call_id"}
    for e in got:
        assert keys <= set(e), f"直播副本缺字段：{keys - set(e)}"
    # ③ 工具调用的 id/参数必须带着——前端就是靠它画工具卡、回填结果
    call = [e for e in got if e["type"] == "tool_call"][-1]
    assert call["tool_calls"][0]["id"] == "c1"
    assert call["thinking"] == "我得先看看这个文件"
    assert call["agent"] == "Main"                 # 执行方（主 agent 起手）
    # ④ 工具结果带着 tool_call_id，前端据此并入那张卡（不新开一行）
    res = [e for e in got if e["type"] == "tool_result"][-1]
    assert res["tool_call_id"] == "c1" and res["content"] == "文件内容"


def test_live_copy_truncates_long_content_but_disk_keeps_it_all(monkeypatch, tmp_path):
    """直播副本**截断**长正文（工具结果可以几百 KB），落盘那条必须完整。"""
    agent = _agent(monkeypatch, tmp_path)
    got: list[dict] = []
    agent.on_event = got.append
    big = "x" * 20000
    agent._record_event("tool_result", {"role": "tool", "tool_call_id": "c1",
                                        "content": big})

    assert len(got[0]["content"]) < len(big)
    assert "直播只显示前" in got[0]["content"]
    # 落盘的那条是完整的（直播只是副本，不丢信息）
    stored = agent.current_round["events"][-1]["message"]["content"]
    assert stored == big


def test_live_push_failure_never_breaks_the_round(monkeypatch, tmp_path):
    """直播推不出去**不许影响干活**（推送回调抛异常要吞掉）。"""
    agent = _agent(monkeypatch, tmp_path)

    def boom(_e):
        raise RuntimeError("客户端已断开")

    agent.on_event = boom
    ev = agent._record_event("final_answer", {"role": "assistant", "content": "答复"})
    assert ev["type"] == "final_answer"
    assert agent.current_round["events"][-1]["message"]["content"] == "答复"
