"""③ 职责更新 / ④ 会合与传话线程 / ⑤ 用户 @ 与广播（worklog §65）。

这三样都是"分裂之后怎么协作"，故测试都建在**已分裂**的任务上：
`工具层`(A) 管 src/a.py，`前端`(B) 管 webui/x.js。
"""

import json

from wovra import registry as registry_module
from wovra import task as task_module
from wovra.agent import Agent
from wovra.task import Task

from ._helpers import _StubLLM


def _split_task(tmp_path, monkeypatch) -> Task:
    (tmp_path / "tasks").mkdir(exist_ok=True)
    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path / "tasks")
    monkeypatch.setattr("wovra.tools.safety.PROJECT_ROOT", tmp_path)
    task = Task.create(goal="协作测试")
    registry_module.merge_into(task.registry, [
        {"name": "工具层", "files": ["src/a.py"], "description": "工具层"},
        {"name": "前端", "files": ["webui/x.js"], "description": "前端"},
    ])
    return task


def _agent(task: Task, view: str) -> Agent:
    agent = Agent(llm=_StubLLM(), tools=[], task=task)
    agent.current_round = {"seq": 1, "events": [], "active_view": view,
                           "participants": []}
    agent.rounds.append(agent.current_round)
    return agent


def test_update_responsibility_is_immediate_and_guards_ownership(tmp_path, monkeypatch):
    """③ 自己改自己的职责/清单，立即生效；但**不能抢别人的文件**。"""
    task = _split_task(tmp_path, monkeypatch)
    agent = _agent(task, "工具层")

    out = agent.update_responsibility(
        description="工具层与守卫", add_files="src/new_helper.py",
        note="新需求长出来的文件",
    )
    assert "立即生效" in out
    entry = agent._registry_entry_for("工具层")
    assert "src/new_helper.py" in registry_module.entry_files(entry)
    assert entry["description"] == "工具层与守卫"
    # 留痕：归属变更走独立 kind
    assert any(e["kind"] == "ownership" and "new_helper" in e["detail"]
               for e in task.history)

    # 抢别人的文件 → 拒
    denied = agent.update_responsibility(add_files="webui/x.js")
    assert "已经属于" in denied and "前端" in denied
    assert "webui/x.js" not in registry_module.entry_files(
        agent._registry_entry_for("工具层")
    )


def test_join_with_queues_participant_and_delivers_thread(tmp_path, monkeypatch):
    """④ 会合：排队参与者 + 传话落盘（公开线程）+ 投递到对方收件箱。"""
    task = _split_task(tmp_path, monkeypatch)
    agent = _agent(task, "工具层")

    out = agent.join_with(agent="前端", task="你改 webui/x.js 的样式，我改 src/a.py")
    assert "已排队" in out and "本轮参与者" in out
    assert agent.current_round["participants"] == [
        {"agent": "前端", "task": "你改 webui/x.js 的样式，我改 src/a.py"}
    ]
    # 公开线程（像聊天软件）：谁交给谁、内容是什么
    assert task.chat and task.chat[-1]["to"] == "前端"
    assert task.chat[-1]["kind"] == "join"
    # 投递：对方的收件箱里有这条传话
    front = agent._registry_entry_for("前端")
    assert front["inbox"] and "会合" in front["inbox"][-1]["message"]


def test_relay_hands_over_then_round_closes(tmp_path, monkeypatch):
    """④ 串行交棒：我干完 → 交给队列里的下一个（不是立刻收轮）；全完才闭合。"""
    task = _split_task(tmp_path, monkeypatch)
    agent = _agent(task, "工具层")
    agent.join_with(agent="前端", task="样式")

    assert agent._relay_to_next_participant("我这边改完了：src/a.py") is True
    assert agent.current_round["active_view"] == "前端"      # 接力棒交出去了
    assert agent.current_round["participants"] == []         # 队列已空
    # 我的产出进了公开线程（接手方据此知道上一手干了什么）
    assert any(m["kind"] == "handoff" and "src/a.py" in m["text"]
               for m in task.chat)
    # 队列空了 → 不再交棒（下一个干完就正常收轮）
    assert agent._relay_to_next_participant("我也干完了") is False


def test_thread_is_delivered_once_into_assembled_envelope(tmp_path, monkeypatch):
    """④ 传话**只投递一次**：接手方的信封里出现 [传话]，送达即清。"""
    task = _split_task(tmp_path, monkeypatch)
    agent = _agent(task, "工具层")
    agent.join_with(agent="前端", task="样式")

    lines = agent._take_thread_lines("前端")
    assert lines and "[传话]" in lines[0]
    assert any("样式" in ln for ln in lines)
    assert agent._registry_entry_for("前端")["inbox"] == []   # 已送达
    assert agent._take_thread_lines("前端") == []             # 不重复投递


