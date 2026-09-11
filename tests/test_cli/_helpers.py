"""共享测试夹具与工具（自 test_cli.py 拆分）。"""

import json
import threading
from types import SimpleNamespace
import pytest
from wovra import cli as cli_module
from wovra import task as task_module
from wovra.cli import main as cli_main
from wovra.task import Task

def _use_tmp_root(monkeypatch, tmp_path):
    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path)
def _seed_task(goal: str, updated_at: str | None = None) -> str:
    """直接落盘一个任务（new 命令已移除，测试自行构造数据）。"""
    task = Task.create(goal=goal)
    if updated_at:
        task.updated_at = updated_at
    task.save()
    return task.id


__all__ = [
    "_use_tmp_root",
    "_seed_task",
]
