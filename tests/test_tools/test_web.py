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


def test_fake_ip_ipv6_artifact_not_blocked(monkeypatch):
    """Fake-IP 是双栈的：v6 伪影段（fdfe:dcba:9876::/48）必须与 v4 一样放行。

    回归 2026-09-13 实测缺陷：v4 段放行、v6 段被判 is_private，而
    getaddrinfo 双栈返回两族、逐条检查一条判死即整体拒绝 → 全网打不开。
    """
    import ipaddress as _ipaddress
    import socket as _socket
    from wovra import tools as tools_module

    monkeypatch.setattr(tools_module.safety, "_audit", lambda detail: None)
    # 双栈解析：v4 伪影段 + v6 伪影段同时返回 → 必须放行
    monkeypatch.setattr(
        _socket, "getaddrinfo",
        lambda host, port, **kw: [
            (_socket.AF_INET, None, None, "", ("198.18.0.5", 0)),
            (_socket.AF_INET6, None, None, "", ("fdfe:dcba:9876::6", 0, 0, 0)),
        ])
    assert tools_module.web._assert_public_url("https://docs.python.org/3/") is None
    # 自定义段可由环境变量追加
    monkeypatch.setenv("WOVRA_FAKE_IP_NETS", "10.99.0.0/16")
    assert tools_module.web._is_internal(_ipaddress.ip_address("10.99.1.1")) is False
    # 真实内网 v6（ULA fc00::/7 中非伪影段）仍然拒绝
    assert tools_module.web._is_internal(_ipaddress.ip_address("fd00::1")) is True


def test_search_engines_parse_canned_html(monkeypatch):
    """两个搜索引擎的解析器：对罐头 HTML 提取标题/链接/摘要。"""
    import sys as _sys

    from wovra import tools as tools_module

    ddg = ('<div><a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fdocs.example.com">'
           'Docs <b>Home</b></a><a class="result__snippet">All about docs</a></div>')
    monkeypatch.setattr(tools_module.web.urllib.request, "urlopen",
                        lambda req, timeout=None: _FakeUrllib._Resp(ddg))
    rows, filtered = tools_module.web._search_ddg("docs home", 5)
    assert rows[0][1] == "https://docs.example.com" and "Docs Home" in rows[0][0]

    # lite 端点形态（单引号 + rel=nofollow + 协议相对跳转壳）也必须解出目标 URL
    lite = ('<td><a rel="nofollow" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fdocs.example.com'
            '%2Fguide&amp;rut=x" class=\'result-link\'>Docs Guide</a></td>'
            '<td class=\'result-snippet\'>Lite snippet</td>')
    monkeypatch.setattr(tools_module.web.urllib.request, "urlopen",
                        lambda req, timeout=None: _FakeUrllib._Resp(lite))
    rows, _ = tools_module.web._search_ddg("docs guide", 5)
    assert rows[0][1] == "https://docs.example.com/guide" and "Lite snippet" in rows[0][2]

    bing = ('<li class="b_algo"><h2><a href="https://bing.example.com/x">Bing Result</a></h2>'
            '<p>Bing snippet</p></li>')
    monkeypatch.setattr(tools_module.web.urllib.request, "urlopen",
                        lambda req, timeout=None: _FakeUrllib._Resp(bing))
    rows, _ = tools_module.web._search_bing("bing result", 5)
    assert rows[0][0] == "Bing Result" and rows[0][2] == "Bing snippet"


def test_search_relevance_filter_drops_unrelated(monkeypatch):
    """§2 的 P1 缺陷：与查询词零重叠的结果**不呈现**，宁缺毋滥。

    实测现象（2026-09-15 复现）：中文长查询经 Bing 返回日本汉字字典页，
    旧实现把它当合法结果交给模型。现在两条真实相关的过、两条无关的被滤。
    """
    from wovra import tools as tools_module

    query = "智元 OmniPicker 夹爪 Modbus 通信协议"
    relevant = ("智元OmniPicker机械夹爪调试技术指南",
                "https://wenku.example.com/x",
                "OmniPicker 系列机械夹爪 Modbus 通信协议与寄存器说明")
    unrelated = ("漢字「智」の部首・画数", "https://kanji.example.jp/2719",
                 "智は、ちえ / さといなどの意味を持つ漢字です。")
    assert tools_module.web._relevant(query, *relevant) is True
    assert tools_module.web._relevant(query, *unrelated) is False
    # 全被滤掉 → 明说"未找到相关结果"，不拿噪声充数
    out = tools_module.web._screen(query, [unrelated], 5)
    assert isinstance(out, str) and "未找到相关结果" in out
    kept, filtered = tools_module.web._screen(query, [unrelated, relevant], 5)
    assert len(kept) == 1 and filtered == 1


