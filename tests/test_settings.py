"""配置面板的后端契约：`.env` 改值不改注释 + 参数表分档 + 立即/重启两档生效。"""

import os

import pytest

from wovra import envfile
from wovra import settings as settings_module


SAMPLE = """# 顶部注释不许动
Wovra_MODEL=gpt-4o-mini
Wovra_API_KEY=sk-old
WOVRA_ORG_WATERMARK=100000

# 中间注释
WOVRA_OUTPUT_LIMIT=200000
"""


def _env(tmp_path, text: str = SAMPLE):
    p = tmp_path / ".env"
    p.write_text(text, encoding="utf-8")
    return p


def test_set_values_keeps_comments_and_other_lines(tmp_path):
    p = _env(tmp_path)
    changed = envfile.set_values({"WOVRA_ORG_WATERMARK": "50000"}, p)
    assert changed == ["WOVRA_ORG_WATERMARK"]
    text = p.read_text(encoding="utf-8")
    assert "# 顶部注释不许动" in text and "# 中间注释" in text
    assert "Wovra_MODEL=gpt-4o-mini" in text          # 无关行逐字节保留
    assert "WOVRA_ORG_WATERMARK=50000" in text
    assert text.startswith("# 顶部注释不许动\n")


def test_set_values_appends_missing_key_once(tmp_path):
    p = _env(tmp_path)
    envfile.set_values({"WOVRA_FOLD_TARGET": "0.5"}, p)
    envfile.set_values({"WOVRA_MAX_TURNS": "50"}, p)
    text = p.read_text(encoding="utf-8")
    assert text.count(envfile._APPEND_MARK) == 1      # 追加段只标一次
    assert "WOVRA_FOLD_TARGET=0.5" in text and "WOVRA_MAX_TURNS=50" in text


def test_set_values_rewrites_duplicates_and_clears(tmp_path):
    p = _env(tmp_path, "Wovra_MODEL=a\nWovra_MODEL=b\n")
    envfile.set_values({"Wovra_MODEL": "c"}, p)
    assert p.read_text(encoding="utf-8") == "Wovra_MODEL=c\nWovra_MODEL=c\n"
    envfile.set_values({"Wovra_MODEL": ""}, p)
    assert envfile.load_pairs(p)["Wovra_MODEL"] == ""


def test_set_values_quotes_whitespace_values(tmp_path):
    p = _env(tmp_path, "")
    envfile.set_values({"WOVRA_READONLY_DIRS": "a;b c"}, p)
    assert envfile.load_pairs(p)["WOVRA_READONLY_DIRS"] == "a;b c"


def test_load_pairs_reads_file_without_process_env(tmp_path):
    p = _env(tmp_path)
    pairs = envfile.load_pairs(p)
    assert pairs["Wovra_MODEL"] == "gpt-4o-mini"
    assert pairs["WOVRA_ORG_WATERMARK"] == "100000"


def test_env_path_honours_override(tmp_path, monkeypatch):
    monkeypatch.setenv("WOVRA_ENV_FILE", str(tmp_path / "custom.env"))
    assert envfile.env_path() == tmp_path / "custom.env"


def test_describe_masks_secrets_and_marks_scopes(tmp_path, monkeypatch):
    monkeypatch.setenv("WOVRA_ENV_FILE", str(_env(tmp_path)))
    monkeypatch.setenv("Wovra_API_KEY", "sk-abcdef123456")
    monkeypatch.delenv("Wovra_MODEL", raising=False)
    data = settings_module.describe()
    by_key = {row["key"]: row for row in data["items"]}
    key_row = by_key["Wovra_API_KEY"]
    assert key_row["value"] == "" and key_row["set"] is True
    assert key_row["masked"].endswith("3456") and "sk-abcdef" not in key_row["masked"]
    # 两档都有人：密钥/水位是立即生效，数据目录是重启后生效
    assert key_row["scope"] == settings_module.LIVE
    assert by_key["WOVRA_TASKS_ROOT"]["scope"] == settings_module.RESTART
    assert by_key["Wovra_MODEL"]["value"] == "gpt-4o-mini"   # 没设过 → 用默认展示
    assert {g["id"] for g in data["groups"]} >= {"model", "ctx", "maint", "tools"}


def test_apply_writes_file_and_env(monkeypatch, tmp_path):
    p = _env(tmp_path)
    monkeypatch.setenv("WOVRA_ENV_FILE", str(p))
    monkeypatch.delenv("WOVRA_ORG_WATERMARK", raising=False)
    monkeypatch.delenv("Wovra_MODEL", raising=False)
    r = settings_module.apply({"WOVRA_ORG_WATERMARK": "40000",
                               "Wovra_MODEL": "gpt-4o"})
    assert r["ok"] and sorted(r["saved"]) == ["WOVRA_ORG_WATERMARK", "Wovra_MODEL"]
    assert envfile.load_pairs(p)["WOVRA_ORG_WATERMARK"] == "40000"
    assert os.environ["WOVRA_ORG_WATERMARK"] == "40000"      # 立即同步进程
    assert os.environ["Wovra_MODEL"] == "gpt-4o"
    assert r["pending_restart"] == []                        # 这两项都是立即档


