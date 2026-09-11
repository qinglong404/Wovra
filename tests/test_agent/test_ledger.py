"""Agent 运行时测试（自 test_agent.py 拆分，2026-09-11）。

本模块：test_ledger。"""

from wovra import task as task_module
from wovra.agent import Agent
from wovra.task import Task

# 共享夹具/工具（_helpers.py 是原文件的公共头部）
from ._helpers import *  # noqa: F401,F403

def test_verify_milestone_closes_round_and_opens_checkpoint(monkeypatch, tmp_path):
    """里程碑驱动轮：verify_milestone = 检查点 = 轮边界——闭合当前轮、
    开新轮续写同一回合（新轮 user_input 带运行时说明）。"""
    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path)
    task = Task.create(goal="g")
    agent = Agent(llm=_StubLLM(), tools=[], task=task)
    agent._open_or_reuse_round("干活")
    agent.todo(action="start_milestone", goal="大步一", acceptance=["可跑"])
    agent.todo(action="add_step", text="搭起可运行骨架")
    agent.todo(action="check_step", text="搭起可运行骨架")
    agent.todo(action="verify_milestone", evidence="测试全绿")

    assert len(agent.rounds) == 2
    assert agent.rounds[0]["end_state"] == "completed"
    assert agent.current_round is agent.rounds[1]
    assert agent.rounds[1]["end_state"] == "open"
    assert "大步验收通过" in agent.rounds[1]["user_input"]["original"]
    assert agent.task.todo["milestone"] is None
    # 验收证据进了 TaskState 账本
    assert any("大步一" in c for c in task.get_state().completed)


def test_submit_organization_guard_is_noop_in_work_dialog():
    """工作对话误调用提交工具：只返回说明文本，无副作用。"""
    task = Task.create(goal="x")
    agent = Agent(llm=_StubLLM(), tools=[], task=task)

    out = agent.submit_organization(rounds=[{"seq": 1}], state_patch={"is_done": True})
    out2 = agent.submit_domains(domains=[], split_assessment={"splittable": True})

    assert "忽略" in out and "忽略" in out2
    assert task.task_state == {}
    assert all("pending_org" not in r for r in task.rounds)


def test_todo_milestone_lifecycle(monkeypatch, tmp_path):
    """大步/小步账本：深度恒 1、结构闸门、证据闸门、非阻塞人工验收不搁置。"""
    task = Task.create(goal="演示页")
    agent = Agent(llm=_StubLLM(), tools=[], task=task)

    # 深度恒 1：无大步时 verify/drop 拒绝；开大步必须带验收标准
    assert "无开启中的大步" in agent.todo(action="verify_milestone", evidence="x")
    assert "acceptance" in agent.todo(action="start_milestone", goal="多会话版")

    out = agent.todo(
        action="start_milestone", goal="多会话版",
        acceptance=["刷新后会话保留", "删除当前会话回落"],
    )
    assert "多会话版" in out
    # 已有开启中的大步 → 拒绝再开（深度恒 1）
    assert "深度恒 1" in agent.todo(action="start_milestone", goal="另一个")

    # 结构闸门：从未拆过小步 → verify 拒绝（大步是阶段，阶段内必须拆）
    out = agent.todo(action="verify_milestone", evidence="测试全绿")
    assert "还没有拆过小步" in out
    assert task.todo["milestone"]["goal"] == "多会话版"  # 大步仍在开

    assert "OK" in agent.todo(action="add_step", text="会话数据结构")
    assert "OK" in agent.todo(action="check_step", text="会话数据结构")
    assert "OK" in agent.todo(action="add_step", text="切换/删除交互")

    # 结构闸门：带未完成小步 → verify 拒绝并列出未完成项
    out = agent.todo(action="verify_milestone", evidence="测试全绿")
    assert "未完成小步" in out and "切换/删除交互" in out
    assert "OK" in agent.todo(action="check_step", text="切换/删除交互")

    # 非阻塞人工验收：挂起继续干，verify 时一次性呈交并转 experiments
    out = agent.todo(action="defer_check", text="浅色主题配色是否刺眼（主观，最后统一验收）")
    assert "挂起" in out
    out = agent.todo(
        action="verify_milestone",
        evidence="node tests/run.js 全绿（5 套件）",
    )
    assert "大步已验收" in out and "待办实验" in out
    assert task.get_state().completed[-1].startswith("[大步] 多会话版")
    assert any("浅色主题配色" in e for e in task.get_state().experiments)
    # 关大步即清：小步与挂起项清空，可以开下一大步
    out = agent.todo(action="start_milestone", goal="搜索功能",
                     acceptance=["关键词命中高亮"])
    assert "OK" in out
    assert task.todo["milestone"]["goal"] == "搜索功能"
    assert task.todo["steps"] == []
    assert task.todo["milestone"]["planned"] is False   # 新大步待拆

    # 证据闸门：无证据 verify 拒绝（在结构闸门之前判定）
    agent.todo(action="add_step", text="高亮渲染")
    assert "evidence" in agent.todo(action="verify_milestone")

    # 结构闸门不误伤：小步全部完成后（steps 清空）planned 仍为真 → 可验收
    agent.todo(action="check_step", text="高亮渲染")
    agent.todo(action="drop_step", text="高亮渲染")  # 例：该步被并入别处
    out = agent.todo(action="verify_milestone", evidence="演示通过")
    assert "大步已验收" in out


