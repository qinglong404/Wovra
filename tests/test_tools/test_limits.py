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


def test_clip_over_limit_is_preview_plus_pointer(monkeypatch, tmp_path):
    """超限时：只内联短预览 + 体量 + 落盘路径，全文从盘上一字不差取回。

    旧契约是「首尾各留一大段」（limit 那么多字符仍进上下文）；用户口径
    （2026-09-11）是「量大可以不进上下文，返回截断内容但可以让你取回，
    告诉它有多大就可以」。
    """
    from wovra.tools import limits, safety

    monkeypatch.setattr(safety, "PROJECT_ROOT", tmp_path)
    monkeypatch.delenv("WOVRA_PREVIEW_CHARS", raising=False)
    text = "头" * 300 + "中段唯一秘密" * 100 + "尾"

    out = limits.clip(text, "unit", limit=400)

    assert len(out) < 3_000                      # 只留下预览量级，不是 limit 那么多
    assert "未内联" not in out                   # 不再有"中段未内联"这种首尾拼接
    assert "原文共" in out and f"{len(text):,}" in out   # 告诉模型它有多大
    assert "output/spill/" in out                # 告诉模型去哪取
    assert out.startswith("头")
    spilled = list((tmp_path / "output" / "spill").glob("*unit*.txt"))
    assert len(spilled) == 1
    assert spilled[0].read_text(encoding="utf-8") == text  # 一字不差，没丢文本


def test_clip_preview_size_is_configurable(monkeypatch, tmp_path):
    """预览量可调（WOVRA_PREVIEW_CHARS）；非法值退回默认。"""
    from wovra.tools import limits, safety

    monkeypatch.setattr(safety, "PROJECT_ROOT", tmp_path)
    monkeypatch.setenv("WOVRA_PREVIEW_CHARS", "50")
    out = limits.clip("甲" * 5_000, "unit", limit=100)
    assert out.startswith("甲" * 50) and not out.startswith("甲" * 51)

    monkeypatch.setenv("WOVRA_PREVIEW_CHARS", "abc")
    assert limits.preview_chars() == 2_000


def test_clip_source_skips_spill(monkeypatch, tmp_path):
    """原文本就在磁盘上（read_file）时不落盘副本，只指明去哪取。"""
    from wovra.tools import limits, safety

    monkeypatch.setattr(safety, "PROJECT_ROOT", tmp_path)
    out = limits.clip("甲" * 5_000, "read-x.py", limit=100, source="src/x.py")
    assert "src/x.py" in out and "原文共 5,000 字符" in out
    assert not list((tmp_path / "output" / "spill").glob("*.txt"))  # 没有副本


def test_clip_survives_unwritable_dir(monkeypatch, tmp_path):
    """落盘失败也不影响主流程：仍返回预览与体量，只是没有路径提示。"""
    from wovra.tools import limits, safety

    monkeypatch.setattr(safety, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(limits, "spill", lambda text, name: None)

    out = limits.clip("甲" * 500, "unit", limit=100)
    assert "原文共 500 字符" in out and "output/spill/" not in out
    assert out.startswith("甲" * 100)


@pytest.mark.parametrize("name", ["run_command", "search", "glob", "web_fetch"])
def test_spill_names_are_distinguishable(monkeypatch, tmp_path, name):
    """落盘文件名带用途前缀，事后能看出是哪个工具吐出来的。"""
    from wovra.tools import limits, safety

    monkeypatch.setattr(safety, "PROJECT_ROOT", tmp_path)
    limits.spill("x" * 10, name)
    assert len(list((tmp_path / "output" / "spill").glob(f"*{name}*.txt"))) == 1