def test_file_map_not_injected_before_any_split(tmp_path, monkeypatch):
    """**还没分裂就不注入文件地图**（2026-09-13 用户口径）。

    用户原话："现在还没有触发一次分裂，不需要路由，因此不需要这个东西。"
    地图的用处是"谁维护哪些文件"（路由与权限的依据）；注册表里只有主 agent 时
    没有归属歧义、没有路由可走，注入纯属噪声，还白占一段前缀。
    """
    from wovra import views as views_module

    (tmp_path / "tasks").mkdir(exist_ok=True)
    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path / "tasks")
    monkeypatch.setattr("wovra.tools.safety.PROJECT_ROOT", tmp_path)
    (tmp_path / "output").mkdir(exist_ok=True)
    (tmp_path / "output" / "tool_probe.txt").write_text(
        "# Wovra 工具连通性探针\n", encoding="utf-8")
    task = Task.create(goal="未分裂")
    agent = _agent(task, registry_module.MAIN_AGENT_ID)
    # 分裂前主 agent 建的文件也会进它自己的清单（F5 谁创建谁拥有）——
    # 用户看到的那条地图行正是这么来的
    agent._claim_new_file("output/tool_probe.txt")
    lines, sig = views_module.file_map_lines(agent.rounds, task.registry)
    assert lines and sig, "地图本身算得出来——问题不在算，而在还没分裂就注入"
    assert any("tool_probe" in ln for ln in lines)

    assert agent._inject_file_map_if_changed() is False
    assert agent.current_round["events"] == []
    assert not task.file_map_sig, "没注入就不该盖章：分裂后第一张地图照样要进得来"


def test_persist_rebases_when_another_writer_touched_the_file(tmp_path, monkeypatch):
    """两个写者不许互相覆盖（worklog §86）。

    整理改异步之后，后台维护线程（旧 agent）与下一轮的 agent 会**同时写同一份
    task.json**，而两边写的都是整份文件——后写的会把先写的盖掉。最坏的一种：
    维护刚 promote 出来的分裂产物被下一轮的保存静默抹掉。

    口径：保存前用一次 stat 判"盘上是不是别人写过"，是就重读 + **并集合并**；
    两边都这么做，于是谁最后写、写下去的都是双方的并集。
    """
    import json as _json
    from wovra import task as task_module

    task = _split_task(tmp_path, monkeypatch)          # 已分裂：Main/工具层/前端
    agent = _agent(task, "工具层")
    agent.rounds[0]["org_state"] = "pending"
    agent._persist_rounds()
    sig = task._saved_sig
    assert sig, "保存后要记下『我写的是哪一版盘』"

    # 模拟**另一方**（后台维护线程 / 另一个 agent）往同一个文件里写：
    # 一轮新轮 + 一个全新的注册表条目
    path = task_module.TASKS_ROOT / task.id / "task.json"
    disk = _json.loads(path.read_text(encoding="utf-8"))
    disk["rounds"].append({"seq": 99, "user_input": {"original": "下一句"},
                           "events": [], "end_state": "completed"})
    disk["registry"].append({"id": "Z", "name": "别人新开的域", "files": ["z.py"]})
    path.write_text(_json.dumps(disk, ensure_ascii=False), encoding="utf-8")

    # 我这边再保存一次（我手里那份**不含**别人的轮与条目）
    agent.current_round["org_state"] = "done"
    agent._persist_rounds()

    after = _json.loads(path.read_text(encoding="utf-8"))
    seqs = [r.get("seq") for r in after["rounds"]]
    ids = [e.get("id") for e in after["registry"]]
    assert 99 in seqs, "别人的轮被我的整份保存盖掉了"
    assert "Z" in ids, "别人新增的注册表条目被我的整份保存盖掉了"
    assert "A" in ids, "我自己原有的条目被并集合并弄丢了"