def test_redirect_to_internal_is_blocked(monkeypatch):
    """重定向 SSRF：公网 URL 302 跳到内网必须被拦（不发第二跳请求）。"""
    import urllib.error
    from wovra import tools as tools_module

    monkeypatch.setattr(tools_module.safety, "_audit", lambda detail: None)
    monkeypatch.setattr(tools_module.web, "_assert_public_url", lambda url: None)

    def fake_open(req, timeout=None):
        raise urllib.error.HTTPError(
            "https://public.example.com/", 302, "Found",
            {"Location": "http://169.254.169.254/latest/meta-data/"}, None)

    monkeypatch.setattr(tools_module.web.urllib.request, "build_opener",
                        lambda *a: type("O", (), {"open": staticmethod(fake_open)})())
    # 第二跳目标重新校验 → 命中 169.254 元数据地址，被拦
    monkeypatch.setattr(tools_module.web, "_assert_public_url",
                        lambda url: (f"拒绝访问内网/保留地址（{url}）"
                                    if "169.254" in url else None))
    try:
        tools_module.web._open_url("https://public.example.com/")
        assert False, "应当抛出被拦截"
    except ValueError as error:
        assert "重定向被拦截" in str(error) and "169.254" in str(error)


def test_gbk_page_decodes_without_mojibake(monkeypatch):
    """GBK 页面按 charset 解码：中文不再乱码（2026-09-13 之前固定 UTF-8）。"""
    from wovra import tools as tools_module

    gbk_html = "<html><body><p>中文测试内容</p></body></html>".encode("gbk")
    assert "中文测试内容" in tools_module.web._html_to_text(gbk_html, "text/html; charset=gbk")
    # 未声明 charset 时读 <meta charset>
    gbk_meta = ('<html><head><meta charset="gbk"></head><body>元数据编码</body></html>'
                ).encode("gbk")
    assert "元数据编码" in tools_module.web._html_to_text(gbk_meta, "text/html")
    # 声明了 UTF-8 的页面照常
    assert "正常" in tools_module.web._html_to_text("正常内容".encode(), "text/html; charset=utf-8")


def test_bing_redirect_shell_is_unwrapped(monkeypatch):
    """Bing 的 bing.com/ck/a?u=a1<base64> 跳转壳要还原成目标 URL。"""
    import base64
    from wovra import tools as tools_module

    target = "https://docs.python.org/3/library/asyncio.html"
    shell = "https://www.bing.com/ck/a?!&&p=abc&u=a1" + base64.urlsafe_b64encode(
        target.encode()).decode().rstrip("=")
    assert tools_module.web._unwrap_redirect(shell) == target
    # HTML 转义过的壳（&amp;）也必须解开——实测搜索结果里全是这种
    assert tools_module.web._unwrap_redirect(shell.replace("&u=", "&amp;u=")) == target
    # 解不开 / 非 http 的原样返回，不影响搜索
    assert tools_module.web._unwrap_redirect("https://real.example.com/x") == "https://real.example.com/x"
    assert tools_module.web._unwrap_redirect("https://www.bing.com/ck/a?!&&p=x&u=a1!!!") .startswith(
        "https://www.bing.com/ck/a")


def test_web_search_falls_back_to_second_engine(monkeypatch):
    """DDG 失败自动换 Bing；全失败时回传各引擎原因（禁用缓存，聚焦回退）。"""
    from wovra import tools as tools_module

    monkeypatch.setattr(tools_module.safety, "_audit", lambda detail: None)
    # 缓存是新关注点，本测试禁用以免命中绕过引擎回退
    monkeypatch.setattr(tools_module.web, "_cache_get", lambda k, t: None)
    monkeypatch.setattr(tools_module.web, "_cache_put", lambda k, t, v: None)
    monkeypatch.setattr(tools_module.web, "_search_ddg", lambda q, n: "duckduckgo 无结果或被限流。")
    monkeypatch.setattr(tools_module.web, "_search_bing",
                        lambda q, n: ([("命中标题", "https://x.example.com", "")], 0))
    assert "命中" in tools_module.web_search("q")

    monkeypatch.setattr(tools_module.web, "_search_bing", lambda q, n: "bing 失败: 限流")
    result = tools_module.web_search("q")
    assert "未找到相关结果" in result and "bing 失败" in result


