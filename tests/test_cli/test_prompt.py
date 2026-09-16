"""CLI 测试（自 test_cli.py 拆分，2026-09-11）。

本模块：test_prompt。"""

from types import SimpleNamespace
import pytest
from wovra import cli as cli_module
from wovra.cli import main as cli_main
from wovra.tools import safety as safety_module

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


def test_system_prompt_splits_user_intent_into_three_classes():
    """用户意图三分：**问 / 思想对齐 / 行动**（2026-09-13 用户口径）。

    用户原话："那将用户需要分成三类：1 问，这个时候只需读…只需回答用户问题即可，
    而不去做。2 思想对齐…只需分析其可行性，查漏补缺，给出建议。3 行动…应先将
    细节问清晰，收敛范围等等，然后开始行动，直到做完…收敛范围必须开始定好，
    后面无论是多少条，都做完，那怕是一百条，也做完再汇报。"

    为什么必须钉住：这是**语义判断**，机制管不了。判错的现场就在会话
    `20260913-151842-2628dd`——R4「如果让你写…你准备如何写？」直接开工、
    R7「可以实现吗？」跑了 42 条命令 25 次改文件、R8 已明说「先不提交，给我讲讲」
    仍跑了 7 条命令 1 次改文件。两模式都要带这三条。
    """
    from wovra.agent import MODE_BASELINE, MODE_MANAGED
    from wovra.cli import _system_prompt

    for mode in (MODE_MANAGED, MODE_BASELINE):
        prompt = _system_prompt(mode)
        assert "问 / 思想对齐 / 行动" in prompt
        # ① 问：只回答，不改
        assert "只回答" in prompt and "不改文件、不提交、不安装、不删除" in prompt
        # ② 思想对齐：只分析，不动手实现
        assert "只分析" in prompt and "不要动手实现" in prompt
        # ③ 行动：先收敛范围，然后一次做完（哪怕一百条）
        assert "把范围收敛定死" in prompt
        assert "哪怕一百条" in prompt and "做完再汇报" in prompt
        # 判不准时的兜底：问一句 + 默认按最保守的那一类
        assert "ask_user" in prompt and "默认按最保守的那一类办" in prompt
    # 路由只对第 3 类做（否则"我顺着问一句"就被转给别的域回答了）
    assert "路由只对第 3 类（行动）做" in _system_prompt(MODE_MANAGED)
    from wovra.agent import _ROUTE_TO_SCHEMA
    desc = _ROUTE_TO_SCHEMA["function"]["description"]
    assert "只转「要动手的活」" in desc


def test_system_prompt_forbids_ack_on_runtime_injection():
    """运行时注入（非用户输入）：不确认，**也不解释这条通知本身**。

    用户拍板（2026-09-11）起是"不要做确认性回复"；2026-09-16 提示词审阅 §3 补了
    另一半——实测那一轮跑完两次输出都在**解释运行时通知本身**（比"收到/明白"更啰嗦）。
    识别线索也换掉了：旧文写的"以 [运行时] 开头"这个字面量在源码里出现 0 处，是条
    失效线索；改成按 `<runtime-reminder>` 信封结构识别。
    """
    from wovra.agent import MODE_BASELINE, MODE_MANAGED
    from wovra.cli import _system_prompt

    for mode in (MODE_MANAGED, MODE_BASELINE):
        prompt = _system_prompt(mode)
        assert "也不解释或点评这条通知本身" in prompt
        assert "收到/明白/好的" in prompt
        assert "直接继续既有工作或保持静默" in prompt
        assert "[运行时]" not in prompt          # 失效线索不得留下
        assert "<runtime-reminder>" in prompt


def test_system_prompt_identity_is_self_not_an_external_platform():
    """身份（2026-09-16 提示词审阅 §1，P0）：你就是 Wovra，不是"跑在它上面的助手"。

    实测代价：旧首句"你是 Wovra 的执行助手" + 两处第三人称（"Wovra 运行时注入"、
    "由 Runtime 管理"）合起来被读成"Wovra 是外面的平台"，于是"测 Wovra"变成了
    "搭 harness 去测那个平台"——本仓库那次最大的方向性错误。
    """
    from wovra.agent import MODE_BASELINE, MODE_MANAGED
    from wovra.cli import _system_prompt

    for mode in (MODE_MANAGED, MODE_BASELINE):
        prompt = _system_prompt(mode)
        assert "你就是 Wovra" in prompt
        assert "不存在另一个需要你去查看或测试的「Wovra 系统」" in prompt
        for third_person in ("执行助手", "Wovra 运行时", "由 Runtime", "多模态模型"):
            assert third_person not in prompt


