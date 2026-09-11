"""工具层测试（自 test_tools.py 拆分，2026-09-11）。

本模块：test_interaction。"""

import json
from types import SimpleNamespace
from wovra import task as task_module
from wovra.agent import Agent
from wovra.tools import ask_user, user_input_pending, write_file
from wovra.tools.safety import _ask_yes_no

# 共享夹具/工具（_helpers.py 是原文件的公共头部）
from ._helpers import *  # noqa: F401,F403

def test_ask_user_degrades_in_non_interactive_environment():
    """非交互环境（管道/测试捕获）下 ask_user 降级，不阻塞等待输入。"""
    result = ask_user("用哪个方案？", choices="A|B")
    assert "非交互环境" in result


def test_ask_yes_no_marks_user_input_pending(monkeypatch):
    """等待用户回答期间置 pending 标记（看门狗据此静默且不计秒）。"""
    import builtins
    import sys as _sys
    from types import SimpleNamespace as _NS

    from wovra import tools as tools_module

    monkeypatch.setattr(_sys, "stdin", _NS(isatty=lambda: True))
    # 交互模拟要自足：环境里的非交互标记优先于 isatty（实测教训——
    # 带 WOVRA_NONINTERACTIVE=1 启动的会话里该用例会静默走另一条路）
    monkeypatch.delenv(tools_module.safety.NONINTERACTIVE_ENV, raising=False)
    seen = {}

    def fake_input(prompt):
        seen["pending_during"] = tools_module.user_input_pending()
        return "y"

    monkeypatch.setattr(builtins, "input", fake_input)
    assert _ask_yes_no("确认？") is True
    assert seen["pending_during"] is True
    assert user_input_pending() is False


def test_ask_user_letter_choices_and_multi(monkeypatch):
    """ask_user 选项化：敲字母拍板、多选逗号分隔、自由文本仍可用。"""
    import sys

    from wovra import tools as tools_module
    from types import SimpleNamespace

    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: True))

    answers = iter(["B", "A,C", "我就要 D 这个自定义方案"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))

    out = tools_module.ask_user("选一个", "方案甲|方案乙|方案丙")
    assert out == "用户的回答: 方案乙"
    out = tools_module.ask_user("选几个", "方案甲|方案乙|方案丙", multi=True)
    assert out == "用户的回答: 方案甲 | 方案丙"
    out = tools_module.ask_user("选一个", "方案甲|方案乙|方案丙")
    assert out == "用户的回答: 我就要 D 这个自定义方案"


def test_split_choices_tolerates_newlines_labels_and_list():
    """choices 分割兼容三形态：| 分隔、换行分隔（deepseek 实测）、
    list；选项自带 A./B. 编号时去掉前缀。"""
    from wovra.tools.interaction import _split_choices

    assert _split_choices("是|否|继续") == ["是", "否", "继续"]
    assert _split_choices("我执行 A\n你执行 B\n先不提交") == [
        "我执行 A", "你执行 B", "先不提交",
    ]
    assert _split_choices("A. 甲\nB. 乙") == ["甲", "乙"]
    assert _split_choices(["甲", "乙"]) == ["甲", "乙"]
    assert _split_choices("") == []


def test_ask_user_newline_choices_render_letters(monkeypatch):
    """模型用换行而不是 | 时，选项仍逐条渲染成 A/B/C（不再整段塞进
    一个 A 选项）。"""
    import sys
    from types import SimpleNamespace

    from wovra import tools as tools_module

    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: True))
    seen = {}

    def fake_input(prompt=""):
        seen["prompt"] = prompt
        return "B"

    monkeypatch.setattr("builtins.input", fake_input)
    out = tools_module.ask_user("怎么处理", choices="甲\n乙\n丙")
    assert out == "用户的回答: 乙"
    assert "A. 甲" in seen["prompt"]
    assert "B. 乙" in seen["prompt"]
    assert "C. 丙" in seen["prompt"]


def test_ask_user_tolerates_list_choices(monkeypatch):
    """choices 误传成 list 不再崩（原 AttributeError），按选项处理。"""
    import sys
    from types import SimpleNamespace

    from wovra import tools as tools_module

    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr("builtins.input", lambda prompt="": "A")
    out = tools_module.ask_user("选一个", choices=["方案甲", "方案乙"])
    assert out == "用户的回答: 方案甲"


def test_hooks_block_and_feedback(monkeypatch, tmp_path):
    """用户钩子：pre 拦截（理由回传模型），post 附反馈——扩展点不进代码。"""
    import json as _json

    from wovra import tools as tools_module

    monkeypatch.setattr(tools_module.safety, "PROJECT_ROOT", tmp_path)
    hooks = tmp_path / ".wovra" / "hooks"
    hooks.mkdir(parents=True)
    (hooks / "pre_tool.py").write_text(
        "import json, sys\n"
        "p = json.load(sys.stdin)\n"
        "if p['tool'] == 'write_file' and p['arguments'].get('path', '').endswith('secret.txt'):\n"
        "    print('secret.txt 属于禁写区')\n"
        "    sys.exit(1)\n",
        encoding="utf-8",
    )
    (hooks / "post_tool.py").write_text(
        "import json, sys\n"
        "p = json.load(sys.stdin)\n"
        "if p['tool'] == 'write_file':\n"
        "    print('已同步到审计索引')\n",
        encoding="utf-8",
    )

    blocked = tools_module.run_pre_hook("write_file", {"path": "secret.txt"})
    assert blocked is not None and "禁写区" in blocked
    assert tools_module.run_pre_hook("write_file", {"path": "ok.txt"}) is None

    feedback = tools_module.run_post_hook("write_file", {"path": "ok.txt"}, "已写入")
    assert feedback == "已同步到审计索引"


def test_hooks_silent_when_absent(monkeypatch, tmp_path):
    """没有钩子文件：放行、无反馈——扩展机制缺席时不留痕迹。"""
    from wovra import tools as tools_module

    monkeypatch.setattr(tools_module.safety, "PROJECT_ROOT", tmp_path)
    assert tools_module.run_pre_hook("write_file", {}) is None
    assert tools_module.run_post_hook("write_file", {}, "结果") is None


def test_invoke_tool_wires_hooks_end_to_end(monkeypatch, tmp_path):
    """集成：_invoke_tool 走 钩子拦截/放行/反馈 全链路。"""
    from wovra import tools as tools_module
    from wovra.agent import Agent

    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path)
    monkeypatch.setattr(tools_module.safety, "PROJECT_ROOT", tmp_path)
    hooks = tmp_path / ".wovra" / "hooks"
    hooks.mkdir(parents=True)
    (hooks / "pre_tool.py").write_text(
        "import json, sys\n"
        "p = json.load(sys.stdin)\n"
        "if '禁词' in json.dumps(p, ensure_ascii=False):\n"
        "    print('命中禁词')\n"
        "    sys.exit(1)\n",
        encoding="utf-8",
    )

    def write_file(path, content):
        return f"已创建 {path}"

    agent = Agent(llm=None, tools=[write_file])
    blocked = agent._invoke_tool("write_file", json.dumps({"path": "x.txt", "content": "含禁词"}))
    assert "被用户钩子拦截" in blocked and "命中禁词" in blocked
    ok = agent._invoke_tool("write_file", json.dumps({"path": "y.txt", "content": "正常"}))
    assert "已创建 y.txt" in ok
