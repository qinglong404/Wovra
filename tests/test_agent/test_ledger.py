"""Agent 运行时测试（自 test_agent.py 拆分，2026-09-11）。

本模块：test_ledger。"""

from wovra import registry as registry_module
from wovra import task as task_module
from wovra.agent import Agent
from wovra.task import Task

# 共享夹具/工具（_helpers.py 是原文件的公共头部）
from ._helpers import *  # noqa: F401,F403

def test_verify_stage_records_checkpoint_without_cutting_round(monkeypatch, tmp_path):
    """阶段验收 = **轮内检查点**，不切轮（2026-09-12 用户口径，worklog §52/§53）。

    旧行为：验收 = 轮边界（闭合当前轮 + 开新轮续写），造出没有用户意图的人造轮，
    并让步数跨轮累计。新口径：轮 = 一次用户输入 → 最终回答，永不为运行时事件
    分割；验收只留一个带编号与锚点的检查点。
    """
    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path)
    task = Task.create(goal="g")
    agent = Agent(llm=_StubLLM(), tools=[], task=task)
    agent._open_or_reuse_round("干活")
    # 真实路径的形态：轮的 events 里已有 user 与这次 verify 的 tool_call，
    # 检查点的 event 锚点取自"当前轮最后一条事件"
    agent._record_event("user", {"role": "user", "content": "干活"})
    agent._record_event("tool_call", {
        "role": "assistant", "content": "",
        "tool_calls": [{"id": "c1", "type": "function",
                        "function": {"name": "todo",
                                     "arguments": '{"action":"verify_stage"}'}}],
    })
    agent.todo(action="start_stage", goal="阶段一", acceptance=["可跑"])
    agent.todo(action="add_item", text="搭起可运行骨架")
    agent.todo(action="check_item", text="搭起可运行骨架")
    out = agent.todo(action="verify_stage", evidence="测试全绿")

    assert len(agent.rounds) == 1                      # ★ 不切轮
    assert agent.current_round is agent.rounds[0]
    assert agent.rounds[0]["end_state"] == "open"      # 轮继续
    assert "不因验收分割" in out
    assert agent.task.todo["milestone"] is None
    # 验收证据进了 TaskState 账本（词表：阶段）
    assert any("阶段一" in c for c in task.get_state().completed)
    assert task.get_state().completed[-1].startswith("[阶段]")
    # 轮内检查点：带 M 编号 + round/event 锚点（block 待轮闭合回填）
    cp = agent.rounds[0]["checkpoints"][0]
    assert cp["id"] == "M1" and cp["round"] == 1
    assert cp["event"].startswith("R1-E")
    assert cp["block"] == ""
    # 账本历史同样带编号与锚点
    hist = task.todo["history"][0]
    assert hist["id"] == "M1" and hist["anchor"]["round"] == 1

    # 轮闭合：块切分完成后回填 block 锚点（阶段"在哪一轮、哪一块完成"可查）
    agent._record_event("final_answer", {"role": "assistant", "content": "收工"})
    agent.close_round()
    cp = agent.rounds[0]["checkpoints"][0]
    assert cp["block"].startswith("R1-B")
    assert task.todo["history"][0]["anchor"]["block"] == cp["block"]


def test_stage_actions_accept_legacy_aliases(monkeypatch, tmp_path):
    """旧动作名（start_milestone/add_step/verify_milestone…）保留为别名。

    在飞会话或旧提示词回显时仍能执行；schema 只广告新名（阶段/工作项）。
    """
    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path)
    task = Task.create(goal="g")
    agent = Agent(llm=_StubLLM(), tools=[], task=task)
    agent._open_or_reuse_round("干活")

    assert "OK" in agent.todo(action="start_milestone", goal="甲", acceptance=["可跑"])
    assert task.todo["milestone"]["id"] == "M1"
    assert "OK" in agent.todo(action="add_step", text="工作项一")
    assert "OK" in agent.todo(action="check_step", text="工作项一")
    assert "阶段" in agent.todo(action="verify_milestone", evidence="测试全绿")


def test_submit_organization_guard_is_noop_in_work_dialog():
    """工作对话误调用提交工具：只返回说明文本，无副作用。"""
    task = Task.create(goal="x")
    agent = Agent(llm=_StubLLM(), tools=[], task=task)

    out = agent.submit_organization(rounds=[{"seq": 1}], state_patch={"is_done": True})
    out2 = agent.submit_domains(domains=[], split_assessment={"splittable": True})

    assert "忽略" in out and "忽略" in out2
    assert task.task_state == {}
    assert all("pending_org" not in r for r in task.rounds)