def test_verify_gate_requires_step_planning(monkeypatch, tmp_path):
    """结构闸门（2026-09-11 用户拍板）：大步 = 阶段、小步 = 阶段内拆解
    ——跳过拆解直接验收是普通 todo 的用法，必须被拦。

    E/F 组实测 8 个阶段 0 次 add_step（模型跳过拆解闷头做），闸门即为此设。
    """
    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path)
    task = Task.create(goal="g")
    agent = Agent(llm=_StubLLM(), tools=[], task=task)
    agent._open_or_reuse_round("干活")
    agent.todo(action="start_milestone", goal="阶段一", acceptance=["可跑"])

    # 未拆小步：拒绝，且不产生 completed 记录、不闭合轮
    out = agent.todo(action="verify_milestone", evidence="自测通过")
    assert "还没有拆过小步" in out
    assert not any("阶段一" in c for c in task.get_state().completed)
    assert agent.rounds[-1]["end_state"] == "open"

    # 拆一条即可验收（哪怕就一条——账本要能说明这个阶段做了什么）
    agent.todo(action="add_step", text="唯一工作项")
    agent.todo(action="check_step", text="唯一工作项")
    out = agent.todo(action="verify_milestone", evidence="自测通过")
    assert "大步已验收" in out
    assert any("阶段一" in c for c in task.get_state().completed)


def test_todo_tail_lines_shown_in_reminder(monkeypatch):
    """当前大步/小步进进 runtime-reminder 尾部——跨轮续跑的工作记忆。"""
    task = Task.create(goal="分层回归")
    agent = Agent(llm=_StubLLM(), tools=[], task=task)
    agent.todo(action="start_milestone", goal="ICP 配准", acceptance=["误差 < 1px"])
    agent.todo(action="add_step", text="读取标定参数")
    _make_open_round(agent, 1, "继续")

    msgs = agent._assemble_messages()
    body = "\n".join(m.get("content", "") for m in msgs)

    assert "[当前大步] ICP 配准（小步 0/1）" in body


def test_todo_tail_nudges_step_planning(monkeypatch):
    """未拆小步时尾部带待办提醒——把"被拒"变成"可操作"，避免验收才发现。"""
    task = Task.create(goal="g")
    agent = Agent(llm=_StubLLM(), tools=[], task=task)
    agent.todo(action="start_milestone", goal="配准", acceptance=["误差 < 1px"])
    _make_open_round(agent, 1, "继续")

    body = "\n".join(m.get("content", "") for m in agent._assemble_messages())
    assert "尚未拆小步" in body
    # 拆过后提醒消失
    agent.todo(action="add_step", text="读标定")
    body2 = "\n".join(m.get("content", "") for m in agent._assemble_messages())
    assert "尚未拆小步" not in body2


def test_start_milestone_acceptance_cap_and_started_seq(monkeypatch, tmp_path):
    """大步收窄约束：验收标准硬上限 3 条（F 组 4-6 条打包把轮拖大）；
    started_seq 落在当前轮（修 len(rounds)+1 在轮已开时偏一位的偏差）。"""
    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path)
    task = Task.create(goal="g")
    agent = Agent(llm=_StubLLM(), tools=[], task=task)
    agent._open_or_reuse_round("干活")

    out = agent.todo(
        action="start_milestone", goal="打包件",
        acceptance=["a", "b", "c", "d"],
    )
    assert "硬上限" in out
    assert (task.todo or {}).get("milestone") is None

    out = agent.todo(
        action="start_milestone", goal="收窄的大步",
        acceptance=["a", "b", "c"],
    )
    assert "OK" in out
    assert task.todo["milestone"]["started_seq"] == 1  # 当前轮 R1


