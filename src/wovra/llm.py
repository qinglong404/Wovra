"""LLM 客户端：Wovra 中所有与模型交互的代码都统一经过这里。

统一收口的原因：换模型、换服务商、调整默认参数时只需要改这一个文件，
而不是散落在各个组件里。这也是 README 中 "Task Manager 之下复用
现有能力" 思路的一部分——模型调用是最基础的底层能力。
"""

import os
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv
from openai import (
    APIConnectionError,
    AuthenticationError,
    NotFoundError,
    OpenAI,
    PermissionDeniedError,
)


def _normalize_proxy_schemes() -> None:
    """把 httpx 不认识的 `socks://` 代理协议名改写为 `socks5://`。

    常见代理工具（clash 等）导出的环境变量用 `socks://` 这个
    非 URL 标准的协议名，httpx 构造客户端时会直接抛
    "Unknown scheme for proxy URL"，导致 wovra 崩溃。
    在模块导入时归一化，保证发生在任何 OpenAI 客户端构造之前；
    实际的 socks 转发能力由 socksio 依赖提供。
    """
    for name in (
        "ALL_PROXY", "all_proxy",
        "HTTP_PROXY", "http_proxy",
        "HTTPS_PROXY", "https_proxy",
    ):
        value = os.environ.get(name, "")
        if value.startswith("socks://"):
            os.environ[name] = "socks5://" + value[len("socks://"):]


_normalize_proxy_schemes()

# .env 约定放在项目根目录（本文件位于 src/wovra/，向上三级即根目录）。
# 包被安装到别处时该路径不存在，load_dotenv 会静默跳过，不影响运行；
# 真正的部署环境应当用真实的环境变量注入配置。
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
load_dotenv(dotenv_path=_PROJECT_ROOT / ".env")


def reasoning_of(part) -> str:
    """兼容地取出思考内容（reasoning content）。

    思考内容不是 OpenAI 协议的标准字段，各服务以扩展字段
    `reasoning_content` 下发；SDK 没有为它生成属性时，会落进
    pydantic 的 model_extra。message 和流式 delta 都适用。
    """
    return getattr(part, "reasoning_content", None) or (
        (getattr(part, "model_extra", None) or {}).get("reasoning_content") or ""
    )


class LLMConfigError(RuntimeError):
    """模型服务接入配置错误（BASE_URL / 模型名 / 密钥 / 网络）。

    这类错误用户自己就能修，报错必须直说"检查哪里"，而不是甩一屏
    SDK traceback；原始异常通过 __cause__ 保留，排查时仍可见。
    """


def _raw_detail(error: Exception) -> str:
    """压成单行的原始错误信息（截断），拼在友好提示后面供排查。"""
    text = " ".join(str(error).split())
    return text[:300] + ("…" if len(text) > 300 else "")


def _config_hint(error: Exception, model: str, base_url: Optional[str]) -> str:
    """按错误类型给出"检查哪里"的提示，全部指向 .env 里的配置项。"""
    where = base_url or "OpenAI 官方地址"
    if isinstance(error, NotFoundError):
        return (
            f"模型配置问题：端点 {where} 上不存在模型 {model}，或当前密钥无权访问。\n"
            "请检查 .env：Wovra_BASE_URL 与 Wovra_MODEL 必须配套——不同服务商的"
            "模型名不通用（例如火山方舟上要填方舟的模型 ID，GLM 要配智谱的端点）。"
        )
    if isinstance(error, AuthenticationError):
        return (
            f"模型配置问题：密钥无效或已过期（{where} 返回 401）。"
            "请检查 .env 中的 Wovra_API_KEY。"
        )
    if isinstance(error, PermissionDeniedError):
        return (
            f"模型配置问题：当前密钥无权访问模型 {model}（{where} 返回 403）。"
            "部分模型需要单独开通或升级付费。"
        )
    return (
        f"模型配置问题：无法连接到 {where}。"
        "请检查网络、代理，以及 Wovra_BASE_URL 的域名与路径。"
    )


class LLM:
    """对 OpenAI 协议客户端的薄封装。

    只做三件事：读取配置、持有客户端、转发调用。
    刻意不做流式封装、不做重试——阶段 1 保持最小，够用就好。
    配置类错误（端点/模型/密钥/网络）翻译成 LLMConfigError 给出
    可操作的提示——"薄"不等于把 SDK 原始报错原样甩给用户。
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
    ) -> None:
        # 参数优先，其次环境变量（.env 已在上面加载进环境），最后兜底默认值。
        # 显式传入 > .env > 默认 的顺序让测试和临时换模型都很方便。
        self.model = model or os.environ.get("Wovra_MODEL", "gpt-4o-mini")
        api_key = api_key or os.environ.get("Wovra_API_KEY", "")
        base_url = base_url or os.environ.get("Wovra_BASE_URL")

        if not api_key:
            raise RuntimeError(
                "未配置 API 密钥。请在项目根目录 .env 中填写 Wovra_API_KEY，"
                "或通过 LLM(api_key=...) 传入。"
            )

        # base_url 允许为 None：此时 SDK 使用 OpenAI 官方地址。
        self.base_url = base_url
        self._client = OpenAI(api_key=api_key, base_url=base_url)

    def chat(self, messages: list[dict], tools: Optional[list[dict]] = None,
             stream: bool = False, **kwargs: Any):
        """发送一次对话补全请求；stream=True 时返回分块迭代器。

        返回原始对象/分块而不是只返回文本：工具调用场景需要访问
        tool_calls、usage 等细节，封装掉反而碍事。
        tools=None 时 SDK 会自动省略该参数，不影响普通对话。
        """
        if stream:
            # 流式默认要求服务端在最后一个分块附带 usage 统计
            # （OpenAI 协议扩展 stream_options，主流兼容服务都支持）。
            # 没有它，流式调用就拿不到 tokens 数，成本核算无从谈起。
            kwargs.setdefault("stream_options", {"include_usage": True})
        # 可选参数缺省时不发给服务端（工具禁用 = 整个字段省略，而非 null）：
        # 严格端点对 tools=null / stream=null 这类空值可能直接 400
        payload: dict[str, Any] = {"model": self.model, "messages": messages}
        if stream:
            payload["stream"] = True
        if tools:
            payload["tools"] = tools
        try:
            return self._client.chat.completions.create(**payload, **kwargs)
        except (NotFoundError, AuthenticationError, PermissionDeniedError, APIConnectionError) as error:
            raise LLMConfigError(
                f"{_config_hint(error, self.model, self.base_url)}\n"
                f"服务端原始信息：{_raw_detail(error)}"
            ) from error
