"""Task 的持久化与视图测试。不联网、不依赖 .env。"""

import json
import os
import threading
import time

import pytest

from wovra import task as task_module
from wovra.task import Task


def _use_tmp_root(monkeypatch, tmp_path):
    """把任务存储根目录指到 pytest 的临时目录，避免污染真实 tasks/。"""
    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path)


def test_load_migrates_legacy_agent_ids_exactly_once(monkeypatch, tmp_path):
    """加载期把 v1 的 Agent ID 迁到 v2（主 agent `A` → `Main`），且**只迁一次**。

    v1：主 agent 占 `A`，第一次分裂的顶层域只能叫 `A-1`；v2：主 agent = `Main`，
    顶层域取 `A`、`B`、`C`…。迁移**非幂等**（v2 里 `A-1` 已是"域 A 的子域 1"），
    故靠 `agent_id_scheme` 把住——第二次加载必须原样，否则子域会被读成顶层域。
    """
    _use_tmp_root(monkeypatch, tmp_path)
    task = Task.create(goal="g")
    task.rounds = [{
        "seq": 1, "user_input": {"original": "写工具", "normalized": ""},
        "events": [], "end_state": "completed", "org_state": "done",
        "active_view": "A",                       # v1 的主 agent 哨兵
        "domains": [{"name": "工具层", "file_domains": ["a.py"]}],
    }]
    task.registry = [
        {"id": "A", "name": "主agent", "status": "active", "inbox": []},
        {"id": "A-1", "name": "工具层", "status": "dormant", "inbox": []},
        {"id": "A-1-1", "name": "子域", "status": "dormant", "inbox": []},
    ]
    task.agent_id_scheme = 1                      # 老数据（缺省即 1）
    task.save()

    loaded = Task.load(task.id)
    assert [e["id"] for e in loaded.registry] == ["Main", "A", "A-1"]
    assert loaded.rounds[0]["active_view"] == "Main"
    assert loaded.agent_id_scheme == 2
    assert any(
        "agent ID 体系迁移" in str(h.get("detail"))
        for h in loaded.history if h.get("kind") == "maintenance"
    )

    # 二次加载：scheme 已是 2 → 一字不动（`A-1` 不许再被读成顶层域）
    again = Task.load(task.id)
    assert [e["id"] for e in again.registry] == ["Main", "A", "A-1"]
    assert again.agent_id_scheme == 2


def test_create_and_save_load_roundtrip(monkeypatch, tmp_path):
    _use_tmp_root(monkeypatch, tmp_path)

    task = Task.create(
        goal="测试目标",
        requirements=["约束一"],
    )
    task.record("user_input", "你好")
    task.summary = "已完成一半"  # 字段保留（标废），但 set_summary() 已删
    task.save()

    # 落盘产生两个文件：结构化状态 + 人类可读报告
    assert (tmp_path / task.id / "task.json").exists()
    assert (tmp_path / task.id / "report.md").exists()

    loaded = Task.load(task.id)
    assert loaded.goal == "测试目标"
    assert loaded.requirements == ["约束一"]
    assert loaded.summary == "已完成一半"
    assert loaded.history[-1]["kind"] == "user_input"
    assert loaded.history[-1]["detail"] == "你好"


def test_task_json_is_human_readable(monkeypatch, tmp_path):
    _use_tmp_root(monkeypatch, tmp_path)
    task = Task.create(goal="可读性检查")
    task.save()

    raw = json.loads((tmp_path / task.id / "task.json").read_text(encoding="utf-8"))
    # ensure_ascii=False：中文直接可读，而不是 \uXXXX 转义
    assert raw["goal"] == "可读性检查"


def test_load_or_create_resumes(monkeypatch, tmp_path):
    _use_tmp_root(monkeypatch, tmp_path)

    first = Task.load_or_create("fixed-id", goal="同一目标")
    first.record("user_input", "第一次的输入")
    first.save()

    # 模拟新进程：再次 load_or_create 拿到的应是同一份持久状态
    second = Task.load_or_create("fixed-id", goal="同一目标")
    assert second.id == first.id
    assert any(e["detail"] == "第一次的输入" for e in second.history)


