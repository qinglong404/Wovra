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
    APIError,
    AuthenticationError,
    NotFoundError,
    OpenAI,
    PermissionDeniedError,
)


class LLMStreamError(RuntimeError):
    """流式迭代中途失败（服务端报错/断流/读超时），保留原始错误文本。

    选 RuntimeError 作基类：agent 主循环把它并入空响应护栏自动重试，
    重试耗尽后 CLI 的 RuntimeError 分支按"轮保持开放"收尾——无论哪条
    路，进程都不再被 openai.APIError 打崩。
    """


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

    思考内容不是 OpenAI 协议的标准字段，各家字段名不一：DeepSeek 官方
    走 `reasoning_content`（SDK 没有该属性时落进 pydantic 的 model_extra）；
    OpenRouter 系网关走 `reasoning`（实测 commandcode 网关，2026-09-12）。
    message 和流式 delta 都适用。
    """

    def _str(value) -> str:
        return value if isinstance(value, str) else ""

    return (
        _str(getattr(part, "reasoning_content", None))
        or _str((getattr(part, "model_extra", None) or {}).get("reasoning_content"))
        or _str(getattr(part, "reasoning", None))
        or _str((getattr(part, "model_extra", None) or {}).get("reasoning"))
        or ""
    )


def cached_tokens_of(usage) -> tuple[int, int]:
    """返回 (缓存命中 tok, 未命中 tok)——各服务字段位置不同。

    OpenAI 协议：prompt_tokens_details.cached_tokens；DeepSeek：顶层
    prompt_cache_hit_tokens / prompt_cache_miss_tokens（SDK 未建模时
    落在 model_extra）。miss 缺省时用 prompt - hit 兜底。
    """
    details = getattr(usage, "prompt_tokens_details", None)
    hit = getattr(details, "cached_tokens", None) or 0
    hit_ds = getattr(usage, "prompt_cache_hit_tokens", None)
    if hit_ds is None:
        hit_ds = (getattr(usage, "model_extra", None) or {}).get(
            "prompt_cache_hit_tokens"
        ) or 0
    miss_ds = getattr(usage, "prompt_cache_miss_tokens", None)
    if miss_ds is None:
        miss_ds = (getattr(usage, "model_extra", None) or {}).get(
            "prompt_cache_miss_tokens"
        ) or 0
    prompt = getattr(usage, "prompt_tokens", 0) or 0
    hit = int(max(hit or 0, hit_ds or 0))
    miss = int(miss_ds) if miss_ds else max(0, prompt - hit)
    return hit, miss


class LLMConfigError(RuntimeError):
    """模型服务接入配置错误（BASE_URL / 模型名 / 密钥 / 网络）。

    这类错误用户自己就能修，报错必须直说"检查哪里"，而不是甩一屏
    SDK traceback；原始异常通过 __cause__ 保留，排查时仍可见。
    """


def _raw_detail(error: Exception) -> str:
    """压成单行的原始错误信息（截断），拼在友好提示后面供排查。"""
    text = " ".join(str(error).split())
    return text[:300] + ("…" if len(text) > 300 else "")


def _is_bad_param(error: Exception) -> bool:
    """错误像"端点不认这个参数"吗（用于去掉可选字段重发一次）。"""
    text = str(error).lower()
    return any(k in text for k in (
        "extra_body", "reasoning_effort", "thinking", "unknown", "unrecognized",
        "unsupported", "invalid", "unexpected",
    ) and ("400" in text or "422" in text or "invalid" in text
           or "unsupported" in text or "unknown" in text))


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
        provider_id: str = "",
        reasoning: str = "",
    ) -> None:
        # 参数优先，其次会话指定的渠道商（providers.json），再次环境变量
        # （.env 已在上面加载进环境），最后兜底默认值。
        # 显式传入的（测试、临时换模型）**冻结**；没传的每次调用现读——
        # 前端改完配置或切了模型，下一轮/下一次调用就是新值。
        self._explicit_key = api_key
        self._explicit_base = base_url
        self._explicit_model = model
        # 会话级选择（task.json 里记着，切会话即切）：渠道商 id ＋ 模型 ＋ 思考强度
        self._provider_id = str(provider_id or "").strip()
        self._model_override = str(model or "").strip()
        self._reasoning = str(reasoning or "").strip()
        self.model = ""
        self.base_url = None
        self._provider = {}
        _key, _base, _timeout = self._resolve()
        if not _key:
            raise RuntimeError(
                "未配置 API 密钥。请在项目根目录 .env 中填写 Wovra_API_KEY，"
                "或在配置页添加一个模型渠道商，也可以 LLM(api_key=...) 传入。"
            )
        self._timeout = _timeout
        self._client_key: tuple | None = None
        self._client = None
        # 端点是否拒过思考强度字段（拒过就不再带，省一次 400 往返）
        self._no_reasoning = False
        self._ensure_client()

    def set_session(self, provider_id: str = "", model: str = "",
                    reasoning: str = "") -> None:
        """换会话级选择（下一轮生效）：渠道商/模型/思考强度。"""
        self._provider_id = str(provider_id or "").strip()
        self._model_override = str(model or "").strip()
        self._reasoning = str(reasoning or "").strip()
        self._ensure_client()

    def _resolve(self) -> tuple[str, Optional[str], float]:
        """(密钥, 端点, 读超时)。

        渠道商优先级：`provider_id` 指定的那条 → providers.json 的当前项 →
        `.env` 的三个键 → 默认。三者任一存在就用它，不再往后找。
        """
        from . import providers as providers_module

        entry = None
        if self._provider_id:
            entry = providers_module.get(self._provider_id)
        if entry is None:
            entry = providers_module.current()
        self._provider = entry or {}
        key = self._explicit_key or self._provider.get("api_key") or \
            os.environ.get("Wovra_API_KEY", "")
        base = self._explicit_base or self._provider.get("base_url") or \
            os.environ.get("Wovra_BASE_URL")
        if self._explicit_model:
            self.model = self._explicit_model
        elif self._model_override:
            self.model = self._model_override
        elif self._provider.get("models"):
            self.model = self._provider["models"][0]
        else:
            self.model = os.environ.get("Wovra_MODEL", "gpt-4o-mini")
        raw = (os.environ.get("WOVRA_READ_TIMEOUT") or "").strip()
        try:
            timeout = float(raw) if raw else 180.0
        except ValueError:
            timeout = 180.0
        return key, base, timeout

    def reasoning_body(self) -> dict:
        """思考强度 → 请求体附加字段（`extra_body`）；"auto"/空返回空表。

        各家字段名不同（2026-09-18 实测）：火山方舟与 GLM 走
        `thinking: {type: enabled|disabled}`；OpenAI 系走 `reasoning_effort`。
        `reasoning_field` 可在渠道商上钉死用哪家；auto 时按端点域名猜，
        猜不到就发 `reasoning_effort`（主流兼容端点都认）。
        """
        level = (self._reasoning or "auto").lower()
        if level in ("", "auto"):
            return {}
        field = str(self._provider.get("reasoning_field") or "auto").lower()
        if field == "auto":
            host = str(self.base_url or "").lower()
            field = ("thinking" if ("volces.com" in host or "bigmodel" in host
                                    or "zhipu" in host)
                     else "reasoning_effort")
        if field == "thinking":
            return {"thinking": {"type": "disabled" if level == "off" else "enabled"}}
        # 关：OpenAI 协议里最低档是 minimal；高：high
        return {"reasoning_effort": "minimal" if level == "off" else level}

    def _ensure_client(self) -> None:
        """密钥/端点/超时变了就换一个客户端（SDK 把它们绑在 client 上）。"""
        key, base, timeout = self._resolve()
        sig = (key, base, timeout)
        self._timeout = timeout
        # 输出上限显式声明（2026-09-10）：不传 max_tokens 时端点默认锁
        # 4,096——org 全覆盖产物、write_file 大参数都在此截断。官方上限
        # 384K（393,216 token），直接声明到顶。现读环境：改完下一次调用生效。
        raw = (os.environ.get("WOVRA_MAX_TOKENS") or "").strip()
        try:
            self._max_tokens = int(float(raw)) if raw else 393216
        except ValueError:
            self._max_tokens = 393216
        if sig == self._client_key and self._client is not None:
            return
        self.base_url = base
        # 流式读超时 = 相邻分块的最大静默间隔（不是总时长）——真生成时
        # token 持续到达不会触发；端点挂流（实测空响应吊 751s）在
        # WOVRA_READ_TIMEOUT 内被切断，交给上层重试。总时长由 token 流
        # 自然决定，长思考/长输出不受限。
        self._client = OpenAI(api_key=key, base_url=base, timeout=timeout)
        self._client_key = sig

    def chat(self, messages: list[dict], tools: Optional[list[dict]] = None,
             stream: bool = False, **kwargs: Any):
        """发送一次对话补全请求；stream=True 时返回分块迭代器。

        返回原始对象/分块而不是只返回文本：工具调用场景需要访问
        tool_calls、usage 等细节，封装掉反而碍事。
        tools=None 时 SDK 会自动省略该参数，不影响普通对话。
        """
        self._ensure_client()   # 渠道商/模型/超时都在这里现解（改完下一次调用生效）
        if stream:
            # 流式默认要求服务端在最后一个分块附带 usage 统计
            # （OpenAI 协议扩展 stream_options，主流兼容服务都支持）。
            # 没有它，流式调用就拿不到 tokens 数，成本核算无从谈起。
            kwargs.setdefault("stream_options", {"include_usage": True})
        # 思考强度（会话级）：调用方显式给了 extra_body 就以它为准（维护调用要
        # 关思考）；否则按本会话选的强度发。端点已经拒过一次就不再白试。
        if "extra_body" not in kwargs and not getattr(self, "_no_reasoning", False):
            body = self.reasoning_body()
            if body:
                kwargs["extra_body"] = body
        # 可选参数缺省时不发给服务端（工具禁用 = 整个字段省略，而非 null）：
        # 严格端点对 tools=null / stream=null 这类空值可能直接 400
        payload: dict[str, Any] = {"model": self.model, "messages": messages}
        if stream:
            payload["stream"] = True
        if tools:
            payload["tools"] = tools
        payload["max_tokens"] = self._max_tokens
        # 部分端点不认思考强度字段（返回 400/参数错误）：去掉它重发一次，
        # 而不是让整轮卡死在"不支持的可选参数"上。
        try:
            try:
                return self._client.chat.completions.create(**payload, **kwargs)
            except APIError as error:
                if not kwargs.get("extra_body") or not _is_bad_param(error):
                    raise
                retry = dict(kwargs)
                retry.pop("extra_body", None)
                self._no_reasoning = True   # 记住这端点不吃，后续不再白试
                return self._client.chat.completions.create(**payload, **retry)
        except (NotFoundError, AuthenticationError, PermissionDeniedError, APIConnectionError) as error:
            raise LLMConfigError(
                f"{_config_hint(error, self.model, self.base_url)}\n"
                f"服务端原始信息：{_raw_detail(error)}"
            ) from error
