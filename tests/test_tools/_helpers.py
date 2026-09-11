"""共享测试夹具与工具（自 test_tools.py 拆分）。"""

import json
import os as _os
import re
from types import SimpleNamespace
import pytest
from wovra import task as task_module
from wovra.agent import Agent
from wovra.task import Task
from wovra.tools import (
    FAILURE_MARKERS,
    ask_user,
    user_input_pending,
    check_background,
    delete_file,
    edit_file,
    glob_files,
    list_background,
    read_file,
    move_file,
    replace_lines,
    restore_file,
    run_background,
    run_command,
    search_files,
    stop_background,
    web_fetch,
    write_file,
)
from wovra.tools.safety import _ask_yes_no, _confirm_reason

class _StubLLM:
    model = "stub"

    def chat(self, *args, **kwargs):
        raise AssertionError("单元测试不应触发真实模型调用")
def _tool_call(name, arguments):
    return SimpleNamespace(id="c1", function=SimpleNamespace(name=name, arguments=arguments))
def _re_search_id(started: str) -> str:
    import re as _re

    return _re.search(r"bg-\d+", started).group(0)
class _FakeUrllib:
    """替换 tools.urllib.request.urlopen 的最小桩：返回罐头 HTML。"""

    def __init__(self, html: str):
        self._html = html

    class _Resp:
        def __init__(self, html: str):
            self._html = html
            self.headers = {"Content-Type": "text/html"}

        def read(self, n=-1):
            return self._html.encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def request(self, url, **kwargs):
        return None

    def __getattr__(self, name):
        raise AttributeError(name)
@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """隔离工作区：界内 root/，界外 outside/，并造好穿透用素材。

    返回 SimpleNamespace(root=..., outside=...)，方便断言"界外文件
    没被读到/没被删掉"。
    """
    from wovra import tools as tools_module

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("WOVRA_SECRET_TOKEN 界外机密\n" * 3, encoding="utf-8")
    (outside / "victim.txt").write_text("OUTSIDE DATA\n", encoding="utf-8")

    root = tmp_path / "workspace"
    root.mkdir()
    (root / "sub").mkdir()
    (root / "t1.txt").write_text("ROOT VERSION\n", encoding="utf-8")
    (root / "sub" / "t1.txt").write_text("SUB VERSION\n", encoding="utf-8")
    (root / "real.txt").write_text("REAL TARGET\n", encoding="utf-8")
    # 指向界外文件的链接、指向界外目录的链接、指向界内文件的链接
    try:
        _os.symlink(outside / "secret.md", root / "link_escape.md")
        _os.symlink(outside, root / "dirlink")
        _os.symlink(root / "real.txt", root / "alias.txt")
    except (OSError, NotImplementedError):  # Windows 无权限建链接
        pytest.skip("当前环境不支持创建符号链接")

    monkeypatch.setattr(tools_module.safety, "PROJECT_ROOT", root)
    monkeypatch.setattr(tools_module.safety, "_audit", lambda detail: None)
    monkeypatch.setattr(tools_module.safety, "_ask_yes_no", lambda question: True)
    return SimpleNamespace(root=root, outside=outside)

_TOOL_SURFACE_BASELINE = {
    "read_file": ["path", "start_line", "num_lines"],
    "write_file": ["path", "content", "force"],
    "edit_file": ["path", "old_text", "new_text", "replace_all"],
    "replace_lines": ["path", "start_line", "end_line", "new_content"],
    "delete_file": ["path"],
    "move_file": ["path", "new_path"],
    "list_files": ["directory"],
    "glob_files": ["pattern", "directory", "include_hidden"],
    "search_files": ["pattern", "directory", "glob", "context"],
    "web_fetch": ["url", "max_chars"],
    "web_search": ["query", "max_results"],
    "run_command": ["command", "timeout"],
    "run_background": ["command", "keep_alive"],
    "check_background": ["task_id"],
    "stop_background": ["task_id"],
    "list_background": [],
    "get_current_time": [],
    "restore_file": ["path", "version"],
    "ask_user": ["question", "choices", "multi"],
}

__all__ = [
    "_StubLLM",
    "_tool_call",
    "_re_search_id",
    "_FakeUrllib",
    "workspace",
    "_TOOL_SURFACE_BASELINE",
]
