"""Event 的 Runtime 截断器：零 LLM 成本的信息概览。

职责边界（设计稿第 3/4/14 节）：

* Event 的 Full = 原始协议消息，永不修改，是事实来源；
* Truncated = 本模块**用代码**生成的低概览，不调用 LLM——
  它回答"这里发生过什么"，不试图理解复杂语义；
* 超大 Event 触发 Context Safety Limit：Full 完整保存，
  上下文只放"输出过大 + 事件 ID"，细节靠 expand_history 找回。

截断优先保留：事件类型、动作、对象、结果、状态、关键错误。
"""

from datetime import datetime
from typing import Any

# 单条工具结果进入上下文的安全阈值（字符）：超过则 Full 只存事件、
# 上下文放"输出过大 + 事件 ID"。这是 Safety Limit，不是压缩——
# 正常长度的结果不受影响
SAFE_RESULT_LIMIT = 2000

# Truncated 视图里单条事件预览的默认长度（与报告时间线口径一致）
TRUNCATED_LIMIT = 120

# 工具结果失败标记：与 task/ui 共用一套判定（tools.FAILURE_MARKERS）
from .tools import FAILURE_MARKERS  # noqa: E402


def _head(text: str, limit: int = TRUNCATED_LIMIT) -> str:
    """折叠空白后取前 limit 字符——所有 Truncated 的兜底形状。"""
    collapsed = " ".join((text or "").split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[:limit] + "…"


def _tool_result_status(content: str, name: str) -> str:
    """提取工具结果的成败状态（供 Event.status 与截断文本使用）。"""
    if any(marker in content for marker in FAILURE_MARKERS):
        return "error"
    return "ok"


def _truncate_tool_result(name: str, content: str) -> tuple[str, str]:
    """工具结果的截断规则：优先保留结果、状态、关键错误。

    返回 (status, truncated)。通用规则 + 少量专用规则
    （专用规则按工具名注册在这里，V1 先覆盖 run_command）。
    """
    status = _tool_result_status(content, name)

    if name == "run_command":
        # 结构固定：第一行是 exit_code 或失败头，随后 stdout/stderr
        first_line, _, rest = content.partition("\n")
        error_lines = [
            line
            for line in rest.splitlines()
            if any(k in line.lower() for k in ("error", "failed", "traceback", "no such", "denied"))
        ]
        parts = [_head(first_line, 160)]
        if error_lines:
            parts.append("关键错误：" + _head(error_lines[0], 120))
        return status, " ".join(parts)

    # 通用规则：成败 + 头部预览
    prefix = "失败，" if status == "error" else ""
    return status, f"{prefix}{_head(content)}"


def make_event(
    event_id: str,
    type: str,  # noqa: A002——对外术语就是 type，保持与设计稿一致
    message: dict[str, Any],
    tool_name: str = "",
) -> dict:
    """Event 工厂：从协议消息生成完整的事件记录。

    * message：OpenAI 协议消息 dict，原样保存为 Full（事实来源）
    * truncated：Runtime 规则生成，零 LLM 成本
    * 超大消息触发 Safety Limit：message.content 被替换为
      "输出过大 + 事件 ID"，完整原文只存在于返回值的 full 字段
    """
    content = message.get("content") or ""

    if type == "user":
        truncated = _head(content)
        status = ""
    elif type == "tool_call":
        calls = "; ".join(
            f"{tc['function']['name']}({tc['function']['arguments'][:80]})"
            for tc in message.get("tool_calls", [])
        )
        truncated = _head(f"调用 {calls}")
        status = ""
    elif type == "tool_result":
        status, truncated = _truncate_tool_result(tool_name, content)
    elif type == "final_answer":
        truncated = _head(content)
        status = ""
    else:  # assistant / system_info 等
        truncated = _head(content)
        status = ""

    event = {
        "id": event_id,
        "type": type,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "status": status,
        "truncated": truncated,
        "message": message,
    }

    # Context Safety Limit：超大内容不直接进上下文，
    # 协议消息里只留安全范围 + 指回 Full 的引用
    if type == "tool_result" and len(content) > SAFE_RESULT_LIMIT:
        event["full"] = content
        safe = (
            content[:SAFE_RESULT_LIMIT]
            + f"\n[输出过大（{len(content)} 字符）已截断。完整结果：{event_id}，"
            f"可用 expand_history(level=\"full\") 查看]"
        )
        event["message"] = {**message, "content": safe}
    return event


def event_index_line(event: dict) -> str:
    """渲染一条事件的截断索引行（带 ID，供 expand_history 定位）。"""
    status = f"[{event['status']}] " if event.get("status") else ""
    return f"[{event['id']}] {status}{event['truncated']}"


def render_round_events(round_data: dict, limit: int | None = None) -> str:
    """把一个 Round 的事件渲染成 Truncated 流（Organization 的输入）。"""
    lines = [event_index_line(e) for e in round_data.get("events", [])]
    if limit:
        lines = lines[-limit:]
    return "\n".join(lines)