def test_report_renders_goal_and_summary(monkeypatch, tmp_path):
    """report.md 渲染目标与进展摘要。

    `acceptance_criteria` 与 `set_summary()` 已随 2026-09-11 遗产整治删除
    （实测 0/77 数据、0 消费者）；`summary` 字段标废保留，故这里直接赋值。
    """
    _use_tmp_root(monkeypatch, tmp_path)
    task = Task.create(goal="写报告的目标")
    task.summary = "进展摘要内容"
    task.save()

    report = (tmp_path / task.id / "report.md").read_text(encoding="utf-8")
    assert "写报告的目标" in report
    assert "进展摘要内容" in report


def test_report_merges_tool_call_and_result(monkeypatch, tmp_path):
    """相邻的调用+结果合并成一行：内容长则只报字数，出错才显示错误。"""
    _use_tmp_root(monkeypatch, tmp_path)
    task = Task.create(goal="合并检查")
    task.record("tool_call", 'read_file({"path":"a.py"})')
    task.record("tool_result", "read_file -> " + "内容" * 300)  # 很长的正常结果
    task.record("tool_call", "boom({})")
    task.record("tool_result", "boom -> 工具执行出错: ValueError()")
    task.save()

    timeline = (tmp_path / task.id / "report.md").read_text(
        encoding="utf-8"
    ).split("## 时间线")[1]

    assert timeline.count("- `") == 2  # 四条事件合并成两行
    assert "返回 600 字" in timeline
    assert "失败：工具执行出错" in timeline
    assert "内容" * 10 not in timeline  # 长内容原文不出现


def test_report_history_stays_one_line_per_event(monkeypatch, tmp_path):
    """多行/超长的 detail 在报告里必须压成一行，不能冲垮 Markdown 结构。"""
    _use_tmp_root(monkeypatch, tmp_path)
    task = Task.create(goal="格式检查")
    task.record("tool_result", "第一行\n第二行\n\n    缩进的代码" + "x" * 500)
    task.record("final_answer", "多行回答\n\n1. 第一条\n2. 第二条")
    task.save()

    report = (tmp_path / task.id / "report.md").read_text(encoding="utf-8")
    timeline = report.split("## 时间线")[1]
    # 换行被折叠成空格、内容被截断，事件各自只占一行
    assert "第一行 第二行" in timeline
    assert "…" in timeline
    # 每条事件都是单个列表项（- 开头），没有从列表里漏出来的行
    event_lines = [l for l in timeline.splitlines() if l.startswith("- ")]
    assert len(event_lines) == 2


def test_report_renders_stage_and_items(monkeypatch, tmp_path):
    """report.md 必须让人看到阶段/工作项——计划账本平时只在模型上下文里。"""
    _use_tmp_root(monkeypatch, tmp_path)
    task = Task.create(goal="阶段化任务")
    task.todo = {
        "milestone": {
            "id": "M1",
            "goal": "骨架可跑",
            "acceptance": ["能移动", "能看见地形"],
            "started_seq": 2,
            "deferred": ["配色是否顺眼"],
            "planned": True,
        },
        "steps": [
            {"text": "渲染循环", "done": True},
            {"text": "键盘移动", "done": False},
        ],
    }
    task.save()

    report = (tmp_path / task.id / "report.md").read_text(encoding="utf-8")
    assert "## 计划（阶段 / 工作项）" in report
    assert "骨架可跑" in report and "工作项 1/2" in report
    assert "M1" in report                                  # 阶段编号可见
    assert "[x] 渲染循环" in report and "[ ] 键盘移动" in report
    assert "配色是否顺眼" in report


def test_todo_lines_explicit_when_no_stage():
    """无进行中阶段时人视图不留空——明说没有，并交代已验收历史。"""
    task = Task.create(goal="空计划")
    assert task.todo_lines() == ["-（无进行中的阶段）"]

    task.todo = {"history": [{"goal": "第一步"}, {"goal": "第二步"}]}
    lines = task.todo_lines()
    assert lines[0] == "-（无进行中的阶段）"
    assert "已验收 2 个阶段" in lines[1] and "第二步" in lines[1]


