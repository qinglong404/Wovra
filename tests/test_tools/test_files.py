"""工具层测试（自 test_tools.py 拆分，2026-09-11）。

本模块：test_files。"""

import pytest
from wovra.tools import (
    delete_file,
    edit_file,
    read_file,
    move_file,
    replace_lines,
    restore_file,
    write_file,
)

# 共享夹具/工具（_helpers.py 是原文件的公共头部）
from ._helpers import *  # noqa: F401,F403

def test_write_file_creates_then_reports_action(tmp_path, monkeypatch):
    from wovra import tools

    monkeypatch.setattr(tools.safety, "PROJECT_ROOT", tmp_path)
    first = write_file("reports/demo.txt", "第一版")
    assert "创建" in first
    assert (tmp_path / "reports/demo.txt").read_text(encoding="utf-8") == "第一版"

    second = write_file("reports/demo.txt", "第二版")
    assert "覆盖" in second
    assert (tmp_path / "reports/demo.txt").read_text(encoding="utf-8") == "第二版"


def test_edit_file_requires_unique_match(tmp_path, monkeypatch):
    from wovra import tools

    monkeypatch.setattr(tools.safety, "PROJECT_ROOT", tmp_path)
    write_file("code.txt", "alpha beta alpha")

    # 出现两次 → 拒绝，要求补充上下文
    with pytest.raises(ValueError, match="出现 2 次"):
        edit_file("code.txt", "alpha", "gamma")

    # 补充上下文唯一定位 → 成功
    result = edit_file("code.txt", "beta", "gamma")
    assert "已修改" in result
    assert (tmp_path / "code.txt").read_text(encoding="utf-8") == "alpha gamma alpha"

    # 找不到 → 报错
    with pytest.raises(ValueError, match="未找到"):
        edit_file("code.txt", "delta", "epsilon")


def test_read_file_supports_line_ranges(tmp_path, monkeypatch):
    from wovra import tools

    monkeypatch.setattr(tools.safety, "PROJECT_ROOT", tmp_path)
    (tmp_path / "big.txt").write_text(
        "\n".join(f"第{i}行" for i in range(1, 51)), encoding="utf-8"
    )

    result = read_file("big.txt", start_line=10, num_lines=5)
    assert "共 50 行，以下为第 10-14 行" in result
    assert "第10行" in result and "第14行" in result
    assert "第15行" not in result
    assert "start_line=15 继续读取" in result  # 续读提示


def test_read_file_reports_binary_and_empty(tmp_path, monkeypatch):
    from wovra import tools

    monkeypatch.setattr(tools.safety, "PROJECT_ROOT", tmp_path)
    (tmp_path / "bin.dat").write_bytes(b"\x00\x01\xff\xfe")
    (tmp_path / "empty.txt").write_text("", encoding="utf-8")

    assert "不是 UTF-8 文本文件" in read_file("bin.dat")
    assert "空文件" in read_file("empty.txt")


def test_edit_file_rejects_externally_modified_file(monkeypatch, tmp_path):
    """过期保护：文件在观察后被外部改动 → 拒绝编辑并要求重读。"""
    from wovra import tools as tools_module

    monkeypatch.setattr(tools_module.safety, "PROJECT_ROOT", tmp_path)
    write_file("note.md", "v1")
    (tmp_path / "note.md").write_text("用户改过的 v2", encoding="utf-8")  # 外部修改

    result = edit_file("note.md", "v1", "v3")

    assert "已被外部修改" in result
    assert "read_file" in result
    # 重新观察后 → 编辑放行
    read_file("note.md")
    result = edit_file("note.md", "用户改过的 v2", "v3")
    assert "已修改" in result


def test_write_file_rejects_stale_overwrite(monkeypatch, tmp_path):
    """整体覆盖同样受过期保护：外部改过的文件不能被盲写覆盖。"""
    from wovra import tools as tools_module

    monkeypatch.setattr(tools_module.safety, "PROJECT_ROOT", tmp_path)
    write_file("note.md", "v1")
    (tmp_path / "note.md").write_text("用户改过的 v2", encoding="utf-8")

    result = write_file("note.md", "v3")

    assert "已被外部修改" in result
    assert (tmp_path / "note.md").read_text(encoding="utf-8") == "用户改过的 v2"


def test_edit_file_reports_missing_path_friendly(monkeypatch, tmp_path):
    """文件不存在 → 友好消息带解析路径（参数装填错误一眼可见）。"""
    from wovra import tools as tools_module

    monkeypatch.setattr(tools_module.safety, "PROJECT_ROOT", tmp_path)
    result = edit_file("edit_file", "a", "b")
    assert "文件不存在" in result and "glob_files" in result