def test_milestone_map_lines_rounds_to_milestones(monkeypatch, tmp_path):
    """轮 ↔ 大步映射：verify 历史带 started_seq 精确成段；缺 started_seq
    的旧数据按"上一个大步闭合轮 +1"推断；在飞大步标进行中；无大步记录
    返回空列表（不注入指令）。"""
    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path)
    task = Task.create(goal="g")
    agent = Agent(llm=_StubLLM(), tools=[], task=task)
    agent._open_or_reuse_round("起步")
    agent.todo(action="start_milestone", goal="大步甲", acceptance=["可跑"])
    agent.todo(action="add_step", text="甲-1")
    agent.todo(action="check_step", text="甲-1")
    agent.todo(action="verify_milestone", evidence="测试全绿")  # 闭合 R1
    agent.todo(action="start_milestone", goal="大步乙", acceptance=["可点"])
    agent.todo(action="add_step", text="乙-1")
    agent.todo(action="check_step", text="乙-1")
    agent.todo(action="verify_milestone", evidence="演示通过")  # 闭合 R2
    agent.todo(action="start_milestone", goal="大步丙", acceptance=["好看"])

    lines = agent._milestone_map_lines(agent.rounds)
    assert f"  R1 ← 大步『大步甲』（已验收）" in lines
    assert f"  R2 ← 大步『大步乙』（已验收）" in lines
    assert f"  R3 ← 大步『大步丙』（进行中）" in lines

    # 旧数据兼容：无 started_seq 时按上一个大步闭合轮 +1 推断，区间不重叠
    for e in task.todo["history"]:
        e.pop("started_seq", None)
    lines2 = agent._milestone_map_lines(agent.rounds)
    assert f"  R1 ← 大步『大步甲』（已验收）" in lines2
    assert f"  R2 ← 大步『大步乙』（已验收）" in lines2
    assert f"  R3 ← 大步『大步丙』（进行中）" in lines2

    # 无大步记录：返回空列表，指令不注入映射段
    agent2 = Agent(llm=_StubLLM(), tools=[], task=Task.create(goal="g2"))
    assert agent2._milestone_map_lines([{"seq": 1}]) == []


def test_registry_default_and_comm_guards():
    """注册表默认主 agent；自咨询拒绝；未知 agent 列出现存条目。"""
    task = Task.create(goal="x")
    assert task.registry[0]["id"] == "A"
    agent = Agent(llm=_StubLLM(), tools=[], task=task)
    assert "不要 consult 主 agent" in agent.consult(agent="A", question="?")
    assert "未找到 agent：Z" in agent.notify(agent="Z", message="m")
    assert "A(主agent)" in agent.notify(agent="Z", message="m")


def test_notify_and_consult_direct_to_user():
    """单向 notify 落收件箱；双向 consult 以目标视角回答、回复打标签
    直达用户窗口并返回调用方；收件箱随激活送达。"""
    collected = []
    task = Task.create(goal="演示项目")
    task.registry.append({
        "id": "B", "name": "前端agent",
        "description": "负责 index.html 与 css/ 界面层",
        "file_domains": ["index.html", "css/"],
        "status": "dormant", "inbox": [],
    })
    consult_resp = [_chunk(_delta(content="界面层归我，按钮色值用 #0a84ff"))]
    agent = Agent(llm=_StubLLM(consult_responses=[consult_resp]), tools=[], task=task)
    agent._stream_cbs = {"thinking": None, "answer": collected.append}

    out = agent.notify(agent="B", message="接口定了：getStats() 返回 {clicks}")
    assert "已单向送达" in out
    assert task.registry[1]["inbox"][0]["message"].startswith("接口定了")

    reply = agent.consult(agent="前端agent", question="UI 改动走谁的文件域？")
    assert "前端agent 的回复" in reply and "#0a84ff" in reply
    assert task.registry[1]["inbox"] == []              # 收件箱随激活送达
    assert collected[0] == "\n[前端agent] "             # 打标签直达用户窗口
    assert any("#0a84ff" in c for c in collected)