def test_web_fetch_cache_hit_skips_network(monkeypatch, tmp_path):
    """同一 URL 短时间重复抓命中缓存，不再走网络。"""
    from wovra import tools as tools_module

    monkeypatch.setattr(tools_module.safety, "_audit", lambda detail: None)
    monkeypatch.setattr(tools_module.safety, "PROJECT_ROOT", tmp_path)
    calls = {"n": 0}
    # 测试域名不真实解析，跳过 SSRF 校验（校验逻辑另有专项测试）
    monkeypatch.setattr(tools_module.web, "_assert_public_url", lambda url: None)

    def fake_fetch(url, timeout=30):
        calls["n"] += 1
        raise AssertionError("缓存命中后不应再发真实请求")

    monkeypatch.setattr(tools_module.web, "_open_url", fake_fetch)
    # 先直接写缓存（模拟第一次抓取已落盘）——缓存存**完整正文**，不是渲染结果
    tools_module.web._fetch_cache_put("https://cached.example.com/doc",
                                      "[https://cached.example.com/doc] 正文 12 字符",
                                      "正文内容正文内容正文内容")
    result = tools_module.web.web_fetch("https://cached.example.com/doc")
    assert "[缓存命中" in result and "正文内容" in result
    assert calls["n"] == 0
    # §1 附带问题回归：缓存存完整正文，后续**小额度**调用不得把残缺内容固化
    small = tools_module.web.web_fetch("https://cached.example.com/doc", max_chars=4)
    assert "正文内容"[:4] in small and "已按 max_chars 截断" in small
    assert "已落盘 output/spill/" in small      # 完整正文可取回
    full = tools_module.web.web_fetch("https://cached.example.com/doc")
    assert full.count("正文内容") == 3           # 全量调用仍拿到完整正文


def test_html_to_text_density_filters_nav(monkeypatch):
    """密度打分：导航/链接密集段被过滤，正文保留。"""
    from wovra import tools as tools_module

    html = ("<html><body>"
            "<nav><a href='/a'>首页</a><a href='/b'>关于</a><a href='/c'>联系</a></nav>"
            "<article><p>这是正文第一段，包含足够多的有效文字内容。</p>"
            "<p>这是正文第二段，继续提供有信息量的文本。</p></article>"
            "<footer><a href='/x'>版权</a><a href='/y'>条款</a></footer>"
            "</body></html>")
    text = tools_module.web._html_to_text(html.encode(), "text/html")
    assert "这是正文第一段" in text and "这是正文第二段" in text
    assert "首页" not in text and "版权" not in text  # 导航/页脚被滤掉


def test_content_negotiation_markdown_and_llms_txt(monkeypatch, tmp_path):
    """内容协商：站点返回 Markdown 或 /llms.txt 时直接用干净文本。"""
    from wovra import tools as tools_module

    monkeypatch.setattr(tools_module.safety, "_audit", lambda detail: None)
    monkeypatch.setattr(tools_module.safety, "PROJECT_ROOT", tmp_path)
    # 测试域名不真实解析，跳过 SSRF 校验（校验逻辑另有专项测试）
    monkeypatch.setattr(tools_module.web, "_assert_public_url", lambda url: None)
    monkeypatch.setattr(tools_module.web, "_fetch_with_accept", lambda u, timeout=30: (
        "# Site Docs\n\n这是 llms.txt 提供的干净文档。", "text/markdown"))
    result = tools_module.web.web_fetch("https://docs.example.com/")
    assert "llms.txt" in result and "干净文档" in result
    # 非根 URL 不探 llms.txt（_llms_txt_url 返回 None），走 HTML
    assert tools_module.web._llms_txt_url("https://docs.example.com/a/b") is None
    assert tools_module.web._llms_txt_url("https://docs.example.com/") is not None
