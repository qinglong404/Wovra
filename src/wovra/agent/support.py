"""Agent 支撑层：运行常量、无状态工具函数（schema 生成/信封/JSON 解析）。

零逻辑搬运（2026-09-11 自原单文件 agent.py 切出，拆分后归属本模块）。
所有名字对包内各 mixin 公开；`_schema_of` 亦经包 __init__ 再导出
（测试与 scripts 直接引用）。
"""

import inspect
import os
from typing import Callable

from ..task import sanitize_surrogates


MODE_MANAGED = "managed"

MODE_BASELINE = "baseline"

_DEFAULT_CONTEXT_LIMIT = int(os.environ.get("WOVRA_CONTEXT_LIMIT", "1000000"))

_COMPRESS_THRESHOLD = float(os.environ.get("WOVRA_COMPRESS_THRESHOLD", "0.8"))

_MAINTENANCE_PURPOSES = ("organization", "compaction", "split")

_READ_ONLY_TOOLS = frozenset(
    {"read_file", "search_files", "list_files", "get_current_time",
     "glob_files", "web_fetch", "web_search", "list_background"}
)

_ORG_GRACE_ROUNDS_DEFAULT = int(os.environ.get("WOVRA_ORG_GRACE_ROUNDS", "3"))

_ORG_COOLDOWN_ROUNDS_DEFAULT = int(os.environ.get("WOVRA_ORG_COOLDOWN_ROUNDS", "3"))

_ORG_WATERMARK_DEFAULT = int(os.environ.get("WOVRA_ORG_WATERMARK", "100000"))

# 分裂判据的体量门槛（2026-09-11 用户拍板）：主 agent 残留桶（纯对话/
# 未入域块）与域并列构成顶层节点；但其占比 ≤ 该值（≈15K @ 100K 水位）
# 时不拆出——并入主 agent、不计入节点数。纯聊天单独拆出去不合理。
_SPLIT_CHAT_MERGE_RATIO = float(os.environ.get("WOVRA_SPLIT_CHAT_RATIO", "0.15"))

_ORG_MAINT_TIMEOUT_DEFAULT = float(os.environ.get("WOVRA_MAINT_TIMEOUT", "900"))

_DEFAULT_MAX_TURNS = int(os.environ.get("WOVRA_MAX_TURNS", "200"))

_ACTION_WORDS = {
    "write_file": "写入文件",
    "edit_file": "修改文件",
    "replace_lines": "按行替换文件",
    "delete_file": "删除文件",
    "move_file": "移动文件",
    "restore_file": "回滚文件版本",
    "run_command": "运行命令",
    "read_file": "读取文件",
    "search_files": "搜索内容",
    "list_files": "查看目录",
    "get_current_time": "获取当前时间",
    "expand_history": "展开历史",
    "run_background": "后台启动命令",
    "check_background": "查看后台输出",
    "stop_background": "停止后台任务",
    "glob_files": "按模式找文件",
    "web_fetch": "抓取网页",
    "web_search": "网页搜索",
    "ask_user": "询问用户",
    "list_background": "列出后台任务",
}

_JSON_TYPES = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
}

def _clip_quote(text: str, limit: int = 60) -> str:
    """用户原话锚点：压平换行并截断，供分块地图每轮首行展示。"""
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[:limit] + "…"

def _sanitize_json_strings(obj):
    """递归清洗结构里的未配对代理项（见 task.sanitize_surrogates）。"""
    if isinstance(obj, str):
        return sanitize_surrogates(obj)
    if isinstance(obj, list):
        return [_sanitize_json_strings(v) for v in obj]
    if isinstance(obj, dict):
        return {k: _sanitize_json_strings(v) for k, v in obj.items()}
    return obj

def _action_word(name: str) -> str:
    """工具名 → 进度提示的动作词（未知工具退回"调用 xxx"）。"""
    return _ACTION_WORDS.get(name, f"调用 {name}")

def _runtime_reminder(text: str) -> dict:
    """运行时专属注入通道（zcode-borrowings.md 1.1）。

    机制信息（任务状态、文件地图、运行时意志）用 <runtime-reminder>
    信封包裹后以 user-role 注入——OpenAI 协议没有独立的 reminder role，
    用"信封 + 系统提示词声明"实现身份可辨：模型分得清这是运行时
    在说话，不是用户在发言。
    """
    return {"role": "user", "content": f"<runtime-reminder>\n{text}\n</runtime-reminder>"}

def _schema_of(fn: Callable) -> dict:
    """根据函数签名自动生成 OpenAI tools 协议要求的 JSON Schema。"""
    properties = {}
    for name, param in inspect.signature(fn).parameters.items():
        annotation = param.annotation
        # Optional[X]（X | None）取 X 的类型，避免退化为 string
        args_ = getattr(annotation, "__args__", None)
        if args_ and type(None) in args_:
            non_none = [a for a in args_ if a is not type(None)]
            if len(non_none) == 1:
                annotation = non_none[0]
        json_type = _JSON_TYPES.get(annotation, "string")
        properties[name] = {"type": json_type}

    doc = inspect.getdoc(fn)
    # 描述取首段（空行分隔、折叠空白）：关键使用约束（如 run_command
    # 的超时与常驻服务警告）往往一行装不下，首段才能完整送达模型
    description = (
        " ".join(doc.split("\n\n")[0].split()) if doc else fn.__name__
    )

    return {
        "type": "function",
        "function": {
            "name": fn.__name__,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": [
                    name
                    for name, p in inspect.signature(fn).parameters.items()
                    if p.default is inspect.Parameter.empty
                ],
            },
        },
    }