def test_edit_file_anchor_miss_gives_closest_hint(monkeypatch, tmp_path):
    """锚点未命中 → 给出最接近内容的行号，模型一次修正。"""
    from wovra import tools as tools_module

    monkeypatch.setattr(tools_module.safety, "PROJECT_ROOT", tmp_path)
    body = "第一行\n" + "\n".join(f"def func_{i}(x): return x + {i}" for i in range(20)) + "\n尾行"
    write_file("app.py", body)

    with pytest.raises(ValueError) as excinfo:
        edit_file("app.py", "def func_7(x): return x + 999\n多出来的一行", "替换")
    message = str(excinfo.value)
    assert "最接近的内容在第" in message
    assert "func_7" in message  # 提示指向最接近的锚点


def test_edit_file_success_reports_line_number(monkeypatch, tmp_path):
    from wovra import tools as tools_module

    monkeypatch.setattr(tools_module.safety, "PROJECT_ROOT", tmp_path)
    write_file("app.py", "第一行\n第二行\n第三行")
    result = edit_file("app.py", "第二行", "第二行（改）")
    assert "位于第 2 行" in result


def test_edit_file_replace_all_replaces_every_occurrence(monkeypatch, tmp_path):
    """count>1 默认拒绝并提示 replace_all；传 True 替换全部并在结果里报数。"""
    from wovra import tools as tools_module

    monkeypatch.setattr(tools_module.safety, "PROJECT_ROOT", tmp_path)
    write_file("app.py", "todo\n中间\ntodo\n尾部\ntodo")
    with pytest.raises(ValueError) as excinfo:
        edit_file("app.py", "todo", "done")
    assert "replace_all=True" in str(excinfo.value)

    result = edit_file("app.py", "todo", "done", replace_all=True)
    assert "全部 3 处" in result
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "done\n中间\ndone\n尾部\ndone"


def test_edit_file_zero_match_shows_provided_vs_actual_diff(monkeypatch, tmp_path):
    """未命中 → 「你提供的 vs 文件实际」差异反馈：照着改一次就中。"""
    from wovra import tools as tools_module

    monkeypatch.setattr(tools_module.safety, "PROJECT_ROOT", tmp_path)
    write_file("app.py", "def func_7(x):\n    return x + 7")
    with pytest.raises(ValueError) as excinfo:
        edit_file("app.py", "def func_7(x):\n    return x + 8", "换掉")
    message = str(excinfo.value)
    assert "你提供的" in message and "文件实际" in message
    assert "-    return x + 8" in message and "+    return x + 7" in message


def test_replace_lines_replaces_range_and_reports(monkeypatch, tmp_path):
    """行号替换：区间含两端、报告行数变化、结果正确。"""
    from wovra import tools as tools_module

    monkeypatch.setattr(tools_module.safety, "PROJECT_ROOT", tmp_path)
    write_file("app.py", "一\n二\n三\n四\n五")
    result = replace_lines("app.py", 2, 3, "两半\n两半半")
    assert "第 2-3 行" in result and "2 行 → 2 行" in result
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "一\n两半\n两半半\n四\n五"


def test_replace_lines_empty_content_removes_range(monkeypatch, tmp_path):
    """new_content 传空串 = 删除行区间（行数收缩正确）。"""
    from wovra import tools as tools_module

    monkeypatch.setattr(tools_module.safety, "PROJECT_ROOT", tmp_path)
    write_file("app.py", "一\n二\n三")
    replace_lines("app.py", 2, 2, "")
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "一\n三"


def test_replace_lines_rejects_out_of_range_and_stale(monkeypatch, tmp_path):
    """行号越界给出行数提示；文件被外部修改后拒绝（行号整体失效）。"""
    from wovra import tools as tools_module

    monkeypatch.setattr(tools_module.safety, "PROJECT_ROOT", tmp_path)
    write_file("app.py", "一\n二\n三")
    out = replace_lines("app.py", 2, 9, "x")
    assert "行号越界" in out and "共 3 行" in out

    (tmp_path / "app.py").write_text("一\n二\n三\n外部加的", encoding="utf-8")
    out = replace_lines("app.py", 1, 2, "x")
    assert "已被外部修改" in out


def test_checkpoint_archives_and_restores_roundtrip(monkeypatch, tmp_path):
    """checkpoint：覆盖/编辑自动归档旧版本，restore_file 列出并回滚。"""
    from wovra import tools as tools_module

    monkeypatch.setattr(tools_module.safety, "PROJECT_ROOT", tmp_path)
    write_file("app.py", "版本一")
    write_file("app.py", "版本二")          # 覆盖前自动归档"版本一"

    listing = restore_file("app.py")
    assert "历史版本" in listing and "版本一" in listing

    result = restore_file("app.py", "2026")
    assert "已回滚" in result
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "版本一"
    # 回滚前"版本二"也已归档：可再次回滚还原
    again = restore_file("app.py", sorted(
        (tmp_path / ".wovra" / "history" / "app.py").glob("*.bak")
    )[-1].stem)
    assert "已回滚" in again
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "版本二"


