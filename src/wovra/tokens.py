"""提示词 token 的分类估算（成本核算的底层）。

API 只返回 prompt_tokens 总数，不告诉你花在了哪里。要做分类核算，
只能本地估算各部分占比，再按真实总量校准：

    估算各类目的相对占比 → 按服务端返回的真实 prompt_tokens 等比缩放

估算器优先用 tiktoken（OpenAI 官方分词器；对英文准确、对中文只是近似，
但对 GLM 来说"比例"仍然远比拍脑袋可靠）；tiktoken 不可用（未安装/
离线且无本地缓存）时，退化为字符启发式：中文按 1 字 ≈ 1 token、
其他按 4 字符 ≈ 1 token。

六个类目（参考业界通行分法）：
    system     系统提示词（调用方传入的 system_prompt）
    context    注入内容（任务上下文，来自磁盘的任务状态）
    tools      工具定义（schemas，每个任务都固定开销）
    user       用户消息
    assistant  助手消息（含 tool_calls 的参数 JSON）
    tool       工具结果
"""

import json
import os
import unicodedata

# 缓存命中 token 的计价折扣（命中率 → 等效输入的换算系数）。
# 各服务商差异巨大：DeepSeek 约 1/30，GLM-5.3-flash 约 1/3.5
# （命中 0.115 / 未命中 0.4 元/M）。用 WOVRA_CACHE_RATE 覆盖，
# 默认 30（历史口径）。折扣越浅，基座税越重——这是成本模型的关键参数。
CACHE_RATE = float(os.environ.get("WOVRA_CACHE_RATE", "30"))

# tiktoken 词表必须延迟到首次使用才加载：get_encoding 在本地无缓存时
# 会联网下载（openaipublic CDN）——放模块顶层意味着新机器/离线/网络
# 受限环境下 import wovra 就卡在网络超时上，整个 CLI 无法启动。
# 首次 estimate() 时才尝试，失败退化为启发式，代价只是那次调用慢一点。
# 设 WOVRA_TOKENIZER=heuristic 可彻底跳过 tiktoken（air-gapped 环境）。
_encoding = None
_encoding_checked = False


def _get_encoding():
    """惰性获取 tiktoken 词表；只探测一次，之后沿用结果。"""
    global _encoding, _encoding_checked
    if not _encoding_checked:
        _encoding_checked = True
        if os.environ.get("WOVRA_TOKENIZER", "").strip().lower() not in (
            "heuristic",
            "none",
            "off",
        ):
            try:
                import tiktoken

                # cl100k_base 首次使用需要下载词表；失败则走启发式
                _encoding = tiktoken.get_encoding("cl100k_base")
            except Exception:  # noqa: BLE001——任何失败都只意味着"退化为估算"
                _encoding = None
    return _encoding

# 类目标识符 → 中文标签（ui 层展示用）
LABELS = {
    "system": "系统提示词",
    "context": "注入内容",
    "tools": "工具定义",
    "user": "用户消息",
    "assistant": "助手消息",
    "tool": "工具结果",
}

CATEGORIES = tuple(LABELS)


def estimate(text: str) -> int:
    """估算一段文本的 token 数。"""
    if not text:
        return 0
    encoding = _get_encoding()
    if encoding is not None:
        return len(encoding.encode(text))
    # 启发式：全角字符（中日韩）按 1 字 1 token，其余按 4 字符 1 token，
    # 向上取整（非空文本至少 1 token）
    import math

    wide = sum(1 for ch in text if unicodedata.east_asian_width(ch) in "WF")
    return max(1, math.ceil(wide + (len(text) - wide) / 4))


def breakdown(
    system_prompt: str,
    task_context: str,
    tool_schemas: list,
    messages: list,
) -> dict[str, int]:
    """把一次调用的提示词按类目拆开，返回各类目的估算 token 数。

    system 和 context 不从 messages 里取：Agent 构造时把两者拼进了
    system 消息，只有它自己知道边界在哪，所以单独传入。
    """
    result = dict.fromkeys(CATEGORIES, 0)
    result["system"] = estimate(system_prompt)
    result["context"] = estimate(task_context)
    if tool_schemas:
        result["tools"] = estimate(json.dumps(tool_schemas, ensure_ascii=False))

    for message in messages:
        role = message.get("role")
        if role == "system":
            continue  # 已按 system/context 单独计过，避免重复
        if role == "user":
            result["user"] += estimate(message.get("content") or "")
        elif role == "tool":
            result["tool"] += estimate(message.get("content") or "")
        elif role == "assistant":
            # 助手消息的 token 既有正文，也有 tool_calls 的参数 JSON
            result["assistant"] += estimate(message.get("content") or "")
            for call in message.get("tool_calls") or []:
                result["assistant"] += estimate(
                    json.dumps(call.get("function", {}), ensure_ascii=False)
                )
    return result