def test_todo_lines_flags_unplanned_stage():
    """结构闸门的提醒也要出现在人视图里：开了阶段却还没拆工作项。"""
    task = Task.create(goal="x")
    task.todo = {
        "milestone": {"goal": "还没拆", "acceptance": [], "planned": False},
        "steps": [],
    }
    assert any("尚未拆工作项" in l for l in task.todo_lines())


def test_todo_summary_line_renders_stage_and_progress():
    """底栏单行摘要：阶段 + 工作项进度 + 待人工验收，且必须单行。"""
    task = Task.create(goal="x")
    task.todo = {
        "milestone": {
            "id": "M2",
            "goal": "骨架可跑",
            "acceptance": ["能移动"],
            "started_seq": 1,
            "deferred": ["配色是否顺眼"],
            "planned": True,
        },
        "steps": [{"text": "a", "done": True}, {"text": "b", "done": False}],
    }
    line = task.todo_summary_line()
    assert "阶段 M2 骨架可跑" in line and "工作项 1/2" in line and "待验收 1" in line
    assert "\n" not in line


def test_todo_summary_line_flags_unplanned_and_empty():
    task = Task.create(goal="x")
    task.todo = {"milestone": {"goal": "还没拆", "planned": False}, "steps": []}
    assert "未拆工作项" in task.todo_summary_line()

    task.todo = {}
    assert task.todo_summary_line() == "无进行中的阶段"

    task.todo = {"history": [{"goal": "一"}, {"goal": "二"}]}
    assert "已验收 2 个" in task.todo_summary_line()


def test_context_includes_goal_summary_and_recent_history(monkeypatch, tmp_path):
    _use_tmp_root(monkeypatch, tmp_path)
    task = Task.create(goal="给模型看的目标")
    task.record("user_input", "早期事件")
    task.summary = "模型该知道的进展"
    task.record("tool_call", "get_current_time({})")

    context = task.context()
    assert "给模型看的目标" in context
    assert "模型该知道的进展" in context
    assert "tool_call" in context


def test_empty_task_state_renders_nothing():
    """新会话的空状态渲染为空串——不给模型一个空的 [任务状态] 头。"""
    from wovra.task import TaskState

    assert TaskState().render() == ""
    # 有一点内容才出现标题
    assert "[任务状态]" in TaskState(goal="有目标").render()


def test_state_render_respects_budget():
    """回归（2026-09-11 机制评审）：render 的 budget 必须真的裁剪。

    TaskState 的 7 个列表各有 200 条上限（STATE_LIST_CAP），理论最坏
    1400 条，且状态是**每轮都进上下文**的（信封尾部）——不设预算就是
    一条无界常驻负担。render 早就支持 budget，但装配处此前没传，
    裁剪保护形同虚设。

    2026-09-11 P0 修正口径：裁剪从「整块尾部切片」改为「按节分配」。
    旧口径在活会话上实测**只剩「已完成」一节**（决策升级 7 条、待办实验
    13 条全部不可见），且保最旧的取舍方向与 apply_patch 淘汰最旧相反。
    """
    from wovra import task as task_module
    from wovra.task import TaskState, STATE_LIST_CAP, _STATE_TRIM_NOTE

    state = TaskState(goal="目标")
    state.completed = [f"完成项{i}" for i in range(STATE_LIST_CAP)]
    full = state.render()
    assert "完成项0" in full and len(full) > 500

    cut = state.render(budget=200)
    assert "已完成" in cut                      # 节不消失
    assert "完成项199" in cut                   # 保最新（与 apply_patch 淘汰最旧同向）
    assert "完成项0" not in cut                 # 最旧条目被裁
    assert "条已省略，见 report" in cut          # 裁了多少条显式写出来
    assert cut.endswith(_STATE_TRIM_NOTE)       # 有节被裁才加总注

    # 没有超预算时不加总注（不会每轮都挂着一条噪声）
    small = TaskState(goal="小").render(8000)
    assert _STATE_TRIM_NOTE not in small

    # 装配处的预算常量存在且被默认使用
    from wovra.agent.support import _STATE_RENDER_BUDGET
    assert _STATE_RENDER_BUDGET > 0


