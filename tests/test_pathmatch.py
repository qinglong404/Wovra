"""路径鲁棒匹配（`pathmatch`）单元测试。

背景（2026-09-14 用户口径："确实太脆了"）：分裂产物里的路径由模型抄写，
形态天然会漂（带工作区前缀 / 绝对 vs 相对 / `./` / 反斜杠）；此前字符串
全等，抄法一变就判漏项、整批中止。这里把三档规则的边界钉住。
"""
from wovra import pathmatch as pm


def test_norm_normalizes_separators_and_dot_prefix():
    assert pm.norm("a\\b\\c.py") == "a/b/c.py"
    assert pm.norm("./a.py") == "a.py"
    assert pm.norm(".//a.py") == "a.py"
    assert pm.norm("a/b/") == "a/b"
    assert pm.norm("  a/b.py  ") == "a/b.py"
    # 绝对路径保留前导斜杠（/etc/hostname 是合法形态）
    assert pm.norm("/etc/hostname") == "/etc/hostname"
    assert pm.norm("/etc/hostname/") == "/etc/hostname"


def test_matches_exact():
    assert pm.matches("src/a.py", ["src/a.py"])
    assert not pm.matches("src/a.py", ["src/b.py"])


def test_matches_workspace_prefixed_and_absolute_forms():
    """模型写带工作区前缀/绝对路径/./ 的形态，都要能对上真路径。"""
    assert pm.matches("tool-layer-audit.md", ["agent-test/tool-layer-audit.md"])
    assert pm.matches("a.md", ["/home/u/ws/agent-test/a.md"])
    assert pm.matches("/abs/ws/a.md", ["a.md"])
    assert pm.matches("a.md", ["./a.md"])
    assert pm.matches("sub/a.md", ["ws/sub/a.md"])


def test_matches_basename_only_when_unambiguous():
    """同名兜底只在**唯一**时认：两个不同目录的同名文件同时出现 → 不认。"""
    assert pm.matches("ws/deep/a.py", ["a.py"])                 # 唯一同名 → 认
    assert not pm.matches("a.py", ["x/a.py", "y/a.py"])         # 歧义 → 不认
    # 同一目录的两种写法（重复提及）不算歧义
    assert pm.matches("a.py", ["a.py", "./a.py", "ws/a.py"])


def test_directory_prefix_semantics():
    """旧形态目录前缀：按"在它下面"归属；目录不参与同名兜底。"""
    assert pm.entry_matches("src/wovra/tools/a.py", "src/wovra/tools")
    assert pm.entry_matches("src/wovra/tools/a.py", "src/wovra/tools/")
    assert not pm.entry_matches("src/wovra/other/a.py", "src/wovra/tools")
    # 工作区相对形态 vs 绝对形态的目录
    assert pm.under("agent-test/a.py", "/home/u/ws/agent-test")
    assert not pm.under("a.py", "src/wovra/tools")     # 裸文件名推不出目录


def test_no_fuzzy_for_directory_like_entries():
    """无扩展名的条目当目录处理，不靠后缀/同名串（防同名目录误伤）。"""
    assert not pm.matches("x/tools", ["tools"])
    assert not pm.matches("tools/y.py", ["tools"])                 # 目录不参与同名兜底
    assert not pm.entry_matches("x/tools/y.py", "tools")           # 不同目录层不算在它下面


def test_empty_inputs_are_safe():
    assert not pm.matches("", ["a.py"])
    assert not pm.matches("a.py", [])
    assert not pm.matches("a.py", None)
    assert not pm.under("", "src")
    assert pm.dirname("a.py") == "" and pm.basename("a/b.py") == "b.py"
