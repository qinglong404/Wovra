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

# 维护性调用的 purpose 集合：这些调用的成本与延迟**不摊给任何一轮**
# （它们异步/在轮边界跑，混进轮账会漏记或错记）。
_MAINTENANCE_PURPOSES = ("organization", "compaction", "split", "note")

_READ_ONLY_TOOLS = frozenset(
    {"read_file", "search_files", "list_files", "get_current_time",
     "glob_files", "web_fetch", "web_search", "list_background"}
)


def _env_int(name: str, default: int) -> int:
    """调用期读环境变量（非法值退回默认）——`WOVRA_*` 一律走这几个读取器。

    现读而不是 import 期快照：前端"配置"面板改完写进 `.env` 并同步进运行中
    进程的环境，**下一轮**（新建 Agent / 下一次触发判定）就该用新值。
    """
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(float(raw))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def context_limit() -> int:
    """该 agent 的模型上下文窗口（tok）。"""
    return _env_int("WOVRA_CONTEXT_LIMIT", 1000000)


def v4_enabled() -> bool:
    """V4 行为开关（默认开）。

    三件事一起生效：① **取消重组**（所有 agent 看同一份共享历史，不再按域重切）；
    ② **整理停用**（每轮的叙事由轮闭合处的 note 承担，org 那一路不再跑）；
    ③ **分裂按活性文件画树**（不再带块地图/块归属）。
    `WOVRA_V4=0` 退回旧链路（对照与回滚用）。
    """
    return (os.environ.get("WOVRA_V4", "1") or "1").strip().lower() not in (
        "0", "false", "no", "off",
    )


def compress_threshold() -> float:
    """窗口保底折叠阈值（占窗口比例）。"""
    return _env_float("WOVRA_COMPRESS_THRESHOLD", 0.8)


def org_watermark() -> int:
    """整理水位（tok）：到线触发结算/分裂/折档。"""
    return _env_int("WOVRA_ORG_WATERMARK", 100000)


def org_grace_rounds() -> int:
    """会话前 N 轮硬豁免维护。"""
    return _env_int("WOVRA_ORG_GRACE_ROUNDS", 3)


def org_cooldown_rounds() -> int:
    """两次维护之间的最小轮距。"""
    return _env_int("WOVRA_ORG_COOLDOWN_ROUNDS", 3)


def note_timeout() -> float:
    """一批结算的硬上限（秒）；超时按失败处理、只留痕。"""
    return _env_float("WOVRA_NOTE_TIMEOUT", 180)


def note_batch_max() -> int:
    """一批结算最多写多少轮的产物（超了切几次调用）。"""
    return _env_int("WOVRA_NOTE_BATCH_MAX", 12)


def fold_target() -> float:
    """折到水位这个比例之下（一次折够 → 下次换档要等重新长上来）。"""
    return _env_float("WOVRA_FOLD_TARGET", 0.6)


def org_maint_timeout() -> float:
    """整理/分裂那一路的硬上限（秒）。"""
    return _env_float("WOVRA_MAINT_TIMEOUT", 900)


def max_route_hops() -> int:
    """一个用户回合内允许的转交跳数。"""
    return _env_int("WOVRA_ROUTE_HOPS", 3)


def image_view_soft() -> int:
    """看图软线（本回合 view_image 次数）：到线把"该收敛了"写进工具结果。"""
    return _env_int("WOVRA_IMAGE_VIEWS", 6)


def image_view_hard() -> int:
    """看图硬线：到线拒绝本次调用（不注入新图）。"""
    return _env_int("WOVRA_IMAGE_VIEWS_MAX", 12)


def state_render_budget() -> int:
    """任务状态渲染的字符预算（每轮都进上下文的那一段）。"""
    return _env_int("WOVRA_STATE_BUDGET", 8000)


def max_turns() -> int:
    """一个用户回合内的最大步数。"""
    return _env_int("WOVRA_MAX_TURNS", 200)



def image_converge_note(count: int, hard: int) -> str:
    """软线提示：把"该下结论了"写进工具结果（模型看得见，且随轮落盘）。"""
    return (f"[看图预算] 本回合 view_image 已调用 {count} 次（硬上限 {hard}）。"
            f"反复裁剪/放大同一张图的边际收益很低——现在应基于**已看过的**图像"
            f"下结论并标注不确定处，或改走文本路线（page_text / read_file / search_files）。")


def image_budget_refusal(count: int, hard: int) -> str:
    """硬线拒绝：本次调用不执行（不注入新图），要求就地收口。"""
    return (f"[看图预算用尽] 本回合 view_image 已调用 {count} 次（上限 {hard}），"
            f"本次**未执行**、没有新图像注入。请基于已看过的图像给出结论："
            f"最有把握的答案 + 明确标注不确定的部分；确需再看图的，改用文本工具"
            f"（page_text / read_file / search_files），或把问题交回用户。")

# 会**消耗视觉模型**的工具。`screenshot` 只往盘上写 PNG、本身不注入图像
# （要看到还得再 view_image），所以它不在预算里——挡它挡不住看图循环。
_VISION_TOOLS = frozenset({"view_image"})

# 维护调用（org/split）是否把 tools 收窄为单一出口工具。
# 默认 False = 用**与工作对话完全相同的** tools 数组（缓存复议结论，
# 2026-09-11）：
#   * 收窄的代价经实测确认是真的——org 首跳 prompt=215,940/cached=896
#     （命中 0.4%）、split 首跳 290,142/896（0.3%）；每次维护都按全量
#     未命中计费（1 元/M 而非命中价 0.02 元/M），约 0.6 元/批。
#   * 收窄的收益经实测确认**不成立**：维护调用根本不执行工具（只捕获
#     提交参数），漂移的唯一后果是"这批没产物"，而现在有带诊断重发兜底；
#     且 R8 污染复现里工具已只剩 submit_organization，模型照样调用了
#     不存在于工具集的 check_background——收窄连"防跑偏"都没防住。
# 设 WOVRA_MAINT_NARROW_TOOLS=1 可切回收窄（对照实验/回滚用）。
_MAINT_NARROW_TOOLS_ENV = "WOVRA_MAINT_NARROW_TOOLS"


def maint_tools(schemas: list[dict], submit_name: str) -> list[dict]:
    """维护调用使用的 tools 数组。

    默认返回**完整** schema 列表——与工作调用同一序列化，前缀缓存才能
    从 system 一直骑到整理指令之前（tools 是前缀的一部分，数组一差分叉
    即整段未命中）。收窄模式（env 开关）只留单一出口工具。
    """
    value = (os.environ.get(_MAINT_NARROW_TOOLS_ENV) or "").strip().lower()
    if value in ("1", "true", "yes", "on"):
        return [
            s for s in schemas
            if s.get("function", {}).get("name") == submit_name
        ]
    return list(schemas)


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
    "expand_history": "检索/展开历史",
    "run_background": "后台启动命令",
    "check_background": "查看后台输出",
    "stop_background": "停止后台任务",
    "glob_files": "按模式找文件",
    "web_fetch": "抓取网页",
    "web_search": "网页搜索",
    "web_automate": "云浏览器自动化（TinyFish）",
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