def test_state_render_keeps_critical_sections_under_tight_budget():
    """P0 复现：紧预算下**任何一节都不能整节消失**，关键节优先。

    旧口径（整块 text[:budget]）在活会话上的实测结果：只剩「已完成」
    一节，决策升级 7 条与待办实验 13 条全被吃掉——而这两节是文档写明的
    人机协同一等公民、**没有第二个注入点**，模型永远读不到「需要人拍板」
    的事。这条用例把那个形状钉死。
    """
    from wovra.task import TaskState

    state = TaskState(goal="目标")
    state.completed = [f"完成项{i}" for i in range(200)]
    state.decisions = [f"决策{i}" for i in range(20)]
    state.known_issues = [f"问题{i}" for i in range(15)]
    state.open_questions = [f"待解决{i}" for i in range(6)]
    state.escalations = [f"升级{i}" for i in range(7)]
    state.experiments = [f"实验{i}" for i in range(11)]
    state.constraints = [f"约束{i}" for i in range(3)]

    # 预算故意压到只够关键节 + 每节至少一条
    cut = state.render(budget=600)
    for label in ("已完成", "已决策", "已知问题", "待解决问题", "决策升级", "待办实验", "约束"):
        assert label in cut, f"{label} 整节消失——旧口径的病灶复发"
    # 关键节全量保留（它们体量小、且是人机协同的唯一出口）
    assert all(f"升级{i}" in cut for i in range(7))
    assert all(f"实验{i}" in cut for i in range(11))
    assert all(f"约束{i}" in cut for i in range(3))


def test_assembly_truncates_oversized_task_state(monkeypatch):
    """回归（2026-09-11）：装配上下文里的任务状态受预算约束，
    不会随列表增长无限膨胀；且裁剪时关键节不被吃掉。

    2026-09-11 二次修订：`completed` 已退出模型侧（task._MODEL_SIDE_SECTIONS），
    故改用仍在模型侧、同样会无限增长的 `decisions` 造超长状态；
    completed 的退出由 test_assembly_excludes_completed_from_model_side 钉住。
    """
    from wovra.agent import Agent
    from wovra.task import _STATE_TRIM_NOTE

    task = Task.create(goal="目标")
    task.task_state["decisions"] = [f"决策{i}" * 20 for i in range(300)]
    task.task_state["escalations"] = ["需要人拍板的事项"]
    task.task_state["experiments"] = ["需要人当传感器的事项"]
    agent = Agent(llm=object(), tools=[], task=task)
    agent.current_round = None
    agent.messages = []
    msgs = agent._assemble_messages()
    body = "\n".join(m.get("content", "") for m in msgs)
    assert _STATE_TRIM_NOTE in body
    # 关键节即使在被裁的装配里也必须可见（P0 病灶：旧实现只剩「已完成」）
    assert "需要人拍板的事项" in body
    assert "需要人当传感器的事项" in body


def test_assembly_excludes_completed_from_model_side(monkeypatch):
    """回归（2026-09-11 用户拍板）：`completed` 不注入模型侧，人侧全量。

    依据（实测）：completed 是唯一无限增长的节——活会话 49 条 / 19,582
    字符 / 14,005 tok，占全部条目文本 62.7%，内容几乎全是 verify_milestone
    写入的验收证据副本（与 worklog / report.md / events 流三重冗余）；
    而 R16 的按节预算早把它压到 143 tok，等于占 62.7% 存储只给一句片段。
    档案的读取时刻是"人想知道"，不是"每轮都要"——故 render 不传 sections
    时（人视图、report）照旧全量。
    """
    from wovra.agent import Agent

    task = Task.create(goal="目标")
    task.task_state["completed"] = ["[大步] 已验收的证据副本A", "[大步] 已验收的证据副本B"]
    task.task_state["escalations"] = ["需要人拍板的事项"]
    agent = Agent(llm=object(), tools=[], task=task)
    agent.current_round = None
    agent.messages = []
    body = "\n".join(m.get("content", "") for m in agent._assemble_messages())
    assert "已验收的证据副本A" not in body, "completed 仍在模型侧（每轮白付）"
    assert "需要人拍板的事项" in body
    # 人侧不传 sections：全量（report / 活文档要能看到已完成的档案）
    human = task.get_state().render()
    assert "已验收的证据副本A" in human
    assert "已验收的证据副本B" in human