def test_system_prompt_has_stop_signal_and_scope_ceiling():
    """范围只许在"已定范围"内做完，不许擅自扩大；停止信号是终局指令。

    2026-09-16 提示词审阅 §2（P0）：旧文只有"不许缩小"单边约束（"一次做完…不要
    中途自作主张缩小范围"），实测三连犯同一动作——把一句话解读成它能承载的最大
    工作量，其中一次是**已收到"就停止"之后仍新起了一轮评测**。
    """
    from wovra.agent import MODE_BASELINE, MODE_MANAGED
    from wovra.cli import _system_prompt

    for mode in (MODE_MANAGED, MODE_BASELINE):
        prompt = _system_prompt(mode)
        assert "停止信号是终局指令" in prompt
        assert "不新起任何执行、不追加验证" in prompt
        assert "已启动的后台任务要停掉并报告" in prompt
        assert "不得擅自扩大范围" in prompt
        assert "先走便宜的那条" in prompt
        # 单边约束仍在（"一次做完"是已定范围内的纪律，不是扩大范围的理由）
        assert "哪怕一百条" in prompt and "做完再汇报" in prompt


def test_system_prompt_wording_hygiene():
    """措辞规范：不写"用户说…"式转述、不留"本项目"这种指代不明的说法。

    用户口径（2026-09-16）：提示词里不要出现"用户说……"这类啰嗦、身份错位的描述。
    "本项目"在工作区不是 Wovra 仓库时会把包读成"工作区之外另一个 wovra 项目"，
    与身份问题叠加（审阅 §5）——改成"本工作区"。
    """
    from wovra.agent import MODE_BASELINE, MODE_MANAGED
    from wovra.cli import _system_prompt

    for mode in (MODE_MANAGED, MODE_BASELINE):
        prompt = _system_prompt(mode)
        assert "用户说" not in prompt
        assert "本项目" not in prompt
        assert "本工作区" in prompt
        # 运行环境单独成节，不再混在正文里
        assert "## 运行环境" in prompt
        for section in ("## 身份", "## 工作区与环境", "## 工具使用", "## 交互纪律"):
            assert section in prompt


def test_system_prompt_names_the_assembled_web_and_orchestration_tools():
    """已装配的工具要提名（2026-09-16 提示词审阅 §4）。

    实测缺口：`web_automate`（专为"必须真交互的页面"新增）在提示词里从没出现过，
    遇到 SPA 时模型不会想到它；`todo`/`update_responsibility`/`notify`/`join_with`
    也只有 schema 描述，而 schema 在长上下文里优先级低于系统提示词。
    """
    from wovra.agent import MODE_BASELINE, MODE_MANAGED
    from wovra.cli import _system_prompt

    managed = _system_prompt(MODE_MANAGED)
    assert "web_automate" in managed and "不自动降级" in managed
    for name in ("todo", "update_responsibility", "notify", "join_with"):
        assert name in managed, name
    # baseline 模式没有这些编排工具，不许预告（写实原则）
    assert "join_with" not in _system_prompt(MODE_BASELINE)


def test_workspace_instructions_injected_from_agents_md(monkeypatch, tmp_path):
    """工作区指令包（1.3）：AGENTS.md 追加进系统提示词，缺失则跳过。"""
    (tmp_path / "AGENTS.md").write_text(
        "测试用 uv run pytest；不要动 .wovra/ 目录", encoding="utf-8"
    )
    # 工作区来源是 `safety.workspace_root()`（线程绑定优先、进程默认兜底）
    monkeypatch.setattr(safety_module, "PROJECT_ROOT", tmp_path)

    prompt = cli_module._system_prompt("managed")

    assert "[工作区指令]" in prompt
    assert "uv run pytest" in prompt and "不要动 .wovra/" in prompt


def test_workspace_instructions_absent_is_silent(monkeypatch, tmp_path):
    monkeypatch.setattr(safety_module, "PROJECT_ROOT", tmp_path)
    prompt = cli_module._system_prompt("managed")
    assert "[工作区指令]" not in prompt


def test_build_agent_binds_session_workspace(monkeypatch, tmp_path):
    """`_build_agent` 是会话 ↔ 文件世界的绑定点（2026-09-15）。

    它必须在读提示词/AGENTS.md **之前**绑——否则系统提示词里的"工作区：…"
    与实际工具解析的目录会不一致（serve 此前靠改进程全局来对齐，多线程下
    会互相串台，见 safety.py 模块头）。这里用假 Agent 只验绑定与提示词来源。
    """
    from wovra.cli import prompt as prompt_module

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "AGENTS.md").write_text("用 uv run pytest 跑测试", encoding="utf-8")
    captured: dict = {}

    def _fake_agent(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(**kwargs)

    monkeypatch.setattr(prompt_module, "Agent", _fake_agent)
    task = SimpleNamespace(workspace=str(ws), id="t1", task_state={},
                           registry=[], rounds=[])

    agent = prompt_module._build_agent(task)

    assert safety_module.workspace_root() == ws
    assert "用 uv run pytest 跑测试" in agent.system_prompt   # AGENTS.md 按绑定后的工作区读
    assert str(ws) in agent.system_prompt                     # 提示词里的工作区就是它
    assert captured["task"] is task


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
