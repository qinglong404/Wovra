"""Wovra 内置工具箱：文件读写、命令执行、审计与防护。

设计原则——"审计换权限"：

    * 能力上，agent 从"只读研究员"升级为能产出、能执行的执行者；
    * 代价上，每个变更类工具的完整内容都会被 Agent 记入任务历史
      （谁、何时、对哪个文件、做了什么、内容是什么），可追溯；
    * 破坏性操作不做"确认弹窗"（CLI 场景做不到良好交互），
      而是直接硬拒绝——宁可让模型换一种做法，也不赌运气。

所有路径类工具都通过 _safe_path 限制在项目根目录内，
防止模型读写项目之外的任何东西。
"""

import json
import subprocess
from pathlib import Path

# 项目根目录（本文件位于 src/wovra/，向上三级）
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# 变更类工具的集合：Agent._execute 会对这些工具做专门的审计记录
AUDITED_TOOLS = {"write_file", "edit_file", "run_command"}

# 工具结果中的失败标记：ui（红色显示）和 task 报告（"失败："前缀）
# 共用这一份，判定口径保持一致
FAILURE_MARKERS = (
    "工具执行出错",
    "未知工具",
    "合法 JSON",
    "已拒绝执行危险命令",
    "命令执行失败（",
)

# 破坏性命令黑名单：子串匹配，宁可误杀不可放过。
# 有意保持保守——rm -r、git push 这类即使"看起来安全"也拒绝，
# 模型收到拒绝文本后会自行寻找替代方案（这是流式循环的好处）。
_DENIED_PATTERNS = (
    "rm -r",          # 递归删除（含 -rf/-fr）
    "sudo ",
    "mkfs",
    "dd if=",
    ":(){",           # fork 炸弹
    "git push",       # 对外发布，不由 agent 自主决定
    "git reset --hard",
    "git clean",
    "git checkout -- ",
    "git restore",
    "shutdown",
    "reboot",
    "chmod -R",
    "| sh",
    "| bash",
    "|zsh",
    "| sh;",
    "curl ",          # 下载外部内容（配合管道执行是常见攻击面），一律拒绝
    "wget ",
)

_COMMAND_TIMEOUT = 60  # 秒


def _safe_path(relative: str) -> Path:
    """把相对路径解析到项目根目录内，越界直接报错。"""
    path = (PROJECT_ROOT / relative).resolve()
    if not path.is_relative_to(PROJECT_ROOT):
        raise ValueError(f"路径越界，只允许访问项目目录内的文件: {relative}")
    return path


# ---- 只读工具 -------------------------------------------------------------


def list_files(directory: str = ".") -> list[str]:
    """列出项目内某个目录下的文件和子目录（不含递归）。"""
    path = _safe_path(directory)
    return sorted(p.name + ("/" if p.is_dir() else "") for p in path.iterdir())


def read_file(path: str) -> str:
    """读取项目内一个文本文件的内容（最多前 4000 字符）。"""
    target = _safe_path(path)
    text = target.read_text(encoding="utf-8")
    return text[:4000] + ("\n...(已截断)" if len(text) > 4000 else "")


def get_current_time() -> str:
    """获取当前本地时间（ISO 格式）。"""
    from datetime import datetime

    return datetime.now().isoformat(timespec="seconds")


# ---- 变更类工具（AUDITED_TOOLS，Agent 会做完整审计记录） --------------------


def write_file(path: str, content: str) -> str:
    """创建或整体覆盖项目内的一个文本文件。

    覆盖是全量的——只改一部分请用 edit_file，它要求唯一定位，
    误伤面小得多。
    """
    target = _safe_path(path)
    existed = target.exists()
    # 允许写到尚不存在的子目录（模型经常给出 "reports/xx.md" 这类路径）
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    action = "覆盖" if existed else "创建"
    return f"已{action} {path}（{len(content)} 字符）"


def edit_file(path: str, old_text: str, new_text: str) -> str:
    """把文件中「恰好出现一次」的 old_text 替换为 new_text。

    强制唯一定位：找不到或出现多次都直接报错，让模型补充更多
    上下文再试。这是防止"替换了不想替换的地方"的关键约束。
    """
    target = _safe_path(path)
    text = target.read_text(encoding="utf-8")
    count = text.count(old_text)
    if count == 0:
        raise ValueError(f"{path} 中未找到待替换文本（前 80 字符: {old_text[:80]!r}）")
    if count > 1:
        raise ValueError(
            f"{path} 中待替换文本出现 {count} 次，请补充前后文使其唯一定位"
        )
    target.write_text(text.replace(old_text, new_text, 1), encoding="utf-8")
    return f"已修改 {path}（{len(old_text)} 字符 → {len(new_text)} 字符）"


def run_command(command: str) -> str:
    """在项目根目录运行一条 shell 命令，返回退出码与输出。

    防护：
        * 黑名单匹配到破坏性模式时直接拒绝，不执行；
          拒绝文本会回传给模型（它能看到原因并换方案）
        * 60 秒超时强制终止，防止长命令卡死整个任务
        * 输出各截断 1500 字符，防止超长输出撑爆上下文
    """
    for pattern in _DENIED_PATTERNS:
        if pattern in command:
            return (
                f"已拒绝执行危险命令：包含被禁止的模式 `{pattern}`。"
                f"如需完成类似效果，请使用更安全的替代方案。"
            )

    try:
        proc = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=_COMMAND_TIMEOUT,
            cwd=PROJECT_ROOT,  # 固定工作目录：相对路径都在项目内
        )
    except subprocess.TimeoutExpired:
        return f"命令执行失败（超时 {_COMMAND_TIMEOUT} 秒被强制终止）：{command[:200]}"

    stdout = (proc.stdout or "").strip() or "(无输出)"
    stderr = (proc.stderr or "").strip() or "(无输出)"
    header = (
        f"命令执行失败（exit_code={proc.returncode}）"
        if proc.returncode != 0
        else f"exit_code=0"
    )
    return (
        f"{header}\n"
        f"stdout:\n{stdout[:1500]}\n"
        f"stderr:\n{stderr[:1500]}"
    )
