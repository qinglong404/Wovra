"""工具层测试（自 test_tools.py 拆分，2026-09-11）。

本模块：test_search。"""

import pytest
from wovra.tools import glob_files, write_file

# 共享夹具/工具（_helpers.py 是原文件的公共头部）
from ._helpers import *  # noqa: F401,F403

def test_search_files_finds_matches_with_line_numbers(tmp_path, monkeypatch):
    from wovra import tools

    monkeypatch.setattr(tools.safety, "PROJECT_ROOT", tmp_path)
    (tmp_path / "a.py").write_text("def foo():\n    pass\n", encoding="utf-8")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "b.py").write_text("x = search_files 目标\n", encoding="utf-8")
    # 噪声目录里的同名内容应被跳过
    noise = tmp_path / ".venv"
    noise.mkdir()
    (noise / "c.py").write_text("def foo():\n", encoding="utf-8")

    result = tools.search_files(r"def foo", glob="*.py")

    assert "a.py:1:" in result
    assert ".venv" not in result  # 噪声目录被忽略

    scoped = tools.search_files(r"目标", directory="sub")
    assert "sub/b.py:1:" in scoped


def test_search_files_rejects_invalid_regex(tmp_path, monkeypatch):
    from wovra import tools

    monkeypatch.setattr(tools.safety, "PROJECT_ROOT", tmp_path)
    with pytest.raises(ValueError, match="正则表达式无效"):
        tools.search_files("([非法")


def test_search_files_context_lines(monkeypatch, tmp_path):
    """search_files 的 context 参数：匹配行附带前后 N 行（单行内 ⏎ 连接）。"""
    from wovra import tools as tools_module

    monkeypatch.setattr(tools_module.safety, "PROJECT_ROOT", tmp_path)
    write_file("app.py", "头部\n目标行\n尾部")
    out = tools_module.search_files("目标", context=1)
    assert "app.py:2:" in out
    assert "头部" in out and "尾部" in out and "⏎" in out
    out = tools_module.search_files("目标")
    assert "头部" not in out.split("｜上下文")[0] or "｜上下文" not in out


def test_glob_files_matches_and_filters_noise(monkeypatch, tmp_path):
    """glob 按文件名模式查找，跳过噪声目录，忽略目录过滤。"""
    from wovra import tools as tools_module

    monkeypatch.setattr(tools_module.safety, "PROJECT_ROOT", tmp_path)
    (tmp_path / "a.py").write_text("x = 1", encoding="utf-8")
    sub = tmp_path / "docs"
    sub.mkdir()
    (sub / "b.py").write_text("y = 2", encoding="utf-8")
    noise = tmp_path / ".venv"
    noise.mkdir()
    (noise / "c.py").write_text("z = 3", encoding="utf-8")

    result = glob_files("*.py")

    assert "a.py" in result and "docs/b.py" in result
    assert ".venv" not in result
    assert "无匹配文件" in glob_files("*.rs")
