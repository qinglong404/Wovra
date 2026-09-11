"""工具输出上限与超限落盘（2026-09-11，worklog §25）。

背景：工具层此前各自写死小上限（run_command 1500 字符、search_files 50 条、
glob_files 200 条、check_background 2000 字符、web_fetch 8000 字符），
命中即丢内容——模型看不到中段就换方法重试，多次往返的代价远大于省下的
几百 token。现行规则：默认全量（上限 200,000 字符，WOVRA_OUTPUT_LIMIT 可调），
真超限时完整内容落盘 output/spill/，不丢数据。
"""

import pytest


def test_default_limit_is_generous(monkeypatch):
    """默认上限足够大——正常命令/搜索根本碰不到它。"""
    from wovra.tools import limits

    monkeypatch.delenv("WOVRA_OUTPUT_LIMIT", raising=False)
    assert limits.output_limit() == 200_000
    assert limits.output_limit() >= 100_000


def test_limit_is_configurable_and_illegal_falls_back(monkeypatch):
    """WOVRA_OUTPUT_LIMIT 可调；非法值退回默认而不是崩。"""
    from wovra.tools import limits

    monkeypatch.setenv("WOVRA_OUTPUT_LIMIT", "50000")
    assert limits.output_limit() == 50_000

    monkeypatch.setenv("WOVRA_OUTPUT_LIMIT", "abc")
    assert limits.output_limit() == 200_000

    monkeypatch.setenv("WOVRA_OUTPUT_LIMIT", "0")
    assert limits.output_limit() == 200_000


def test_list_limit_only_grows(monkeypatch):
    """条数上限只允许调大：调小会把默认口径也压回旧的小上限。"""
    from wovra.tools import limits

    monkeypatch.delenv("WOVRA_OUTPUT_LIMIT", raising=False)
    assert limits.list_limit(200) == 200

    monkeypatch.setenv("WOVRA_OUTPUT_LIMIT", "500")
    assert limits.list_limit(200) == 500

    monkeypatch.setenv("WOVRA_OUTPUT_LIMIT", "10")
    assert limits.list_limit(200) == 200  # 不因调小而低于默认


def test_clip_is_passthrough_under_limit():
    from wovra.tools import limits

    text = "小内容" * 10
    assert limits.clip(text, "unit") == text


def test_clip_spills_full_content(monkeypatch, tmp_path):
    """超限时：首尾内联 + 完整内容落盘 + 路径可读回（不丢数据）。"""
    from wovra.tools import limits, safety

    monkeypatch.setattr(safety, "PROJECT_ROOT", tmp_path)
    text = "头" * 300 + "中段唯一秘密" * 100 + "尾"

    out = limits.clip(text, "unit", limit=400)

    assert "未内联" in out and "output/spill/" in out
    assert out.startswith("头")
    assert out.rstrip().endswith("尾")
    spilled = list((tmp_path / "output" / "spill").glob("*unit*.txt"))
    assert len(spilled) == 1
    assert spilled[0].read_text(encoding="utf-8") == text  # 一字不差


def test_clip_survives_unwritable_dir(monkeypatch, tmp_path):
    """落盘失败也不影响主流程：仍返回首尾，只是没有路径提示。"""
    from wovra.tools import limits, safety

    monkeypatch.setattr(safety, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(limits, "spill", lambda text, name: None)

    out = limits.clip("甲" * 500, "unit", limit=100)
    assert "未内联" in out and "output/spill/" not in out


@pytest.mark.parametrize("name", ["run_command", "search", "glob", "web_fetch"])
def test_spill_names_are_distinguishable(monkeypatch, tmp_path, name):
    """落盘文件名带用途前缀，事后能看出是哪个工具吐出来的。"""
    from wovra.tools import limits, safety

    monkeypatch.setattr(safety, "PROJECT_ROOT", tmp_path)
    limits.spill("x" * 10, name)
    assert len(list((tmp_path / "output" / "spill").glob(f"*{name}*.txt"))) == 1