def test_file_map_injected_only_when_changed(tmp_path, monkeypatch):
    """文件地图：**描述来自整理写的块摘要**，且**变了才注入**（用户口径）。

    注入形态 = 追加一条 `runtime_note` 进轮（进历史 → 此后在前缀里，只付一次
    钱、且每轮都看得到）。签名不变时再开一轮**不再注入**。
    """
    from wovra import views as views_module

    task = _split_task(tmp_path, monkeypatch)
    rounds = [{
        "seq": 1,
        "events": [
            {"id": "R1-E02", "type": "tool_call",
             "message": {"role": "assistant", "content": "", "tool_calls": [
                 {"id": "c1", "function": {"name": "write_file",
                                           "arguments": '{"path": "src/a.py"}'}}]}},
            {"id": "R1-E03", "type": "tool_result",
             "message": {"role": "tool", "tool_call_id": "c1", "content": "ok"}},
        ],
        "block_summaries": {},
    }]
    # 块 ID 由分块器现场生成，故摘要按真实 ID 挂上（写死 ID 会静默不匹配）
    from wovra import blocks as blocks_module

    bid = blocks_module.segment_round_by_file(rounds[0])[0]["id"]
    rounds[0]["block_summaries"] = {bid: "把 a.py 的边界判定改了"}
    task.rounds = rounds
    lines, sig = views_module.file_map_lines(rounds, task.registry)
    assert any("src/a.py" in ln and "把 a.py 的边界判定改了" in ln for ln in lines)
    assert sig and sig == views_module.file_map_lines(rounds, task.registry)[1]

    agent = _agent(task, "工具层")
    agent.current_round = {"seq": 2, "events": [], "active_view": "工具层"}
    assert agent._inject_file_map_if_changed() is True      # 首次 → 注入
    assert task.file_map_sig == sig
    assert any(e["type"] == "runtime_note"
               and "[文件地图]" in str((e.get("message") or {}).get("content") or "")
               for e in agent.current_round["events"])

    agent.current_round = {"seq": 3, "events": [], "active_view": "工具层"}
    assert agent._inject_file_map_if_changed() is False     # 没变 → 不注入
    assert agent.current_round["events"] == []

    # 文件清单变了（新建文件）→ 签名变 → 再注入一次
    agent.update_responsibility(add_files="src/extra.py")
    agent.current_round = {"seq": 4, "events": [], "active_view": "工具层"}
    assert agent._inject_file_map_if_changed() is True


def test_file_notes_short_description_first_map_then_per_round(tmp_path, monkeypatch):
    """文件的一句话描述（≤30 字）：**第一次由分裂产物自带，之后每轮 agent 自己改**。

    用户口径：不能"等好几回合才同步"——所以地图不是只在整理时刷新，而是
    干活的 agent 读/写/改文件时顺手把这句话改掉（`file_notes=路径=描述`），
    改完即落盘，下一轮开轮签名变化就注入新地图。
    """
    from wovra import views as views_module

    task = _split_task(tmp_path, monkeypatch)
    # ① 第一次地图：分裂产物自带 file_notes
    registry_module.merge_into(task.registry, [
        {"name": "工具层", "files": ["src/a.py"],
         "file_notes": {"src/a.py": "工具层的边界判定与守卫——这一句写得特别长" * 3}},
    ], parent_id="", retire_id="")
    entry = next(e for e in task.registry if e.get("name") == "工具层")
    note = entry["file_notes"]["src/a.py"]
    assert len(note) <= 30 and note.startswith("工具层的边界判定与守卫")   # 硬截断

    lines, sig = views_module.file_map_lines([], task.registry)
    assert any("工具层的边界判定与守卫" in ln for ln in lines)
    assert all(len(ln.split("：", 1)[-1]) <= 30 for ln in lines)

    # ② 之后每轮：agent 自己改（不必等整理）
    agent = _agent(task, "工具层")
    out = agent.update_responsibility(file_notes="src/a.py=守卫边界判定与审批通道")
    assert "✎src/a.py" in out
    entry = agent._registry_entry_for("工具层")
    assert entry["file_notes"]["src/a.py"] == "守卫边界判定与审批通道"
    new_lines, new_sig = views_module.file_map_lines([], task.registry)
    assert new_sig != sig                                   # 签名变 → 会重新注入
    assert any("守卫边界判定与审批通道" in ln for ln in new_lines)


def test_new_file_gets_mechanical_placeholder_note(tmp_path, monkeypatch):
    """新文件的兜底描述是**机械**的（零 LLM）：取首行标题/注释前 30 字。"""
    task = _split_task(tmp_path, monkeypatch)
    (tmp_path / "src").mkdir(exist_ok=True)
    (tmp_path / "src" / "fresh.md").write_text(
        "# 数据清洗脚本\n\n第一行标题就是它的一句话描述\n", encoding="utf-8")
    agent = _agent(task, "工具层")
    agent._claim_new_file("src/fresh.md")
    entry = agent._registry_entry_for("工具层")
    assert entry["file_notes"]["src/fresh.md"] == "数据清洗脚本"


def test_user_interjection_at_or_broadcast(tmp_path, monkeypatch):
    """⑤ 用户插话：`@X` 只给 X；没 @ 就广播给本轮参与者（当前接手方除外）。"""
    task = _split_task(tmp_path, monkeypatch)
    agent = _agent(task, "工具层")
    agent.join_with(agent="前端", task="样式")

    # @ 定向：只投给前端，工具层不受影响
    agent._deliver_user_input("@前端 你这块的配色要按之前定的来")
    front = agent._registry_entry_for("前端")
    tools = agent._registry_entry_for("工具层")
    assert any("配色" in it["message"] for it in front["inbox"])
    assert tools["inbox"] == []
    assert [m["to"] for m in task.chat if m["kind"] == "user"] == ["前端"]

    # 没 @ → 广播给参与者（前端），当前接手方（工具层）走正常消息通道
    agent._deliver_user_input("两边都注意一下接口别改")
    assert any("接口" in it["message"] for it in front["inbox"])
    assert tools["inbox"] == []