def test_state_patch_closes_resolved_items():
    """回归（2026-09-11 用户拍板）：结案机制——模型给片段、机制唯一匹配后移除。

    依据（实测）：本会话 11 条 escalations 里 8 条已结案仍挂着（400 已修、
    幽灵误判已修、v1/v3 双轨已废……），模型每轮读它们、随时可能把做完的事
    重新拿来问一遍。分工纪律：语义判断归模型（读取时刻），机械匹配归机制。
    """
    task = Task.create(goal="g")
    task.apply_state_patch({
        "escalations": ["400 缺陷（需人拍板修法与时机）：预期是维护批次正常产出"],
        "experiments": ["重启后裸跑安全探针"],
    })
    report = task.apply_state_patch({
        "closed": [
            {"field": "escalations", "match": "400 缺陷（需人拍板修法与时机）"},
            {"field": "experiments", "match": "重启后裸跑安全探针"},
        ],
    })
    state = task.get_state()
    assert state.escalations == []
    assert state.experiments == []
    assert len(report["closed"]) == 2
    assert report["unmatched"] == []


def test_close_items_requires_unique_match_and_never_guesses():
    """结案匹配纪律：命中 0 条或多条一律不动、原样回报（宁可留化石，不可错删活账）。

    错删比滞留危险——滞留只是噪，误删会丢约束（决策/升升级队列丢一条，
    模型就永久少一条边界）。
    """
    from wovra.task import TaskState

    state = TaskState(goal="g")
    state.escalations = ["端口冲突：换端口还是杀进程", "端口冲突：另一个上下文"]
    _items, closed, unmatched = state.close_items("escalations", ["端口冲突"])
    assert closed == [] and unmatched == ["端口冲突"]  # 命中 2 条 → 不猜
    assert len(state.escalations) == 2
    _items, closed, unmatched = state.close_items("escalations", ["根本不存在的片段"])
    assert closed == [] and unmatched == ["根本不存在的片段"]  # 命中 0 条 → 不动
    _items, closed, unmatched = state.close_items("escalations", ["换端口还是杀进程"])
    assert len(closed) == 1  # 唯一命中 → 移除
    assert state.escalations == ["端口冲突：另一个上下文"]


def test_promote_records_close_report_in_history(monkeypatch, tmp_path):
    """结案必须留痕：条目从账本删除后，history 是唯一能回答"谁在哪次整理结掉的"的地方。"""
    from wovra.agent import Agent
    from wovra import task as task_module

    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path)
    task = Task.create(goal="g")
    task.apply_state_patch({"escalations": ["400 缺陷（需人拍板）：待定"]})
    agent = Agent(llm=object(), tools=[], task=task)
    agent.rounds = [{"seq": 1, "events": [], "pending_org": {
        "state_patch": {
            "closed": [{"field": "escalations", "match": "400 缺陷（需人拍板）"}],
            "current_status": "已修",
        },
    }}]
    agent._promote_org_results()
    state = task.get_state()
    assert state.escalations == []
    detail = " ".join(str(h.get("detail", "")) for h in task.history)
    assert "结案" in detail
    assert "400 缺陷（需人拍板）" in detail


def test_promote_records_unmatched_close_snippet(monkeypatch, tmp_path):
    """结案片段匹配不到时也留痕——它是提示词质量或片段表述的信号，不能静默丢。"""
    from wovra.agent import Agent
    from wovra import task as task_module

    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path)
    task = Task.create(goal="g")
    task.apply_state_patch({"escalations": ["真实的升级条目"]})
    agent = Agent(llm=object(), tools=[], task=task)
    agent.rounds = [{"seq": 1, "events": [], "pending_org": {
        "state_patch": {"closed": [{"field": "escalations", "match": "抄错的片段"}]},
    }}]
    agent._promote_org_results()
    assert task.get_state().escalations == ["真实的升级条目"]  # 不动活账
    detail = " ".join(str(h.get("detail", "")) for h in task.history)
    assert "未匹配" in detail


def test_state_patch_done_syncs_task_status():
    """整理判定 is_done=true → Task.status 同步为 done，两本账不打架。"""
    task = Task.create(goal="g")
    assert task.status == "in_progress"

    task.apply_state_patch({"is_done": True})

    assert task.get_state().is_done is True
    assert task.status == "done"


