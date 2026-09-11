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


def test_search_and_glob_reach_spill_dir(monkeypatch, tmp_path):
    """显式指定 output/spill 时必须能搜到刚落盘的大输出。

    2026-09-12 用户口径：大输出可以不全加载，但必须能**快速定位**。
    若 output/ 永远被当噪声跳过，「量大不进上下文」就变成「永远找不到」。
    噪声目录现在按**起点**判定：显式进去就照搜，默认起点仍跳过。
    """
    from wovra import tools as tools_module

    monkeypatch.setattr(tools_module.safety, "PROJECT_ROOT", tmp_path)
    spill = tmp_path / "output" / "spill"
    spill.mkdir(parents=True)
    (spill / "20260912-000000-run_command.txt").write_text(
        "开头\n中间 ERROR 关键失败\n结尾", encoding="utf-8"
    )

    hit = tools_module.search_files("ERROR", directory="output/spill")
    assert "run_command.txt:2:" in hit and "关键失败" in hit

    listed = tools_module.glob_files("*.txt", directory="output/spill")
    assert "run_command.txt" in listed

    # 默认起点仍把 output/ 当噪声跳过——平时搜索的手感不变
    assert "无匹配" in tools_module.search_files("ERROR")


def test_search_files_directory_is_file_gives_hint(monkeypatch, tmp_path):
    """search_files 的 directory 指向文件 → 提示用 read_file（不再静默"无匹配"）。"""
    from wovra import tools as tools_module

    monkeypatch.setattr(tools_module.safety, "PROJECT_ROOT", tmp_path)
    write_file("app.py", "def foo(): pass")
    result = tools_module.search_files("foo", directory="app.py")
    assert "文件而非目录" in result and "read_file" in result


def test_search_files_directory_missing_gives_hint(monkeypatch, tmp_path):
    """search_files 的 directory 不存在 → 提示路径，不再静默"无匹配"。"""
    from wovra import tools as tools_module

    monkeypatch.setattr(tools_module.safety, "PROJECT_ROOT", tmp_path)
    result = tools_module.search_files("foo", directory="no-such-dir")
    assert "directory 不存在" in result and "no-such-dir" in result


def test_glob_files_directory_is_file_gives_hint(monkeypatch, tmp_path):
    """glob_files 的 directory 指向文件 → 提示用 read_file（不再静默"无匹配文件"）。"""
    from wovra import tools as tools_module

    monkeypatch.setattr(tools_module.safety, "PROJECT_ROOT", tmp_path)
    write_file("app.py", "x")
    result = tools_module.glob_files("*.py", directory="app.py")
    assert "文件而非目录" in result and "read_file" in result
