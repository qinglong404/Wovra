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

    # 合并后的名字要把原来那摊活说出来（名字即路由身份，不能名不副实）
    assert [d["name"] for d in domains] == ["附件与装配＋前端", "工具层"]
    kept = next(d for d in domains if d["name"].startswith("附件与装配"))
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


def test_report_domain_does_become_an_agent(monkeypatch, tmp_path):
    """**留痕/报告类域照常建 agent**（2026-09-17 用户口径更正）。

    我先前加过"留痕类域一律不建 agent"的准则，理解偏了：用户要的是"可以有一个 agent
    专门写报告"，而**活性文件不许无人管**——无人管就是机制错误（见落地后的完整性检查）。
    """
    from wovra import registry as registry_module

    agent, _task = _agent(monkeypatch, tmp_path, _rounds_with_file("a.py"))
    domains = [
        {"name": "报告与留痕", "files": ["docs/worklog.md", ".gitignore"],
         "description": "写 worklog 与自检报告"},
        {"name": "真活", "files": ["src/wovra/attachments.py"]},
    ]
    assert not hasattr(agent, "_demote_bookkeeping_domains")   # 那条准则已拆掉
    entries = registry_module.build_entries(domains, "")
    assert [e["name"] for e in entries] == ["报告与留痕", "真活"]


def test_ownerless_live_file_is_reported_loudly(monkeypatch, tmp_path):
    """活性文件无人管 → 报错叫人（机制问题），不静默兜底。"""
    agent, task = _agent(monkeypatch, tmp_path, _rounds_with_file("src/wovra/attachments.py"))
    progress: list[str] = []
    agent.on_progress = progress.append

    # 注册表里只有 Main（且 Main 名下没有文件）＋ 该文件没有任何域认领 → 就是无人管
    task.registry = [{"id": "Main", "name": "主agent", "files": []}]
    orphans = agent._report_ownerless_live_files(task.registry)

    assert orphans == ["src/wovra/attachments.py"]
    assert any("有活性文件无人管" in p for p in progress)
    assert any(h.get("kind") == "split_defect" and "无人管" in str(h.get("detail"))
               for h in task.history)


def test_owned_live_file_is_quiet(monkeypatch, tmp_path):
    """有主就不报（正常态）。"""
    agent, task = _agent(monkeypatch, tmp_path, _rounds_with_file("src/wovra/attachments.py"))
    task.registry = [{"id": "Main", "name": "主agent", "files": []},
                     {"id": "A", "name": "附件", "files": ["src/wovra/attachments.py"]}]
    assert agent._report_ownerless_live_files(task.registry) == []


def test_identity_tools_only_advertised_after_split(monkeypatch, tmp_path):
    """**分裂前不广告身份类工具**（转交/咨询/会合/通知/名册/改职责）——省掉"我是谁"的思考。

    只影响"广告"（请求里的 tools 数组）；工具方法本身照旧注册着（直接调用可用）。
    """
    agent, task = _agent(monkeypatch, tmp_path, _rounds_with_file("a.py"))
    names = lambda a: {s["function"]["name"] for s in a._stage_schemas()}

    pre = names(agent)
    assert not pre & set(Agent._IDENTITY_TOOLS)
    assert "submit_round_notes" in pre and "expand_history" in pre   # 常数/提交类常驻

    task.registry.append({"id": "A", "name": "附件", "files": ["a.py"]})
    post = names(agent)
    assert set(Agent._IDENTITY_TOOLS) <= post
    assert post - pre == set(Agent._IDENTITY_TOOLS)                  # 只多出身份类


def test_system_prompt_is_staged_for_the_split(monkeypatch):
    """提示词分期：分裂前不写职责域/路由那套；分裂后追加（用户口径）。"""
    from wovra.cli.prompt import _system_prompt

    pre = _system_prompt("managed", "pre")
    post = _system_prompt("managed", "post")
    for word in ("route_to", "join_with", "update_responsibility", "职责域"):
        assert word not in pre, f"分裂前的提示词里不该出现 {word}"
        assert word in post
    assert "只有你一个 agent" in pre


def test_main_cannot_read_others_files_after_split(monkeypatch, tmp_path):
    """分裂后主 agent **连读也不干**：读别人文件 → 指路（交给主人，省一次读）。"""
    agent, task = _agent(monkeypatch, tmp_path, _rounds_with_file("a.py"))
    task.registry.append({"id": "A", "name": "附件", "files": ["src/wovra/attachments.py"]})
    agent.current_round = {"seq": 1, "active_view": "Main"}
    agent.rounds = [agent.current_round]

    blocked = agent._file_permission("read", "src/wovra/attachments.py")
    assert blocked and "读也算它的活" in blocked and "route_to" in blocked
    # 自己的文件照读；分裂前（P4）也照读
    assert agent._file_permission("read", "src/wovra/attachments.py") is not None
    task.registry[:] = [{"id": "Main", "name": "主agent"}]
    assert agent._file_permission("read", "src/wovra/attachments.py") is None


def test_spawn_creates_an_agent_in_place(monkeypatch, tmp_path):
    """**就地新建**（无主的活）：`route_to(agent="new")` 建一条空条目并把本回合交给它。

    不跑分裂分析、不重排注册表；名字先给占位（身份证不能空）。
    """
    agent, task = _agent(monkeypatch, tmp_path, _rounds_with_file("a.py"))
    agent.context_mode = "managed"
    round_ = {"seq": 9, "active_view": "Main", "events": [], "route_hops": 0}
    agent.rounds = [round_]
    agent.current_round = round_

    out = agent.route_to(agent="new", reason="这摊没人认领的收尾活")
    assert "就地新建" in out
    entry = next(e for e in task.registry if e["id"] == "A")
    assert entry["status"] == "active" and entry.get("name_provisional")
    assert entry["spawned_at"] == 9
    assert agent._pending_route == "A"                      # 本回合交给它
    assert round_["route_handoff"]["to"] == "A"
    assert any(h.get("kind") == "maintenance" and "就地新建 agent" in str(h.get("detail"))
               for h in task.history)


def test_tools_array_is_frozen_within_a_round(monkeypatch, tmp_path):
    """**轮内冻结 tools 数组**（AGENTS §2）：就地新建在轮中长出第一个子 agent 时，
    这一轮剩下的请求不许跟着换数组（换了＝整段前缀作废）。变化点落在下一次开轮。"""
    agent, task = _agent(monkeypatch, tmp_path, _rounds_with_file("a.py"))
    agent.context_mode = "managed"
    round_ = {"seq": 9, "active_view": "Main", "events": [], "route_hops": 0}
    agent.rounds = [round_]
    agent.current_round = round_
    agent._round_schemas = agent._compute_stage_schemas()      # 开轮时冻结（分裂前）
    before = [s["function"]["name"] for s in agent._stage_schemas()]
    assert "route_to" not in before

    agent.route_to(agent="new", reason="新活")                  # 轮中长出 A

    assert [s["function"]["name"] for s in agent._stage_schemas()] == before   # 冻结住
    agent._round_schemas = None                                 # 下一轮：按新阶段重算
    assert "route_to" in [s["function"]["name"] for s in agent._stage_schemas()]
