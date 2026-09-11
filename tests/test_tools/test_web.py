"""工具层测试（自 test_tools.py 拆分，2026-09-11）。

本模块：test_web。"""

from wovra.tools import web_fetch

# 共享夹具/工具（_helpers.py 是原文件的公共头部）
from ._helpers import *  # noqa: F401,F403

def test_web_fetch_rejects_non_http_and_internal_hosts(monkeypatch):
    """SSRF 防护：非 http/https 与内网/回环地址一律拒绝（不发真实请求）。"""
    from wovra import tools as tools_module

    monkeypatch.setattr(tools_module.safety, "_audit", lambda detail: None)
    assert "仅支持 http/https" in web_fetch("ftp://example.com/file")
    assert "拒绝访问内网" in web_fetch("http://127.0.0.1:8000/secret")
    assert "拒绝访问内网" in web_fetch("http://192.168.1.1/admin")


def test_assert_public_url_fake_ip_is_proxy_artifact(monkeypatch):
    """clash Fake-IP 段（198.18.0.0/15）是代理劫持伪影，不按内网拦截；
    字面内网 IP 与真实内网解析结果仍然拒绝。"""
    import socket as _socket
    from wovra import tools as tools_module

    monkeypatch.setattr(tools_module.safety, "_audit", lambda detail: None)
    # Fake-IP：字面地址与解析结果都放行
    assert tools_module.web._assert_public_url("http://198.18.0.1/") is None
    monkeypatch.setattr(
        _socket, "getaddrinfo",
        lambda host, port, **kw: [(None, None, None, "", ("198.18.5.5", 0))])
    assert tools_module.web._assert_public_url("https://example.com/doc") is None
    # 真实内网解析仍然拒绝
    monkeypatch.setattr(
        _socket, "getaddrinfo",
        lambda host, port, **kw: [(None, None, None, "", ("10.0.0.5", 0))])
    assert "拒绝访问内网" in tools_module.web._assert_public_url("https://example.com/doc")
    assert "拒绝访问内网" in tools_module.web._assert_public_url("http://192.168.1.1/")
    assert "拒绝访问内网" in tools_module.web._assert_public_url("http://169.254.169.254/meta")


def test_search_engines_parse_canned_html(monkeypatch):
    """两个搜索引擎的解析器：对罐头 HTML 提取标题/链接/摘要。"""
    import sys as _sys

    from wovra import tools as tools_module

    ddg = ('<div><a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fdocs.example.com">'
           'Docs <b>Home</b></a><a class="result__snippet">All about docs</a></div>')
    monkeypatch.setattr(tools_module.web.urllib.request, "urlopen",
                        lambda req, timeout=None: _FakeUrllib._Resp(ddg))
    result = tools_module.web._search_ddg("docs", 5)
    assert "https://docs.example.com" in result and "Docs Home" in result

    bing = ('<li class="b_algo"><h2><a href="https://bing.example.com/x">Bing Result</a></h2>'
            '<p>Bing snippet</p></li>')
    monkeypatch.setattr(tools_module.web.urllib.request, "urlopen",
                        lambda req, timeout=None: _FakeUrllib._Resp(bing))
    result = tools_module.web._search_bing("x", 5)
    assert "Bing Result" in result and "Bing snippet" in result


def test_web_search_falls_back_to_second_engine(monkeypatch):
    """DDG 失败自动换 Bing；全失败时回传各引擎原因。"""
    from wovra import tools as tools_module

    monkeypatch.setattr(tools_module.safety, "_audit", lambda detail: None)
    monkeypatch.setattr(tools_module.web, "_search_ddg", lambda q, n: "duckduckgo 无结果或被限流。")
    monkeypatch.setattr(tools_module.web, "_search_bing", lambda q, n: "搜索 'q' 的结果：\n1. 命中")
    assert "命中" in tools_module.web_search("q")

    monkeypatch.setattr(tools_module.web, "_search_bing", lambda q, n: "bing 失败: 限流")
    result = tools_module.web_search("q")
    assert "所有搜索通道都失败了" in result and "bing 失败" in result
