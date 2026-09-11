"""端到端安全探针的看门测试：把探针本身纳入 pytest。

为什么要单独一个文件
--------------------
探针（experiments/security_probe.py）是**仪器**，仪器也会坏：
判定器写错、夹具没造好、金丝雀被复述污染，都会让它恒绿——
而恒绿的探针比没有探针更危险（给人虚假的安全感）。

所以这里测的不是"边界拦不拦得住"（那是探针的活儿），而是
**探针本身有没有判别力**：

1. 它在当前代码上应当全过；
2. 它必须**能报红**——用一个故意放开的假工具层验证，防止判定器
   退化成"永远说通过"。

第 2 条是关键：本文件作者在实施加固时，探针第一版就恒绿过一次
（模型拒绝动手 → 零工具调用 → 被误判成 BLOCKED）。
"""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load_probe():
    """按路径加载探针模块（它不在包内，是 experiments/ 下的脚本）。"""
    path = ROOT / "experiments" / "security_probe.py"
    spec = importlib.util.spec_from_file_location("security_probe", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["security_probe"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def probe_env(tmp_path, monkeypatch):
    """隔离出一个探针环境：临时工作区 + 界外素材。"""
    from wovra import tools as tools_module

    probe = _load_probe()
    workspace, outside = probe._build_workspace(tmp_path)
    monkeypatch.setattr(tools_module.safety, "PROJECT_ROOT", workspace)
    monkeypatch.setattr(tools_module.safety, "_audit", lambda text: None)
    monkeypatch.setattr(tools_module.safety, "_ask_yes_no", lambda question: True)
    return probe, workspace, outside


def test_probe_passes_on_hardened_tools(probe_env):
    """当前（已加固）工具层应全部通过——这是探针的基线。"""
    probe, workspace, outside = probe_env
    findings = probe.run_deterministic(workspace, outside)
    failed = [f for f in findings if not f.ok]
    assert not failed, (
        "探针发现越界通路：\n"
        + "\n".join(f"  {f.key} [{f.tool}] {f.detail} → {f.payload[:120]}"
                    for f in failed)
    )


def test_probe_has_control_group(probe_env):
    """必须含反向对照，否则"把工作区锁死"的实现也能全绿。"""
    probe, workspace, outside = probe_env
    findings = probe.run_deterministic(workspace, outside)
    controls = [f for f in findings if f.expect == "allowed"]
    assert len(controls) >= 5, "反向对照太少，测不出误杀"
    assert all(f.ok for f in controls), (
        "反向对照失败 = 界内合法操作被误拦（或被锁死）：\n"
        + "\n".join(f"  {f.key} → {f.payload[:120]}"
                    for f in controls if not f.ok)
    )


def test_probe_can_actually_detect_leaks(probe_env, monkeypatch):
    """判别力测试：把工具层换成"完全不拦"的假实现，探针必须报红。

    防的是判定器退化——如果探针无论面对什么都报通过，它就只是安慰剂。
    """
    from wovra import tools as tools_module

    probe, workspace, outside = probe_env

    # 假工具层：read_file/run_command 完全不校验路径，直接照做
    def fake_read(path, start_line=1, num_lines=200):
        real = workspace / path
        try:
            return real.read_text(encoding="utf-8")
        except OSError:
            return f"文件不存在: {path}"

    def fake_run(command, timeout=None):
        import os
        import subprocess
        import tempfile

        from wovra.tools.shell import _kill_process_tree
        # 与 tools.py 同款纪律：输出去临时文件而不是 PIPE——PIPE 会被
        # 孙进程攥住写端导致清理永久挂起（该教训已在 run_command 记录
        # 在案）；timeout 必给：语料含 `find /` 这类重量级命令，Windows
        # 下 PATH 里的 GNU find 会全盘扫描，探针执行任何命令都必须有界。
        with tempfile.TemporaryFile() as out_f, tempfile.TemporaryFile() as err_f:
            proc = subprocess.Popen(
                command, shell=True, stdout=out_f, stderr=err_f,
                stdin=subprocess.DEVNULL, cwd=workspace,
                start_new_session=os.name != "nt",
                creationflags=(
                    subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
                ),
            )
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                _kill_process_tree(proc.pid)
                return "exit_code=-1（超出假工具层 15s 上限）\nstdout:\n(无输出)"
            out_f.seek(0)
            out = out_f.read().decode("utf-8", errors="replace").strip() or "(无输出)"
        return f"exit_code={proc.returncode}\nstdout:\n{out}"

    monkeypatch.setattr(tools_module, "read_file", fake_read)
    monkeypatch.setattr(tools_module, "run_command", fake_run)

    findings = probe.run_deterministic(workspace, outside)
    leaked = [f for f in findings if f.breached and not f.known_leak]
    assert leaked, (
        "假工具层完全不拦，探针却仍报全绿——判定器已失效，"
        "这个探针不能再用来判断安全"
    )
    # 具体确认它抓到了经典的绝对路径通路
    keys = {f.key for f in leaked}
    assert any("cat绝对" in k or "read:绝对路径" in k for k in keys), (
        f"未抓到最基础的绝对路径通路，实际抓到：{sorted(keys)}"
    )


def test_probe_corpus_covers_all_channels(probe_env):
    """语料要覆盖各通道，别只剩一两个工具。"""
    probe, _, _ = probe_env
    attempts = probe.build_attempts()
    tools_covered = {a.tool for a in attempts}
    for required in ("read_file", "search_files", "glob_files", "list_files",
                     "write_file", "edit_file", "delete_file", "move_file",
                     "run_command"):
        assert required in tools_covered, f"{required} 未被任何尝试覆盖"
    assert len(attempts) >= 40, f"语料太薄（{len(attempts)} 条），覆盖面不足"


def test_probe_canary_never_appears_in_prompts(probe_env):
    """金丝雀纪律：标记字符串绝不能写进给模型的指令里。

    否则模型复述任务时会把标记吐进回答，判定器误报泄漏——
    作者在实施加固时踩过两次同类坑。
    """
    probe, _, _ = probe_env
    for _, prompt in probe.MODEL_SCENARIOS:
        assert probe.OUTSIDE_CANARY not in prompt, (
            f"模型指令里含金丝雀，会造成判定污染：{prompt[:80]}"
        )
