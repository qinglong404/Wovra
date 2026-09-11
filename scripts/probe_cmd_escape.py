"""安全层命令检测探针（2026-09-11）：定位 git commit 被误拦的根因。

背景：`git commit -m "…prompts.py / support.py / … .venv/bin/python …"`
整条命令被安全层拦下，报错"访问工作区之外的绝对路径（/）"。
本探针逐项复现检测器，确认是哪条规则误伤。
"""

from wovra.tools.safety import (
    _command_escape,
    _linked_outside,
    _outside_absolute_paths,
)

CASES = [
    # 疑似元凶 1：前后带空格的孤立 `/`（文本分隔符）
    "prompts.py / support.py / tools/__init__.py",
    # 疑似元凶 2：message 文本里的 .venv/bin/python（界内指向界外的链接）
    'git commit -m "run_command 拦截 .venv/bin/python 时提示"',
    # 对照：真实要拦的根目录访问
    "find / -name x",
    "ls -la /",
    "rm -rf /tmp/x",
    # 对照：纯文本分隔符（不该拦）
    "echo a / b",
    # 对照：正常提交消息
    'git commit -m "fix: 修 bug"',
]

for c in CASES:
    print(f"命令: {c!r}")
    print(f"  _outside_absolute_paths -> {_outside_absolute_paths(c)}")
    print(f"  _linked_outside        -> {_linked_outside(c)}")
    print(f"  _command_escape        -> {_command_escape(c)!r}")