def test_apply_flags_restart_scope(monkeypatch, tmp_path):
    p = _env(tmp_path)
    monkeypatch.setenv("WOVRA_ENV_FILE", str(p))
    r = settings_module.apply({"WOVRA_TASKS_ROOT": str(tmp_path / "data")})
    assert r["ok"] and r["pending_restart"] == ["WOVRA_TASKS_ROOT"]


def test_apply_rejects_bad_values_without_writing(monkeypatch, tmp_path):
    p = _env(tmp_path)
    monkeypatch.setenv("WOVRA_ENV_FILE", str(p))
    before = p.read_text(encoding="utf-8")
    r = settings_module.apply({"WOVRA_ORG_WATERMARK": "abc",
                               "WOVRA_FOLD_TARGET": "7",
                               "WOVRA_NO_SUCH_KEY": "1"})
    assert not r["ok"] and r["saved"] == []
    assert r["errors"]["WOVRA_ORG_WATERMARK"] == "要整数"
    assert "不大于" in r["errors"]["WOVRA_FOLD_TARGET"]
    assert r["errors"]["WOVRA_NO_SUCH_KEY"] == "未知参数"
    assert p.read_text(encoding="utf-8") == before


def test_apply_secret_blank_keeps_and_nul_clears(monkeypatch, tmp_path):
    p = _env(tmp_path)
    monkeypatch.setenv("WOVRA_ENV_FILE", str(p))
    monkeypatch.setenv("Wovra_API_KEY", "sk-live")
    r = settings_module.apply({"Wovra_API_KEY": ""})          # 留空 = 不改
    assert r["saved"] == [] and envfile.load_pairs(p)["Wovra_API_KEY"] == "sk-old"
    r = settings_module.apply({"Wovra_API_KEY": "\x00"})      # 显式清空
    assert r["saved"] == ["Wovra_API_KEY"]
    assert envfile.load_pairs(p)["Wovra_API_KEY"] == ""
    assert "Wovra_API_KEY" not in os.environ


def test_apply_normalises_bool(monkeypatch, tmp_path):
    p = _env(tmp_path)
    monkeypatch.setenv("WOVRA_ENV_FILE", str(p))
    settings_module.apply({"WOVRA_V4": "off"})
    assert envfile.load_pairs(p)["WOVRA_V4"] == "0"
    assert os.environ["WOVRA_V4"] == "0"


def test_every_spec_key_is_documented_in_env_example():
    """参数表里的键要么在 .env.example 里出现过，要么带 hint——不留"黑盒旋钮"。"""
    from pathlib import Path
    example = (Path(__file__).resolve().parents[1] / ".env.example").read_text(
        encoding="utf-8")
    for spec in settings_module.SPECS:
        assert spec.label and spec.group in {g for g, _ in settings_module.GROUPS}
        assert spec.key in example or spec.hint, f"{spec.key} 既不在 .env.example 也无说明"


def test_live_scope_values_take_effect_without_restart(monkeypatch):
    """标 `live` 的参数改完**不重启**就该吃到（面板的"立即生效"是承诺，不是文案）。

    判据：内存里新建一个 Agent / 调用一次读取器，拿到的就是刚改的值。
    """
    from wovra.agent import support as support_module
    from wovra.agent import Agent
    from wovra.task import Task

    monkeypatch.setenv("WOVRA_ORG_WATERMARK", "70000")
    monkeypatch.setenv("WOVRA_FOLD_TARGET", "0.25")
    monkeypatch.setenv("WOVRA_IMAGE_VIEWS_MAX", "3")
    monkeypatch.setenv("WOVRA_MAX_TURNS", "77")
    assert support_module.org_watermark() == 70000
    assert support_module.fold_target() == 0.25
    assert support_module.image_view_hard() == 3

    agent = Agent(llm=object(), tools=[], task=Task.create(goal="g"))
    assert agent._org_watermark == 70000          # 构造期就吃到
    assert agent.max_turns == 77
    # 进程运行中再改（等于面板保存那一刻）：下一次读取就是新值，不必重启
    monkeypatch.setenv("WOVRA_ORG_WATERMARK", "80000")
    assert agent._org_watermark == 80000
    assert support_module.org_watermark() == 80000


def test_explicit_values_are_not_overridden_by_env(monkeypatch):
    """显式传入/赋过的值不被环境改动顶掉（测试与探针的确定性靠这条）。"""
    from wovra.agent import Agent
    from wovra.task import Task

    monkeypatch.setenv("WOVRA_ORG_WATERMARK", "70000")
    agent = Agent(llm=object(), tools=[], task=Task.create(goal="g"),
                  org_watermark=1234, max_turns=5)
    assert agent._org_watermark == 1234 and agent.max_turns == 5
    monkeypatch.setenv("WOVRA_ORG_WATERMARK", "90000")
    monkeypatch.setenv("WOVRA_MAX_TURNS", "999")
    assert agent._org_watermark == 1234 and agent.max_turns == 5


def test_envfile_rejects_nothing_silently(tmp_path):
    """文件不存在 = 空表（不抛），写入会新建（带 600 权限）。"""
    p = tmp_path / "sub" / ".env"
    assert envfile.load_pairs(p) == {}
    envfile.set_values({"Wovra_MODEL": "m"}, p)
    assert envfile.load_pairs(p) == {"Wovra_MODEL": "m"}
    if os.name != "nt":
        assert (p.stat().st_mode & 0o777) == 0o600
