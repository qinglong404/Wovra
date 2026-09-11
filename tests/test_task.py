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
