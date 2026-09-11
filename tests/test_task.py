"""Task 的持久化与视图测试。不联网、不依赖 .env。"""

import json

from wovra import task as task_module
from wovra.task import Task


def _use_tmp_root(monkeypatch, tmp_path):
    """把任务存储根目录指到 pytest 的临时目录，避免污染真实 tasks/。"""
    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path)


def test_create_and_save_load_roundtrip(monkeypatch, tmp_path):
    _use_tmp_root(monkeypatch, tmp_path)

    task = Task.create(
        goal="测试目标",
        requirements=["约束一"],
        acceptance_criteria=["标准一"],
    )
    task.record("user_input", "你好")
    task.set_summary("已完成一半")
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
    _use_tmp_root(monkeypatch, tmp_path)
    task = Task.create(goal="写报告的目标", acceptance_criteria=["A", "B"])
    task.set_summary("进展摘要内容")
    task.save()

    report = (tmp_path / task.id / "report.md").read_text(encoding="utf-8")
    assert "写报告的目标" in report
    assert "进展摘要内容" in report
    assert "- A" in report and "- B" in report


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


def test_report_renders_todo_milestone_and_steps(monkeypatch, tmp_path):
    """report.md 必须让人看到大步/小步——计划账本平时只在模型上下文里。"""
    _use_tmp_root(monkeypatch, tmp_path)
    task = Task.create(goal="阶段化任务")
    task.todo = {
        "milestone": {
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
    assert "## 当前阶段（大步 / 小步）" in report
    assert "骨架可跑" in report and "小步 1/2" in report
    assert "[x] 渲染循环" in report and "[ ] 键盘移动" in report
    assert "配色是否顺眼" in report


def test_todo_lines_explicit_when_no_milestone():
    """无开启大步时人视图不留空——明说没有，并交代已验收历史。"""
    task = Task.create(goal="空计划")
    assert task.todo_lines() == ["-（无开启中的大步）"]

    task.todo = {"history": [{"goal": "第一步"}, {"goal": "第二步"}]}
    lines = task.todo_lines()
    assert lines[0] == "-（无开启中的大步）"
    assert "已验收 2 个大步" in lines[1] and "第二步" in lines[1]


def test_todo_lines_flags_unplanned_milestone():
    """结构闸门的提醒也要出现在人视图里：开大步却还没拆小步。"""
    task = Task.create(goal="x")
    task.todo = {
        "milestone": {"goal": "还没拆", "acceptance": [], "planned": False},
        "steps": [],
    }
    assert any("尚未拆小步" in l for l in task.todo_lines())


def test_todo_summary_line_renders_stage_and_progress():
    """底栏单行摘要：阶段 + 小步进度 + 待人工验收，且必须单行。"""
    task = Task.create(goal="x")
    task.todo = {
        "milestone": {
            "goal": "骨架可跑",
            "acceptance": ["能移动"],
            "started_seq": 1,
            "deferred": ["配色是否顺眼"],
            "planned": True,
        },
        "steps": [{"text": "a", "done": True}, {"text": "b", "done": False}],
    }
    line = task.todo_summary_line()
    assert "阶段 骨架可跑" in line and "小步 1/2" in line and "待验收 1" in line
    assert "\n" not in line


def test_todo_summary_line_flags_unplanned_and_empty():
    task = Task.create(goal="x")
    task.todo = {"milestone": {"goal": "还没拆", "planned": False}, "steps": []}
    assert "未拆小步" in task.todo_summary_line()

    task.todo = {}
    assert task.todo_summary_line() == "无开启大步"

    task.todo = {"history": [{"goal": "一"}, {"goal": "二"}]}
    assert "已验收 2 个" in task.todo_summary_line()


def test_context_includes_goal_summary_and_recent_history(monkeypatch, tmp_path):
    _use_tmp_root(monkeypatch, tmp_path)
    task = Task.create(goal="给模型看的目标")
    task.record("user_input", "早期事件")
    task.set_summary("模型该知道的进展")
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
    """回归（2026-09-11 机制评审）：render 的 budget 必须真的截断。

    TaskState 的 7 个列表各有 200 条上限（STATE_LIST_CAP），理论最坏
    1400 条，且状态是**每轮都进上下文**的（信封尾部）——不设预算就是
    一条无界常驻负担。render 早就支持 budget，但装配处此前没传，
    截断保护形同虚设。
    """
    from wovra import task as task_module
    from wovra.task import TaskState, STATE_LIST_CAP

    state = TaskState(goal="目标")
    state.completed = [f"完成项{i}" for i in range(STATE_LIST_CAP)]
    full = state.render()
    assert "完成项0" in full and len(full) > 500

    cut = state.render(budget=200)
    assert len(cut) <= 200 + 30          # 截断 + 提示行
    assert cut.endswith("(任务状态过长已截断)")
    assert "完成项0" in cut               # 保头部（最旧条目在头部）
    assert "完成项最后" not in cut

    # 装配处的预算常量存在且被默认使用
    from wovra.agent.support import _STATE_RENDER_BUDGET
    assert _STATE_RENDER_BUDGET > 0


def test_assembly_truncates_oversized_task_state(monkeypatch):
    """回归（2026-09-11）：装配上下文里的任务状态受预算约束，
    不会随列表增长无限膨胀。"""
    from wovra.agent import Agent

    task = Task.create(goal="目标")
    task.task_state["completed"] = [f"完成项{i}" * 20 for i in range(300)]
    agent = Agent(llm=object(), tools=[], task=task)
    agent.current_round = None
    agent.messages = []
    msgs = agent._assemble_messages()
    body = "\n".join(m.get("content", "") for m in msgs)
    assert "任务状态过长已截断" in body


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