def test_todo_stage_item_lifecycle(monkeypatch, tmp_path):
    """阶段/工作项账本：深度恒 1、结构闸门、证据闸门、非阻塞人工验收不搁置。"""
    task = Task.create(goal="演示页")
    agent = Agent(llm=_StubLLM(), tools=[], task=task)

    # 深度恒 1：无阶段时 verify/drop 拒绝；开阶段必须带验收标准
    assert "无进行中的阶段" in agent.todo(action="verify_stage", evidence="x")
    assert "acceptance" in agent.todo(action="start_stage", goal="多会话版")

    out = agent.todo(
        action="start_stage", goal="多会话版",
        acceptance=["刷新后会话保留", "删除当前会话回落"],
    )
    assert "多会话版" in out
    # 已有进行中的阶段 → 拒绝再开（深度恒 1）
    assert "深度恒 1" in agent.todo(action="start_stage", goal="另一个")

    # 结构闸门：从未拆过工作项 → verify 拒绝（阶段是推进增量，阶段内必须拆）
    out = agent.todo(action="verify_stage", evidence="测试全绿")
    assert "还没有拆过工作项" in out
    assert task.todo["milestone"]["goal"] == "多会话版"  # 阶段仍在进行

    assert "OK" in agent.todo(action="add_item", text="会话数据结构")
    assert "OK" in agent.todo(action="check_item", text="会话数据结构")
    assert "OK" in agent.todo(action="add_item", text="切换/删除交互")

    # 结构闸门：带未完成工作项 → verify 拒绝并列出未完成项
    out = agent.todo(action="verify_stage", evidence="测试全绿")
    assert "未完成工作项" in out and "切换/删除交互" in out
    assert "OK" in agent.todo(action="check_item", text="切换/删除交互")

    # 非阻塞人工验收：挂起继续干，verify 时一次性呈交并转 experiments
    out = agent.todo(action="defer_check", text="浅色主题配色是否刺眼（主观，最后统一验收）")
    assert "挂起" in out
    out = agent.todo(
        action="verify_stage",
        evidence="node tests/run.js 全绿（5 套件）",
    )
    assert "已验收" in out and "待办实验" in out
    assert task.get_state().completed[-1].startswith("[阶段] 多会话版")
    assert any("浅色主题配色" in e for e in task.get_state().experiments)
    # 关阶段即清：工作项与挂起项清空，可以开下一阶段
    out = agent.todo(action="start_stage", goal="搜索功能",
                     acceptance=["关键词命中高亮"])
    assert "OK" in out
    assert task.todo["milestone"]["goal"] == "搜索功能"
    assert task.todo["steps"] == []
    assert task.todo["milestone"]["planned"] is False   # 新阶段待拆

    # 证据闸门：无证据 verify 拒绝（在结构闸门之前判定）
    agent.todo(action="add_item", text="高亮渲染")
    assert "evidence" in agent.todo(action="verify_stage")

    # 结构闸门不误伤：工作项全部完成后（steps 清空）planned 仍为真 → 可验收
    agent.todo(action="check_item", text="高亮渲染")
    agent.todo(action="drop_item", text="高亮渲染")  # 例：该条被并入别处
    out = agent.todo(action="verify_stage", evidence="演示通过")
    assert "已验收" in out


def test_verify_gate_requires_item_planning(monkeypatch, tmp_path):
    """结构闸门（2026-09-11 用户拍板）：阶段 = 可验收增量、工作项 = 阶段内拆解
    ——跳过拆解直接验收是普通 todo 的用法，必须被拦。

    E/F 组实测 8 个阶段 0 次 add_item（模型跳过拆解闷头做），闸门即为此设。
    """
    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path)
    task = Task.create(goal="g")
    agent = Agent(llm=_StubLLM(), tools=[], task=task)
    agent._open_or_reuse_round("干活")
    agent.todo(action="start_stage", goal="阶段一", acceptance=["可跑"])

    # 未拆工作项：拒绝，且不产生 completed 记录、不切轮
    out = agent.todo(action="verify_stage", evidence="自测通过")
    assert "还没有拆过工作项" in out
    assert not any("阶段一" in c for c in task.get_state().completed)
    assert agent.rounds[-1]["end_state"] == "open"

    # 拆一条即可验收（哪怕就一条——账本要能说明这个阶段做了什么）
    agent.todo(action="add_item", text="唯一工作项")
    agent.todo(action="check_item", text="唯一工作项")
    out = agent.todo(action="verify_stage", evidence="自测通过")
    assert "已验收" in out
    assert any("阶段一" in c for c in task.get_state().completed)


def test_todo_tail_lines_shown_in_reminder(monkeypatch):
    """当前阶段/工作项进 runtime-reminder 尾部——跨轮续跑的工作记忆。"""
    task = Task.create(goal="分层回归")
    agent = Agent(llm=_StubLLM(), tools=[], task=task)
    agent.todo(action="start_stage", goal="ICP 配准", acceptance=["误差 < 1px"])
    agent.todo(action="add_item", text="读取标定参数")
    _make_open_round(agent, 1, "继续")

    msgs = agent._assemble_messages()
    body = "\n".join(m.get("content", "") for m in msgs)

    assert "[当前阶段] M1 ICP 配准（工作项 0/1）" in body


