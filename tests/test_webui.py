"""前端静态守卫：内联 <script> 必须是合法 JS。

由来（2026-09-14，Linux 侧拉取实测）：一批"补丁插错位置"的提交里，
`function purgeLiveDom(){` 被插进注释中间、`}` 与
`async function loadTree(force){` 互换了位置——整个 script 块报废，
页面停在"加载中"，而 Python 测试一条都不会红（前端是唯一"改完看不见"
的层，`scripts/webui_syntax_check.py` 的 docstring 早就这么写了，只是
从没被自动跑过）。这里把它接进 pytest，node 缺了就跳过——不能因为
缺工具把测试判红，也不能让它静默空转（脚本对"0 个 script 块"直接
报错退出，空转的仪器等于没有仪器）。
"""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]


def test_webui_inline_scripts_are_syntactically_valid():
    if shutil.which("node") is None:
        pytest.skip("node 不可用，跳过前端语法检查")
    result = subprocess.run(
        [sys.executable, str(_ROOT / "scripts" / "webui_syntax_check.py")],
        cwd=_ROOT, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "通过" in result.stdout, result.stdout


def test_webui_renders_without_undefined_or_nan():
    """语义关：把内联脚本在迷你 DOM 里真跑一遍，核对 30 个渲染场景。

    语法合法不等于画得对——`renderCtxbar` 被吞进 `ensureViewSizes`（顶层
    声明错位）那类损伤，语法检查可以全绿而页面对应区域整块空白；这台
    仪器（`scripts/webui_render_check.py`）是产品侧早就有的第二道关，
    2026-09-14 起接进 pytest，改前端必过。node 缺了跳过。
    """
    if shutil.which("node") is None:
        pytest.skip("node 不可用，跳过前端渲染核对")
    result = subprocess.run(
        [sys.executable, str(_ROOT / "scripts" / "webui_render_check.py")],
        cwd=_ROOT, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "渲染核对：通过" in result.stdout, result.stdout