def test_save_sanitizes_lone_surrogates(monkeypatch, tmp_path):
    """模型偶发的不成对代理转义不能让落盘当场崩掉（实测 2026-09-05）。"""
    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path)
    task = Task.create(goal="g")
    task.history.append({"time": "t", "kind": "tool_result", "detail": "x\ud83d"})

    task.save()  # 修复前：UnicodeEncodeError 直接崩

    loaded = Task.load(task.id)
    assert "\ud83d" not in loaded.history[-1]["detail"]
    assert "\ufffd" in loaded.history[-1]["detail"]


def test_save_survives_concurrent_writers(monkeypatch, tmp_path):
    """并发保存不得撞车（2026-09-12 实测：固定 tmp 名 → WinError 5 崩会话）。

    真实触发场景：主线程的账本/转交保存与后台整理线程的保存同时发生——
    原实现的固定 `task.json.tmp` 被两边交叉写、或目标文件被另一方持句柄，
    os.replace 直接 PermissionError，异常穿透 CLI 把整个会话带走。
    """
    _use_tmp_root(monkeypatch, tmp_path)
    task = Task.create(goal="并发落盘")
    errors: list[BaseException] = []

    def worker(n: int) -> None:
        try:
            for i in range(20):
                task.record("maintenance", f"线程{n} 第{i}笔")
                task.save()
        except BaseException as error:  # noqa: BLE001——写进列表，别吞
            errors.append(error)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    raw = json.loads((tmp_path / task.id / "task.json").read_text(encoding="utf-8"))
    assert raw["id"] == task.id
    # 唯一 tmp 名 + 失败清理：目录里不留任何半截文件
    assert list((tmp_path / task.id).glob("*.tmp.*")) == []


def test_save_retries_transient_permission_error(monkeypatch, tmp_path):
    """os.replace 被外部句柄短暂占用（杀毒/索引器/查看器）→ 退避重试后成功。"""
    _use_tmp_root(monkeypatch, tmp_path)
    task = Task.create(goal="重试")
    real = os.replace
    seen: list[str] = []

    def flaky(src, dst):
        seen.append(str(dst))
        if len([p for p in seen if p.endswith("task.json")]) <= 2:
            raise PermissionError(13, "拒绝访问")
        return real(src, dst)

    monkeypatch.setattr(task_module.os, "replace", flaky)
    monkeypatch.setattr(task_module, "_SAVE_BACKOFF", 0.001)

    task.save()  # 修复前：第一次失败就崩

    # task.json 两次失败 + 一次成功；report.md 一次成功
    assert len([p for p in seen if p.endswith("task.json")]) == 3
    assert len([p for p in seen if p.endswith("report.md")]) == 1
    assert (tmp_path / task.id / "task.json").exists()


def test_save_gives_up_and_cleans_tmp(monkeypatch, tmp_path):
    """重试耗尽必须上抛（不假装保存成功），且不留半截 tmp。"""
    _use_tmp_root(monkeypatch, tmp_path)
    task = Task.create(goal="放弃")
    monkeypatch.setattr(task_module, "_SAVE_ATTEMPTS", 3)
    monkeypatch.setattr(task_module, "_SAVE_BACKOFF", 0.001)

    def always_denied(src, dst):
        raise PermissionError(13, "拒绝访问")

    monkeypatch.setattr(task_module.os, "replace", always_denied)

    with pytest.raises(PermissionError):
        task.save()

    assert list((tmp_path / task.id).glob("*.tmp.*")) == []


def test_report_md_failure_is_not_fatal(monkeypatch, tmp_path):
    """report.md 是派生物：被外部占用时不致命，task.json 照常落盘。"""
    _use_tmp_root(monkeypatch, tmp_path)
    task = Task.create(goal="人视图被占用")
    real = os.replace

    def only_report_denied(src, dst):
        if str(dst).endswith("report.md"):
            raise PermissionError(13, "拒绝访问")
        return real(src, dst)

    monkeypatch.setattr(task_module.os, "replace", only_report_denied)
    monkeypatch.setattr(task_module, "_SAVE_ATTEMPTS", 2)
    monkeypatch.setattr(task_module, "_SAVE_BACKOFF", 0.001)

    task.save()  # 不抛——派生物失败不该拖垮正在进行的会话

    raw = json.loads((tmp_path / task.id / "task.json").read_text(encoding="utf-8"))
    assert raw["goal"] == "人视图被占用"
    assert any("report.md 写入失败" in h["detail"] for h in task.history)


