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
