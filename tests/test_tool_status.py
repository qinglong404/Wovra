"""工具结果成败判定：唯一权威口径的回归（含跨语言共享夹具）。

背景（2026-09-14 用户报障："工具消息块对成功/失败的判断太草率了…老是判断错"）：
判定曾是**全文子串**匹配——成功的 read_file 读到本项目源码（正文里必然出现
`工具执行出错` / `命令执行失败（` 这些字面量）就被判成失败。实测 tasks/ 全量
2541 条 tool_result 里 42 条假阳性、33 条假阴性（`scripts/probe_tool_status.py`）。

本模块钉两件事：
1. `tools/status.py` 的三条规则（只看首行 / 结构化 exit_code 优先 / 三分类）；
2. **共享夹具** `tests/fixtures/tool_status_cases.json` 全线通过——同一份用例
   前端（webui/index.html::resultStatus）也跑（见 tests/test_webui.py），
   任一侧漂移即红：这是"四个消费者一个口径"的机械保证。
"""
import json
import pathlib

import pytest

from wovra.tools import status
from wovra.tools.status import classify, denied, failed, first_line, label

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "tool_status_cases.json"


def _cases() -> list[dict]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]


def test_shared_fixture_exists_and_is_substantial():
    """夹具本身是契约：少了就是有人把它删了，不是"没用例"。"""
    cases = _cases()
    assert len(cases) >= 30
    assert {c["expect"] for c in cases} == {"ok", "error", "deny"}
    for case in cases:
        assert case.get("why"), f"用例必须写清钉什么：{case}"


@pytest.mark.parametrize("case", _cases(), ids=[c["content"][:28] or "空" for c in _cases()])
def test_fixture_case(case):
    got = classify(case["content"])
    assert got == case["expect"], f"{case['why']}：期望 {case['expect']}，得到 {got}"


def test_only_first_line_is_examined():
    """正文里出现任何失败字样都不许翻案——这是 42 条假阳性的根因。"""
    body = "\n".join([
        "src/wovra/tools/safety.py（共 1060 行，以下为第 44-61 行）",
        "    FAILURE_MARKERS = (",
        '        "工具执行出错", "命令执行失败（", "未知工具",',
        ")",
        "# 命令执行失败（exit_code=1）会走这里",
        "raise ValueError('路径越界，只允许访问项目目录内的文件')",
    ])
    assert classify(body) == "ok"
    assert not failed(body)


def test_exit_code_beats_wording_but_only_on_first_line():
    """结构化信号最硬，但同样只在首行——正文里的 exit_code=0 不许翻案。"""
    assert classify("命令执行失败（exit_code=1，耗时 0.1s）\nstderr: boom") == "error"
    assert classify("exit_code=0（耗时 1.2s）\nstdout:\nok") == "ok"
    # 首行判负 / 正文的 0 无关
    assert classify("命令执行失败（exit_code=1）\n上一次是 exit_code=0") == "error"


def test_three_way_split_of_not_ok():
    """ok / error / deny 三分类：拒绝与出错是两件事，但都属于"没成"。"""
    refusal = "已拒绝执行危险命令：包含被禁止的模式 `rm -r`。"
    error = "文件不存在: nope.txt（解析为 /x）"
    assert classify(refusal) == "deny" and denied(refusal) and failed(refusal)
    assert classify(error) == "error" and not denied(error) and failed(error)
    assert classify("已创建 a.py（3 字符）") == "ok" and not failed("已创建 a.py（3 字符）")


def test_wrapper_prefix_is_stripped():
    """task.py 会记 `工具名 -> 结果`；判定前必须剥掉，否则首行锚定全落空。"""
    assert first_line("read_file -> 文件不存在: a.txt") == "文件不存在: a.txt"
    assert classify("boom({}) -> 工具执行出错: ValueError()") == "error"
    assert classify("read_file -> a.py（共 1 行）") == "ok"
    # 包装不影响正常文案（结果本身以短名开头但不是包装）
    assert first_line("已创建 a.py") == "已创建 a.py"


def test_labels_are_human_readable():
    assert label("ok") == "成功"
    assert label("文件不存在: x") == "失败"
    assert label("已拒绝执行危险命令：…") == "拒绝"
    assert status.LABELS == {"ok": "成功", "error": "失败", "deny": "拒绝"}


def test_real_corpus_regression_no_false_positive_on_source_reads():
    """真实语料回归：读本仓库源码的成功结果，一条都不许判失败。

    这是用户报障的原型场景（模型高频 read_file 自己的源码）。
    """
    root = pathlib.Path(__file__).resolve().parents[1] / "src" / "wovra" / "tools"
    samples = [
        f"{p}（共 900 行，以下为第 1-80 行）\n" + p.read_text(encoding="utf-8")[:4000]
        for p in sorted(root.glob("*.py"))[:8]
    ]
    assert samples
    for text in samples:
        assert classify(text) == "ok", f"源码读取被误判：{text[:60]}"
