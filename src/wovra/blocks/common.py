"""Block 共用底座：工具集合、命令分类、调用解析等微工具。

被 segment / labels / digest 共享；本模块不 import 包内其它模块。
"""
import json
import re

WRITE_TOOLS = ("write_file", "edit_file")

READ_TOOLS = ("read_file",)

FILE_OP_TOOLS = {
    "read_file": "read",
    "write_file": "write",
    "edit_file": "edit",
    "replace_lines": "edit",
    "delete_file": "delete",
}

_TAG_RULES = (
    ("environment", re.compile(
        r"pip3?\s+install|uv\s+(add|pip|sync|venv|init|tool)|npm\s+(install|\bi\b|\bci\b)"
        r"|yarn\s+add|pnpm\s+(add|i\b)|conda\s+(install|create|activate)"
        r"|apt(-get)?\s+(install|update)|brew\s+install|cargo\s+add"
        r"|python[\d.]*\s+-m\s+venv|virtualenv|requirements\.txt|export\s+\w+=")),
    ("test", re.compile(
        r"pytest|py\.test|unittest|npm\s+(run\s+)?test|yarn\s+test|pnpm\s+test"
        r"|jest|vitest|node\s+--test|go\s+test|cargo\s+test|\btox\b")),
    ("build", re.compile(
        r"npm\s+run\s+build|yarn\s+build|pnpm\s+build|\bgcc\b|\bg\+\+|clang"
        r"|\bmake\b|cmake|cargo\s+build|go\s+build|\btsc\b|py_compile|webpack"
        r"|esbuild|vite\s+build|\bmvn\b|gradle")),
    ("environment", re.compile(
        r"uvicorn|gunicorn|flask\s+run|http\.server|npm\s+run\s+dev|npm\s+start"
        r"|yarn\s+(dev|start)|pnpm\s+dev|\bvite\b|next\s+dev|live-server"
        r"|runserver|artisan\s+serve")),
    ("file", re.compile(
        r"\b(mkdir|rmdir|cp|mv|rm|touch|ls|dir|cat|head|tail|ln|chmod|chown"
        r"|tar|unzip|zip|gzip|grep|rg|find|findstr|sed|awk|wc|diff)\b")),
    ("run", re.compile(
        r"\bpython[\d.]*\b|\bnode\b|\bdeno\b|\bbun\b|\bruby\b|\bjava\b|go\s+run"
        r"|cargo\s+run|\bbash\b|\bzsh\b|\bsh\b|\./|powershell")),
)

def tag_command(command: str) -> str:
    """shell 命令 → 六类标签（零 LLM）。

    按 _TAG_RULES 顺序首个命中即返回，全不命中归 "other"（git/curl/
    echo 等杂项）。复合命令（&&/;）整串匹配——安装、测试这类强信号
    优先，宁可高估环境属性也不漏隔离。
    """
    for tag, pattern in _TAG_RULES:
        if pattern.search(command or ""):
            return tag
    return "other"

def _call_info(call: dict) -> tuple[str, dict]:
    """tool_call → (工具名, 参数 dict)；参数解析失败按空参处理。"""
    fn = call.get("function") or {}
    try:
        args = json.loads(fn.get("arguments") or "{}")
    except json.JSONDecodeError:
        args = {}
    if not isinstance(args, dict):
        args = {}
    return str(fn.get("name") or ""), args

def _remember(block: dict, key: str, value: str) -> None:
    """去重保序地记录一个值（文件路径 / 命令标签）。"""
    if value and value not in block[key]:
        block[key].append(value)

def _head(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit] + "…"