def test_todo_tail_nudges_item_planning(monkeypatch):
    """未拆工作项时尾部带待办提醒——把"被拒"变成"可操作"，避免验收才发现。"""
    task = Task.create(goal="g")
    agent = Agent(llm=_StubLLM(), tools=[], task=task)
    agent.todo(action="start_stage", goal="配准", acceptance=["误差 < 1px"])
    _make_open_round(agent, 1, "继续")

    body = "\n".join(m.get("content", "") for m in agent._assemble_messages())
    assert "尚未拆工作项" in body
    # 拆过后提醒消失
    agent.todo(action="add_item", text="读标定")
    body2 = "\n".join(m.get("content", "") for m in agent._assemble_messages())
    assert "尚未拆工作项" not in body2


def test_start_stage_acceptance_cap_and_started_seq(monkeypatch, tmp_path):
    """阶段收窄约束：验收标准硬上限 3 条（F 组 4-6 条打包把轮拖大）；
    started_seq 落在当前轮（修 len(rounds)+1 在轮已开时偏一位的偏差）。"""
    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path)
    task = Task.create(goal="g")
    agent = Agent(llm=_StubLLM(), tools=[], task=task)
    agent._open_or_reuse_round("干活")

    out = agent.todo(
        action="start_stage", goal="打包件",
        acceptance=["a", "b", "c", "d"],
    )
    assert "硬上限" in out
    assert (task.todo or {}).get("milestone") is None

    out = agent.todo(
        action="start_stage", goal="收窄的阶段",
        acceptance=["a", "b", "c"],
    )
    assert "OK" in out
    assert task.todo["milestone"]["started_seq"] == 1  # 当前轮 R1
    assert task.todo["milestone"]["id"] == "M1"        # 阶段编号


def test_stage_map_lines_rounds_to_stages(monkeypatch, tmp_path):
    """轮 ↔ 阶段映射：verify 历史带 started_seq 精确成段；缺 started_seq
    的旧数据按"上一阶段闭合轮 +1"推断；进行中阶段标进行中；无阶段记录
    返回空列表（不注入指令）。阶段带 M 编号（§53 用户口径：要编号 + 锚点）。"""
    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path)
    task = Task.create(goal="g")
    agent = Agent(llm=_StubLLM(), tools=[], task=task)
    agent._open_or_reuse_round("起步")
    agent.todo(action="start_stage", goal="阶段甲", acceptance=["可跑"])
    agent.todo(action="add_item", text="甲-1")
    agent.todo(action="check_item", text="甲-1")
    agent.todo(action="verify_stage", evidence="测试全绿")   # 记在 R1 检查点
    agent.close_round()
    agent._open_or_reuse_round("继续")
    agent.todo(action="start_stage", goal="阶段乙", acceptance=["可点"])
    agent.todo(action="add_item", text="乙-1")
    agent.todo(action="check_item", text="乙-1")
    agent.todo(action="verify_stage", evidence="演示通过")   # 记在 R2 检查点
    agent.close_round()
    agent._open_or_reuse_round("再继续")
    agent.todo(action="start_stage", goal="阶段丙", acceptance=["好看"])

    lines = agent._milestone_map_lines(agent.rounds)
    assert "  R1 ← 阶段『M1 阶段甲』（已验收）" in lines
    assert "  R2 ← 阶段『M2 阶段乙』（已验收）" in lines
    assert "  R3 ← 阶段『M3 阶段丙』（进行中）" in lines

    # 旧数据兼容：无 started_seq 时按上一阶段闭合轮 +1 推断，区间不重叠
    for e in task.todo["history"]:
        e.pop("started_seq", None)
    lines2 = agent._milestone_map_lines(agent.rounds)
    assert "  R1 ← 阶段『M1 阶段甲』（已验收）" in lines2
    assert "  R2 ← 阶段『M2 阶段乙』（已验收）" in lines2
    assert "  R3 ← 阶段『M3 阶段丙』（进行中）" in lines2

    # 无阶段记录：返回空列表，指令不注入映射段
    agent2 = Agent(llm=_StubLLM(), tools=[], task=Task.create(goal="g2"))
    assert agent2._milestone_map_lines([{"seq": 1}]) == []


def test_registry_default_and_comm_guards():
    """注册表默认主 agent；自咨询拒绝；未知 agent 列出现存条目。"""
    task = Task.create(goal="x")
    assert task.registry[0]["id"] == registry_module.MAIN_AGENT_ID
    agent = Agent(llm=_StubLLM(), tools=[], task=task)
    assert "不要 consult 主 agent" in agent.consult(agent="Main", question="?")
    assert "未找到 agent：Z" in agent.notify(agent="Z", message="m")
    assert "Main(主agent)" in agent.notify(agent="Z", message="m")


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