def test_stale_tmp_is_cleaned_but_fresh_one_kept(monkeypatch, tmp_path):
    """只清**够旧**的半截 tmp：新鲜的很可能是别人正在写的，动了就打坏它。"""
    _use_tmp_root(monkeypatch, tmp_path)
    task = Task.create(goal="清理")
    directory = tmp_path / task.id
    directory.mkdir(parents=True, exist_ok=True)
    old = directory / "task.json.tmp.999.1.1"
    fresh = directory / "task.json.tmp.999.1.2"
    old.write_text("半截", encoding="utf-8")
    fresh.write_text("正在写", encoding="utf-8")
    stale_time = time.time() - 3600
    os.utime(old, (stale_time, stale_time))

    task.save()

    assert not old.exists()
    assert fresh.exists()


def test_load_self_heals_legacy_v1_blocks(monkeypatch, tmp_path):
    """回归（2026-09-11）：加载旧会话时把 v1 粗分块自愈为 v3 并落盘。

    为什么必须自愈而不是"手动迁移"：活跃会话的进程内存里握着旧 rounds，
    手动改盘会被下一次 `_persist_rounds` 覆盖回去——只有重启后的新进程
    读盘时顺手治好才算真修。迁移是确定性、幂等的，故随加载进行是安全的。
    """
    import json as _json

    from wovra.task import Task

    _use_tmp_root(monkeypatch, tmp_path)
    task = Task.create(goal="g")
    task.rounds = [{
        "seq": 1,
        "user_input": {"original": "读两个文件", "normalized": ""},
        "events": [
            {"id": "R1-E01", "type": "user",
             "message": {"role": "user", "content": "读两个文件"}},
            {"id": "R1-E02", "type": "tool_call",
             "message": {"role": "assistant", "content": "", "tool_calls": [
                 {"id": "c1", "type": "function",
                  "function": {"name": "read_file",
                               "arguments": _json.dumps({"path": "a.py"})}}]}},
            {"id": "R1-E03", "type": "tool_result",
             "message": {"role": "tool", "tool_call_id": "c1", "content": "a 内容"}},
            {"id": "R1-E04", "type": "final_answer",
             "message": {"role": "assistant", "content": "读完"}},
        ],
        # 落盘的是 v1 粗分块（kind=work、无 events 键）
        "blocks": [{
            "id": "R1-B1", "kind": "work", "start": 0, "end": 3,
            "start_event": "R1-E01", "end_event": "R1-E04",
            "touched_files": ["a.py"], "wrote_files": [], "command_types": [],
        }],
        "end_state": "completed",
    }]
    task.save()

    loaded = Task.load(task.id)

    blk = loaded.rounds[0]["blocks"][0]
    assert blk["kind"] == "file"           # v3：按文件聚合
    assert blk["file"] == "a.py"
    assert "events" in blk                 # v3 块恒带 events 键
    # 留痕可查
    assert any(
        "历史块结构迁移" in e.get("detail", "")
        for e in loaded.history if e.get("kind") == "maintenance"
    )
    # 落盘生效（不是只改了内存对象）
    assert Task.load(task.id).rounds[0]["blocks"][0]["kind"] == "file"

    # 幂等：已是 v3 的会话再次加载不再改写、不再留痕
    history_before = len(Task.load(task.id).history)
    again = Task.load(task.id)
    assert len(again.history) == history_before


def test_task_binds_and_restores_workspace(monkeypatch, tmp_path):
    """会话绑定工作区：创建时记录，加载时恢复——从任何目录恢复都回原地。"""
    from wovra import tools as tools_module

    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path / "tasks")
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setattr(tools_module.safety, "PROJECT_ROOT", ws)

    task = Task.create(goal="g")
    assert task.workspace == str(ws)
    task.save()

    # 模拟从别处启动：PROJECT_ROOT 已变，但加载旧会话会恢复其绑定的工作区
    monkeypatch.setattr(tools_module.safety, "PROJECT_ROOT", tmp_path)
    loaded = Task.load(task.id)
    assert loaded.workspace == str(ws)
    assert str(tools_module.safety.PROJECT_ROOT) == str(ws)


