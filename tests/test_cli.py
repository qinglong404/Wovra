"""CLI 的离线测试：new / list / show 不依赖模型调用，可以完整覆盖。

run / chat 会发起真实模型调用，不属于单元测试范围——它们的逻辑
（任务加载、Agent 绑定）已被其他测试覆盖。
"""

import json

from wovra import task as task_module
from wovra.cli import main as cli_main


def _use_tmp_root(monkeypatch, tmp_path):
    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path)


def test_new_creates_task_and_prints_id(monkeypatch, tmp_path, capsys):
    _use_tmp_root(monkeypatch, tmp_path)

    cli_main(["new", "测试目标", "--req", "约束A", "--criteria", "标准B"])

    out = capsys.readouterr().out
    assert "已创建任务" in out
    # 输出里的任务 id 应与磁盘上的目录对应
    task_id = out.split(":")[1].split()[0]
    data = json.loads((tmp_path / task_id / "task.json").read_text(encoding="utf-8"))
    assert data["goal"] == "测试目标"
    assert data["requirements"] == ["约束A"]
    assert data["acceptance_criteria"] == ["标准B"]


def test_list_shows_existing_tasks(monkeypatch, tmp_path, capsys):
    _use_tmp_root(monkeypatch, tmp_path)

    cli_main(["new", "第一个任务"])
    cli_main(["new", "第二个任务"])
    capsys.readouterr()  # 丢弃 new 的输出

    cli_main(["list"])
    out = capsys.readouterr().out
    assert "第一个任务" in out
    assert "第二个任务" in out
    assert "in_progress" in out


def test_show_prints_report(monkeypatch, tmp_path, capsys):
    _use_tmp_root(monkeypatch, tmp_path)

    cli_main(["new", "要被展示的目标"])
    task_id = capsys.readouterr().out.split(":")[1].split()[0]

    cli_main(["show", task_id])
    out = capsys.readouterr().out
    assert "# 任务报告：" in out
    assert "要被展示的目标" in out


def test_run_with_missing_task_fails_friendly(monkeypatch, tmp_path):
    _use_tmp_root(monkeypatch, tmp_path)

    try:
        cli_main(["run", "不存在的任务", "你好"])
    except SystemExit as e:
        assert "任务不存在" in str(e)
    else:
        raise AssertionError("应该以 SystemExit 报错")