def test_history_prunes_to_keep_limit(monkeypatch, tmp_path):
    """每文件只保留最近 _HISTORY_KEEP 份，超出淘汰最旧。"""
    from wovra import tools as tools_module

    monkeypatch.setattr(tools_module.safety, "PROJECT_ROOT", tmp_path)
    for i in range(13):
        write_file("app.py", f"v{i}")
    versions = sorted((tmp_path / ".wovra" / "history" / "app.py").glob("*.bak"))
    assert len(versions) == tools_module.files._HISTORY_KEEP


def test_write_file_shrink_guard_blocks_and_force_bypasses(monkeypatch, tmp_path):
    """覆盖写缩水过半 → 防呆拦截；force=true 显式确认后放行。"""
    from wovra import tools as tools_module

    monkeypatch.setattr(tools_module.safety, "PROJECT_ROOT", tmp_path)
    write_file("page.html", "甲" * 3000)
    result = write_file("page.html", "薄壳")
    assert "防呆拦截" in result and "force" in result
    assert (tmp_path / "page.html").read_text(encoding="utf-8") == "甲" * 3000
    result = write_file("page.html", "薄壳", force=True)
    assert "已覆盖" in result


def test_delete_file_confirms_archives_and_deletes(monkeypatch, tmp_path):
    """删除走确认门：拒绝则保留；确认则归档后删除、可回滚。"""
    from wovra import tools as tools_module

    monkeypatch.setattr(tools_module.safety, "PROJECT_ROOT", tmp_path)
    write_file("app.py", "要被删的内容")
    monkeypatch.setattr(tools_module.safety, "_ask_yes_no", lambda q: False)
    assert "用户拒绝" in delete_file("app.py")
    assert (tmp_path / "app.py").exists()

    monkeypatch.setattr(tools_module.safety, "_ask_yes_no", lambda q: True)
    result = delete_file("app.py")
    assert "已删除" in result and "归档" in result
    assert not (tmp_path / "app.py").exists()
    restore = restore_file("app.py", sorted(
        (tmp_path / ".wovra" / "history" / "app.py").glob("*.bak")
    )[-1].stem)
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "要被删的内容"


def test_move_file_moves_and_refuses_overwrite(monkeypatch, tmp_path):
    from wovra import tools as tools_module

    monkeypatch.setattr(tools_module.safety, "PROJECT_ROOT", tmp_path)
    write_file("a.py", "内容")
    write_file("b.py", "占位")
    assert "目标已存在" in move_file("a.py", "b.py")
    result = move_file("a.py", "sub/a.py")
    assert "已移动" in result
    assert (tmp_path / "sub" / "a.py").read_text(encoding="utf-8") == "内容"


def test_edit_file_multi_match_lists_all_line_numbers(monkeypatch, tmp_path):
    """多匹配报错列出全部行号：模型扩写上下文消歧不必盲猜。"""
    from wovra import tools as tools_module

    monkeypatch.setattr(tools_module.safety, "PROJECT_ROOT", tmp_path)
    write_file("app.py", "todo\n中间\ntodo\n尾部\ntodo")
    with pytest.raises(ValueError) as excinfo:
        edit_file("app.py", "todo", "done")
    message = str(excinfo.value)
    assert "第 1 行" in message and "第 3 行" in message and "第 5 行" in message


def test_edit_file_success_shows_persistent_anchor(monkeypatch, tmp_path):
    """成功回显持久锚点（最近的注释/函数行）——行号漂移后仍可定位。"""
    from wovra import tools as tools_module

    monkeypatch.setattr(tools_module.safety, "PROJECT_ROOT", tmp_path)
    body = "// 配置区：以下为超时参数\nconst TIMEOUT = 30;\nconst RETRY = 3;\nconst BACKOFF = 5;"
    write_file("app.js", body)
    result = edit_file("app.js", "const BACKOFF = 5;", "const BACKOFF = 8;")
    assert "↳ const RETRY = 3;" in result  # 编辑点上方最近的持久锚点


def test_list_files_annotates_size_and_mtime(monkeypatch, tmp_path):
    """list_files 附带大小与修改时间（读段策略与新鲜度判断用）。"""
    from wovra import tools as tools_module

    monkeypatch.setattr(tools_module.safety, "PROJECT_ROOT", tmp_path)
    write_file("app.py", "内容")
    out = tools_module.list_files(".")
    entry = next(e for e in out if e.startswith("app.py"))
    assert "（" in entry and "B" in entry
