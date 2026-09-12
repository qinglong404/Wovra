"""安全层命令检测探针（2026-09-11）：定位 git commit 被误拦的根因。

背景：`git commit -m "…prompts.py / support.py / … .venv/bin/python …"`
整条命令被安全层拦下，报错"访问工作区之外的绝对路径（/）"。
本探针逐项复现检测器，确认是哪条规则误伤。

2026-09-11 修复后本探针兼作回归验证：
- 误伤 1（文本分隔符）：`prompts.py / support.py` 不再拦
- 误伤 2（引号文本）：commit message 里的 .venv/bin/... 不再拦
- 误伤 3（cd 自身工作区）：`cd /home/.../Wovra` 不再拦
- 真实越界（find /、ls /、cat /etc/...、cd /tmp）仍拦
"""

from pathlib import Path

from wovra.tools.safety import (
    _command_escape,
    _linked_outside,
    _outside_absolute_paths,
)

WORKSPACE = Path.cwd().resolve()

CASES = [
    # 误伤 1：前后带空格的孤立 `/`（文本分隔符）——不该拦
    "prompts.py / support.py / tools/__init__.py",
    "echo a / b",
    # 误伤 2：message 文本里的 .venv/bin/python——不该拦
    'git commit -m "run_command 拦截 .venv/bin/python 时提示"',
    # 误伤 3：cd 到工作区本身的绝对路径——不该拦
    f"cd {WORKSPACE} && pwd",
    # 误伤 4（2026-09-12，worklog §48）：带值开关 `/C:"…"` 被当成盘根 C:
    # → 授权门判"过于宽泛"直接驳回，合法命令彻底挡死
    'python -m pip install --dry-run --no-deps pytest 2>&1 | findstr /C:"Would install"',
    'findstr /S /N /C:"from wovra.blocks" /C:"import blocks" src\\wovra\\tools\\*.py',
    "xcopy /E: src dst",
    # 对照：真实要拦的根目录访问
    "find / -name x",
    "ls -la /",
    "cat /etc/passwd",
    "head -3 /etc/passwd",
    "rm -rf /tmp/x",
    "cd /tmp && ls",
    "cd .. && pwd",
    # 对照：盘根绝对路径（开关判据不放过 `C:\x` 形态）
    "type C:\\Windows\\win.ini",
    # 真洞（2026-09-12，worklog §49 ①）：纯点斜 token 曾整类漏拦，`dir ..`
    # 实测列出了界外目录——属于"真读到了"，与上面几条误伤不同，必须拦
    "ls ..",
    "dir ..",
    "ls ../..",
    "cat -n ..",
    # 对照：文本里的点号（不是路径访问）——不该拦
    "echo ..",
    "printf 1..2",
    "ls a/../b.txt",
    # §49 ②：开关的值是盘根形态 → 仍受检
    "robocopy src dst /XD:C:\\Windows",
    # 引号内的绝对路径（数据文本）——不该拦
    'echo "see /etc/passwd for users"',
    # 正常提交消息
    'git commit -m "fix: 修 bug"',
]

print(f"工作区: {WORKSPACE}")
for c in CASES:
    print(f"命令: {c!r}")
    print(f"  _outside_absolute_paths -> {_outside_absolute_paths(c)}")
    print(f"  _linked_outside        -> {_linked_outside(c)}")
    print(f"  _command_escape        -> {_command_escape(c)!r}")
