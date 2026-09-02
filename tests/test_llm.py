"""代理环境归一化与 LLM 客户端构造的测试。"""

from wovra.llm import _normalize_proxy_schemes


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
