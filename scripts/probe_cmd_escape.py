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
    # 对照：真实要拦的根目录访问
    "find / -name x",
    "ls -la /",
    "cat /etc/passwd",
    "head -3 /etc/passwd",
    "rm -rf /tmp/x",
    "cd /tmp && ls",
    "cd .. && pwd",
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
