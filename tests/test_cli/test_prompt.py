"""CLI 测试（自 test_cli.py 拆分，2026-09-11）。

本模块：test_prompt。"""

from types import SimpleNamespace
import pytest
from wovra import cli as cli_module
from wovra.cli import main as cli_main

# 共享夹具/工具（_helpers.py 是原文件的公共头部）
from ._helpers import *  # noqa: F401,F403

def test_system_prompt_matches_mode_and_environment(monkeypatch):
    """系统提示词按模式写实：managed 才提 expand_history，并附带运行环境。"""
    import os as _os

    from wovra.agent import MODE_BASELINE, MODE_MANAGED
    from wovra.cli import _system_prompt

    managed = _system_prompt(MODE_MANAGED)
    baseline = _system_prompt(MODE_BASELINE)
    assert "expand_history" in managed
    assert "expand_history" not in baseline  # baseline 没注册这个工具，不能预告
    # R5 教训（新需求整体重写文件被用户批评）固化为工程纪律，两模式都有
    for prompt in (managed, baseline):
        assert "模块化多文件结构" in prompt
        assert "禁止为一条新需求整体重写文件" in prompt
        # D 组实证驱动的两条：批量调用省首字延迟；观感是前端评分大头
        assert "批量发出" in prompt
        assert "一眼全黑" in prompt
        assert "验证分层" in prompt
        # E 组实证：单次响应承载大批量写入 → 生成数分钟 → 缓存过期全价重算
        assert "大文件写每轮一两个" in prompt
    # 运行环境信息防止模型在 Windows 上跑类 Unix 命令
    if _os.name == "nt":
        assert "cmd.exe" in managed and "Windows" in managed
    else:
        assert "Linux" in managed
    # 可用性改进（2026-09-11）：提示词直接告诉模型本项目用 uv run，
    # 不直接调 .venv/bin/...（会被安全层拦，见 test_safety venv 用例）
    assert "uv run" in managed and "uv run" in baseline
    assert ".venv/bin" in managed


def test_system_prompt_forbids_ack_on_runtime_injection():
    """系统提示词要求模型对运行时注入（非用户输入）不做确认性回复。

    用户拍板（2026-09-11）：收到 <runtime-reminder> 信封或 [运行时] 开头
    的新轮消息这类机制注入时，不要回"收到/明白/好的"这类无意义确认——
    它们不是问题、不需要应答。两模式都要带这条约束。
    """
    from wovra.agent import MODE_BASELINE, MODE_MANAGED
    from wovra.cli import _system_prompt

    for mode in (MODE_MANAGED, MODE_BASELINE):
        prompt = _system_prompt(mode)
        assert "不要做确认性回复" in prompt
        assert "收到" in prompt or "不是问题、不需要应答" in prompt
        assert "直接继续既有工作或保持静默" in prompt


def test_workspace_instructions_injected_from_agents_md(monkeypatch, tmp_path):
    """工作区指令包（1.3）：AGENTS.md 追加进系统提示词，缺失则跳过。"""
    (tmp_path / "AGENTS.md").write_text(
        "测试用 uv run pytest；不要动 .wovra/ 目录", encoding="utf-8"
    )
    monkeypatch.setattr(cli_module.prompt, "PROJECT_ROOT", tmp_path)

    prompt = cli_module._system_prompt("managed")

    assert "[工作区指令]" in prompt
    assert "uv run pytest" in prompt and "不要动 .wovra/" in prompt


def test_workspace_instructions_absent_is_silent(monkeypatch, tmp_path):
    monkeypatch.setattr(cli_module.prompt, "PROJECT_ROOT", tmp_path)
    prompt = cli_module._system_prompt("managed")
    assert "[工作区指令]" not in prompt


def test_config_error_exits_with_friendly_message(monkeypatch, capsys):
    """配置错误由 main 统一打印提示并以非零码退出,不打 traceback。"""
    import importlib

    from wovra.llm import LLMConfigError

    def fake_cmd(args):
        raise LLMConfigError("模型配置问题:端点上不存在该模型")

    # patch 的是 main 模块里 main() 实际调用的那个 cmd_chat（from-import
    # 绑定）；用 importlib 取模块对象——包属性 `wovra.cli.main` 被入口
    # 函数同名遮蔽，点号 patch 串会解析到函数上。
    monkeypatch.setattr(
        importlib.import_module("wovra.cli.main"), "cmd_chat", fake_cmd
    )
    with pytest.raises(SystemExit) as excinfo:
        cli_main(["chat"])
    assert excinfo.value.code == 1
    assert "模型配置问题" in capsys.readouterr().out


def test_run_turn_propagates_config_error_not_turn_limit():
    """回归：LLMConfigError 不能被 _run_turn 的 RuntimeError 分支吞成"步数超限"。"""
    from types import SimpleNamespace

    from wovra.cli import _run_turn
    from wovra.llm import LLMConfigError

    finalized = []

    def _raise(*args, **kwargs):
        raise LLMConfigError("模型配置问题")

    agent = SimpleNamespace(run=_raise, finalize_round=finalized.append)
    with pytest.raises(LLMConfigError):
        _run_turn(agent, "hi")
    assert finalized == ["open"]  # 轮次已收尾为开放，异常原样上抛