def test_apply_state_patch_syncs_goal_to_task(monkeypatch, tmp_path):
    """回归（2026-09-11 遗产整治）：TaskState.goal 变化同步到 Task.goal。

    两处 goal 长期分裂：`Task.goal` 是 V1 时代"建任务时定死"的字段（人视图
    显示它——`wovra list`、report head、启动横幅），`TaskState.goal` 是整理
    产出的权威当前目标（模型侧 render 用它）。实测活会话前者为空、后者写满
    了整段真实目标，于是**终端与报告一直显示"（目标待明确）"，模型侧却看得
    到完整目标**。goal 是文档写明的"最慢层"，两本账必须指同一个。
    """
    _use_tmp_root(monkeypatch, tmp_path)
    task = Task.create(goal="")
    task.apply_state_patch({"goal": "整理产出的真实目标"})
    assert task.goal == "整理产出的真实目标"
    # 人视图读的就是它
    from wovra import ui

    assert "整理产出的真实目标" in ui.report_view(task, [])


def test_load_backfills_goal_from_state(monkeypatch, tmp_path):
    """回归：旧会话的 goal 分裂在加载期自愈（不整理的会话也能治好）。

    与 v1→v3 块迁移、注册表回填同一模式：幂等，故可随加载进行。
    """
    import json as _json

    _use_tmp_root(monkeypatch, tmp_path)
    task = Task.create(goal="")
    task.task_state["goal"] = "旧会话里只在 State 里的目标"
    task.save()

    loaded = Task.load(task.id)
    assert loaded.goal == "旧会话里只在 State 里的目标"
    assert any(
        "goal" in e.get("detail", "") and "回填" in e.get("detail", "")
        for e in loaded.history if e.get("kind") == "maintenance"
    )
    # 落盘生效 + 幂等（第二次加载不再留痕）
    again = Task.load(task.id)
    assert again.goal == "旧会话里只在 State 里的目标"
    assert len(again.history) == len(loaded.history)


def test_load_backfill_does_not_override_existing_goal(monkeypatch, tmp_path):
    """反向对照：Task.goal 已有值时不被 State 覆盖（不夺权，只手补缺口）。"""
    _use_tmp_root(monkeypatch, tmp_path)
    task = Task.create(goal="建任务时定的目标")
    task.task_state["goal"] = "State 里的目标"
    task.save()

    loaded = Task.load(task.id)
    assert loaded.goal == "建任务时定的目标"


def test_load_drops_removed_field_keys(monkeypatch, tmp_path):
    """回归（2026-09-11 遗产整治）：加载期丢弃已删字段的遗留键。

    `acceptance_criteria` 已从 Task 删除（0/77 数据、0 消费者），但**实测
    77/77 个历史 task.json 都带这个键**（值为 `[]`，所以"非空计数"口径
    看不出它）。`cls(**data)` 遇到未知键直接抛 TypeError，不过滤则所有
    历史会话加载即崩——这正是"删字段必须配套加载口兼容"的现场证据。
    """
    import json as _json

    from wovra.task import Task

    _use_tmp_root(monkeypatch, tmp_path)
    task = Task.create(goal="g")
    task.save()

    # 手工往盘上的 JSON 塞回已删字段（模拟历史会话）+ 一个从未见过的键
    path = tmp_path / task.id / "task.json"
    data = _json.loads(path.read_text(encoding="utf-8"))
    data["acceptance_criteria"] = []
    data["some_future_field"] = {"x": 1}
    path.write_text(_json.dumps(data, ensure_ascii=False), encoding="utf-8")

    loaded = Task.load(task.id)  # 不抛 TypeError 即通过
    assert loaded.goal == "g"
    assert not hasattr(loaded, "acceptance_criteria")
    assert any(
        "丢弃已删字段" in e.get("detail", "")
        for e in loaded.history if e.get("kind") == "maintenance"
    )
    # 落盘生效：重新读盘后 JSON 里不再有这些键
    raw = _json.loads((tmp_path / task.id / "task.json").read_text(encoding="utf-8"))
    assert "acceptance_criteria" not in raw and "some_future_field" not in raw
