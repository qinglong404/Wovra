"""代理环境归一化与 LLM 客户端构造的测试。"""

from types import SimpleNamespace

import openai
import pytest

try:
    # openai 3.x 依赖改名版 httpx（见 uv.lock 里的 httpx2）
    import httpx2 as httpx
except ImportError:  # pragma: no cover——直接 pip 装的环境用的是原版 httpx
    import httpx

from wovra.llm import LLM, LLMConfigError, _normalize_proxy_schemes


def test_socks_scheme_is_normalized(monkeypatch):
    # 模拟 clash 等工具导出的非标准协议名
    monkeypatch.setenv("ALL_PROXY", "socks://127.0.0.1:7897/")
    monkeypatch.setenv("https_proxy", "socks://127.0.0.1:7897")
    monkeypatch.delenv("HTTP_PROXY", raising=False)

    _normalize_proxy_schemes()

    import os

    assert os.environ["ALL_PROXY"] == "socks5://127.0.0.1:7897/"
    assert os.environ["https_proxy"] == "socks5://127.0.0.1:7897"


def test_standard_schemes_are_untouched(monkeypatch):
    import os

    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:7890")
    monkeypatch.setenv("HTTPS_PROXY", "socks5://127.0.0.1:7897")

    _normalize_proxy_schemes()

    assert os.environ["ALL_PROXY"] == "http://127.0.0.1:7890"
    assert os.environ["HTTPS_PROXY"] == "socks5://127.0.0.1:7897"


# ---- 配置类错误的友好翻译 ----------------------------------------------------


def _status_error(cls, status_code: int, message: str) -> Exception:
    """构造 openai SDK 的 HTTP 状态错误（需要绑定 httpx Request/Response）。"""
    request = httpx.Request("POST", "https://example.com/v1/chat/completions")
    response = httpx.Response(
        status_code, request=request, json={"error": {"message": message}}
    )
    return cls(message, response=response, body=None)


class _FakeCompletions:
    def __init__(self, error: Exception) -> None:
        self._error = error

    def create(self, **kwargs):
        raise self._error


def _llm_that_raises(error: Exception) -> LLM:
    llm = LLM(api_key="test-key", base_url="https://example.com/v1", model="glm-x")
    llm._client = SimpleNamespace(
        chat=SimpleNamespace(completions=_FakeCompletions(error))
    )
    return llm


def test_model_not_found_points_to_model_config():
    """404（端点上没有这个模型）→ 提示检查 BASE_URL 与 MODEL 的配套关系。"""
    error = _status_error(
        openai.NotFoundError, 404,
        "The model or endpoint glm-x does not exist or you do not have access",
    )
    with pytest.raises(LLMConfigError, match="模型配置问题") as excinfo:
        _llm_that_raises(error).chat([{"role": "user", "content": "hi"}])
    message = str(excinfo.value)
    assert "Wovra_BASE_URL" in message and "Wovra_MODEL" in message
    assert "glm-x" in message
    assert excinfo.value.__cause__ is error  # 原始异常保留，排查时可见


def test_auth_error_points_to_api_key():
    error = _status_error(openai.AuthenticationError, 401, "invalid api key")
    with pytest.raises(LLMConfigError, match="Wovra_API_KEY"):
        _llm_that_raises(error).chat([{"role": "user", "content": "hi"}])


def test_connection_error_points_to_endpoint():
    request = httpx.Request("POST", "https://example.com/v1/chat/completions")
    error = openai.APIConnectionError(request=request)
    with pytest.raises(LLMConfigError, match="无法连接"):
        _llm_that_raises(error).chat([{"role": "user", "content": "hi"}])
