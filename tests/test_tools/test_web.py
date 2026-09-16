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
    """本地兜底通道的解析器：对罐头 HTML 提取标题/链接/摘要（含 lite 端点形态）。"""
    from wovra import tools as tools_module

    ddg = ('<div><a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fdocs.example.com">'
           'Docs <b>Home</b></a><a class="result__snippet">All about docs</a></div>')
    monkeypatch.setattr(tools_module.web.urllib.request, "urlopen",
                        lambda req, timeout=None: _FakeUrllib._Resp(ddg))
    rows, dropped, note = tools_module.web._search_ddg("docs home", 5)
    assert rows[0][1] == "https://docs.example.com" and "Docs Home" in rows[0][0]
    assert dropped == 0 and note == ""

    # lite 端点形态（单引号 + rel=nofollow + 协议相对跳转壳）也必须解出目标 URL
    lite = ('<td><a rel="nofollow" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fdocs.example.com'
            '%2Fguide&amp;rut=x" class=\'result-link\'>Docs Guide</a></td>'
            '<td class=\'result-snippet\'>Lite snippet</td>')
    monkeypatch.setattr(tools_module.web.urllib.request, "urlopen",
                        lambda req, timeout=None: _FakeUrllib._Resp(lite))
    rows, _, _ = tools_module.web._search_ddg("docs guide", 5)
    assert rows[0][1] == "https://docs.example.com/guide" and "Lite snippet" in rows[0][2]


def test_search_relevance_is_advisory_not_a_gate(monkeypatch):
    """相关性降级为**标注**（2026-09-16 改口径）。

    旧口径（TOOLING_REVIEW.md §2"宁缺毋滥"）把词面相关性当硬闸，GAIA 实测
    （output/gaia/FINDINGS.md §1）证明它会造成**错误结论**：Bing 的 10 条候选
    全被滤掉后返回"未找到相关结果"，模型据此判定"网上没有资料"并放弃。
    现在不够相关的行照给，但必须带一条"没有相关性保证"的显式警告。
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

    # 全不相关：**不返回**"未找到相关结果"，而是给出结果 + 警告
    kept, dropped, note = tools_module.web._screen_local(query, [unrelated], 5)
    assert len(kept) == 1 and dropped == 0
    assert "没做相关性保证" in note and "网上没有资料" in note
    # 部分相关：不贴警告（有相关行垫底）
    kept, dropped, note = tools_module.web._screen_local(query, [unrelated, relevant], 5)
    assert len(kept) == 2 and dropped == 0 and note == ""


def test_search_ads_and_engine_shell_pages_are_dropped(monkeypatch):
    """广告行与引擎自家壳页按 host 滤掉。

    DDG 的广告是 `duckduckgo.com/y.js?ad_domain=…`，标题/摘要**与查询高度重合**
    （买来的位置当然贴合关键词）——只靠词面重叠判不出来，故按 host 单独判。
    2026-09-16 补第二类：DDG 把广告位伪装成自家 help 页
    （`/duckduckgo-help-pages/company/ads-by-microsoft-…`），正文是广告词且含查询词。
    实测那次连查 `Wikipedia Tower Bridge` 都只回这两条壳页——路径名单认不全，
    故改为"引擎自家域一律不当检索答案"。它比无关结果更坏：看起来最相关。
    """
    from wovra import tools as tools_module

    assert tools_module.web._is_ad("https://duckduckgo.com/y.js?ad_domain=udemy.com")
    assert tools_module.web._is_ad("https://www.bing.com/aclick?ld=xyz")
    assert tools_module.web._is_ad(
        "https://duckduckgo.com/duckduckgo-help-pages/company/"
        "ads-by-microsoft-on-duckduckgo-private-search/")
    assert tools_module.web._is_ad("https://www.bing.com/search?q=x")
    assert not tools_module.web._is_ad("https://docs.python.org/3/library/zlib.html")

    query = "how to parse PNG header in pure python"
    ad = ("more info",
          "https://duckduckgo.com/duckduckgo-help-pages/company/ads-by-microsoft-x/",
          "Explore Tower Bridge's Sightseeing Tours.")
    good = ("Pure Python PNG parsing — zlib and struct",
            "https://example.org/png-header",
            "Parse the PNG header with struct.unpack and decompress IDAT with zlib")
    assert tools_module.web._relevant(query, *ad) is False     # 广告直接被判无关
    assert tools_module.web._relevant(query, *good) is True
    kept, dropped, _ = tools_module.web._screen_local(query, [ad, good], 5)
    assert [row[1] for row in kept] == ["https://example.org/png-header"]
    assert dropped == 1


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


_SEARCH_KEY_VARS_FOR_TEST = (
    "Wovra_SEARCH_PROVIDER", "Wovra_SEARCH_KEY",
    "Wovra_Tavily", "TAVILY_API_KEY",
    "Wovra_Serper", "SERPER_API_KEY",
    "Wovra_Exa", "EXA_API_KEY",
    "Wovra_Bocha", "BOCHA_API_KEY",
    "Wovra_SerpAPI", "Wovra_Serpapi", "SERPAPI_API_KEY",
    "Wovra_Firecrawl", "FIRECRAWL_API_KEY",
)


def _clean_search_env(monkeypatch):
    """隔离真实 .env（本机可能真配了检索密钥）：不读它、清掉相关变量。"""
    from wovra import tools as tools_module

    monkeypatch.setattr(tools_module.web, "_ensure_dotenv", lambda: None)
    for var in _SEARCH_KEY_VARS_FOR_TEST:
        monkeypatch.delenv(var, raising=False)


def test_search_provider_selection(monkeypatch):
    """后端选择：显式指定优先；否则取注册表里第一个配了密钥的；都没有则空（本地）。

    密钥变量名两套都认：`Wovra_<家>`（用户 .env 里的写法）与各家惯用名。
    """
    from wovra import tools as tools_module

    web = tools_module.web
    _clean_search_env(monkeypatch)
    assert web.search_provider() == ""
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-x")
    assert web.search_provider() == "firecrawl"
    monkeypatch.setenv("Wovra_Bocha", "bocha-x")
    assert web.search_provider() == "bocha"          # 注册表顺序：bocha 在 firecrawl 前
    monkeypatch.setenv("Wovra_SEARCH_PROVIDER", "serpapi")
    assert web.search_provider() == "serpapi"        # 显式指定压过自动探测
    monkeypatch.setenv("Wovra_SEARCH_KEY", "generic")  # 通用键对每一家都算配了
    assert web._search_key("serper") == "generic"


def test_search_order_is_random_but_explicit_goes_first(monkeypatch):
    """默认随机挑一家先试（把调用摊到各家额度上）；显式指定的排第一，但**不是**
    唯一候选——它挂了仍要往后轮（用户口径："失败换下一个"）。"""
    from wovra import tools as tools_module

    web = tools_module.web
    _clean_search_env(monkeypatch)
    assert web._search_order() == []
    for name in ("Wovra_Tavily", "Wovra_Serper", "Wovra_Exa"):
        monkeypatch.setenv(name, "k")
    orders = {tuple(web._search_order()) for _ in range(40)}
    assert all(set(order) == {"tavily", "serper", "exa"} for order in orders)
    assert len(orders) > 1                            # 确实在打乱
    monkeypatch.setenv("Wovra_SEARCH_PROVIDER", "exa")
    for _ in range(10):
        order = web._search_order()
        assert order[0] == "exa" and len(order) == 3


def test_ensure_dotenv_points_at_repo_root(monkeypatch):
    """回归 2026-09-16：`.env` 路径不能写死 parents[N]。

    本文件在 `src/wovra/tools/` 下，比 `llm.py` 深一层；写死 `parents[2]` 会
    指到 `src/.env`（不存在）——实测症状是**密钥一个都读不到**，检索静默退回
    本地兜底，用户以为"配了没用"。
    """
    from pathlib import Path as _Path

    import dotenv as dotenv_module
    from wovra import tools as tools_module

    web = tools_module.web
    seen: dict = {}
    monkeypatch.setattr(dotenv_module, "load_dotenv",
                        lambda dotenv_path=None, **kwargs: seen.setdefault(
                            "path", dotenv_path))
    monkeypatch.setattr(web, "_dotenv_loaded", False)
    web._ensure_dotenv()
    assert seen["path"] is not None and seen["path"].name == ".env"
    # 判据是"仓库根"，也就是含有 pyproject.toml 的那一层——不是某个固定层数
    assert (seen["path"].parent / "pyproject.toml").exists()


def test_api_rows_adapts_each_vendor_schema():
    """六家的字段名各不相同，_api_rows 各归各位；缺字段/未知供应商不抛。"""
    from wovra import tools as tools_module

    web = tools_module.web
    assert web._api_rows("tavily", {"results": [
        {"title": "T", "url": "https://b.example", "content": "C"}]}) == [
        ("T", "https://b.example", "C")]
    assert web._api_rows("serper", {"organic": [
        {"title": "T", "link": "https://c.example", "snippet": "S"}]})[0][2] == "S"
    assert web._api_rows("serpapi", {"organic_results": [
        {"title": "T", "link": "https://e.example", "snippet": "S"}]})[0][1] == "https://e.example"
    assert web._api_rows("bocha", {"webPages": {"value": [
        {"name": "T", "url": "https://f.example", "summary": "SM"}]}}) == [
        ("T", "https://f.example", "SM")]
    # 博查也可能把内容套在 data 里；summary 缺失时退到 snippet
    assert web._api_rows("bocha", {"data": {"webPages": {"value": [
        {"name": "T", "url": "https://g.example", "snippet": "SN"}]}}})[0][2] == "SN"
    assert web._api_rows("firecrawl", {"data": {"web": [
        {"title": "T", "url": "https://h.example", "description": "D"}]}}) == [
        ("T", "https://h.example", "D")]
    assert web._api_rows("exa", {"results": [
        {"title": "T", "url": "https://d.example", "highlights": ["H1", "H2"]}]})[0][2] == "H1 H2"
    assert web._api_rows("tavily", {}) == []
    assert web._api_rows("nope", {"results": [{"title": "T", "url": "u"}]}) == []


def test_api_search_failures_are_readable(monkeypatch):
    """API 层的失败都要变成可读文本（模型据此决定兜底还是换路），不许抛异常。"""
    import urllib.error
    from wovra import tools as tools_module

    web = tools_module.web
    _clean_search_env(monkeypatch)
    assert "Wovra_SEARCH_KEY" in web._api_search("tavily", "q", 3)
    assert "只支持" in web._api_search("bing-scrape", "q", 3)

    def boom(*args, **kwargs):
        raise urllib.error.HTTPError("https://api.x/", 429, "Too Many", {}, None)

    monkeypatch.setenv("Wovra_SEARCH_KEY", "k")
    monkeypatch.setattr(web, "_http_json", boom)
    out = web._api_search("tavily", "q", 3)
    assert "429" in out and "限流" in out

    def dns_boom(*args, **kwargs):
        raise OSError("dns boom")

    monkeypatch.setattr(web, "_http_json", dns_boom)
    assert "API 失败" in web._api_search("tavily", "q", 3)


def test_api_search_keeps_key_out_of_url_and_result(monkeypatch):
    """密钥只进请求头与约定的 body 字段：不进 URL、不进给模型的文本。"""
    from wovra import tools as tools_module

    web = tools_module.web
    _clean_search_env(monkeypatch)
    monkeypatch.setenv("Wovra_SEARCH_KEY", "SECRET-KEY-123")
    seen: dict = {}

    def fake_json(url, payload=None, headers=None):
        seen.update(url=url, payload=payload, headers=headers or {})
        return {"organic": [{"title": "T", "link": "https://a.example",
                             "snippet": "D"}]}

    monkeypatch.setattr(web, "_http_json", fake_json)
    rows = web._api_search("serper", "q", 3)
    assert rows[0][1] == "https://a.example"
    assert seen["headers"].get("X-API-KEY") == "SECRET-KEY-123"
    assert "SECRET-KEY-123" not in seen["url"]
    assert "SECRET-KEY-123" not in repr(rows)
    # tavily 走 body.api_key（它的接口约定），也同样不能出现在结果里
    monkeypatch.setattr(web, "_http_json",
                        lambda url, payload=None, headers=None: {
                            "results": [{"title": "T", "url": "https://b.example",
                                         "content": "C"}]})
    rows = web._api_search("tavily", "q", 3)
    assert rows[0][1] == "https://b.example" and "SECRET-KEY-123" not in repr(rows)


def test_web_search_prefers_api_then_falls_back_to_local(monkeypatch):
    """链路：API 成功不碰本地；API 失败退本地并标注"兜底（API 都不可用）"；
    两路都空时报两路各自的失败原因。"""
    from wovra import tools as tools_module

    web = tools_module.web
    monkeypatch.setattr(tools_module.safety, "_audit", lambda detail: None)
    monkeypatch.setattr(web, "_cache_get", lambda k, t: None)
    monkeypatch.setattr(web, "_cache_put", lambda k, t, v: None)
    monkeypatch.setattr(web, "_search_order", lambda: ["tavily"])
    called = {"local": 0}

    def local_rows(query, n):
        called["local"] += 1
        return ([("本地标题", "https://local.example", "本地摘要")], 0, "")

    monkeypatch.setattr(web, "_search_ddg", local_rows)
    monkeypatch.setattr(web, "_api_search", lambda p, q, n: [
        ("API 标题", "https://api.example", "API 摘要")])
    out = tools_module.web_search("q")
    assert "API 标题" in out and "引擎: tavily" in out and called["local"] == 0

    monkeypatch.setattr(
        web, "_api_search",
        lambda p, q, n: "tavily API 失败（HTTP 401：密钥无效或未授权）。")
    out = tools_module.web_search("q")
    assert "本地标题" in out and "本地兜底（API 都不可用）" in out and "401" in out

    monkeypatch.setattr(web, "_search_ddg", lambda q, n: "duckduckgo 失败: 限流")
    out = tools_module.web_search("q")
    assert "检索失败" in out and "401" in out and "限流" in out


def test_web_search_tries_next_provider_on_failure(monkeypatch):
    """一家失败就换下一家（用户口径："如果失败换下一个"），第三个成功就收工。"""
    from wovra import tools as tools_module

    web = tools_module.web
    monkeypatch.setattr(tools_module.safety, "_audit", lambda detail: None)
    monkeypatch.setattr(web, "_cache_get", lambda k, t: None)
    monkeypatch.setattr(web, "_cache_put", lambda k, t, v: None)
    monkeypatch.setattr(web, "_search_order", lambda: ["bocha", "serpapi", "exa"])

    def no_local(*args, **kwargs):
        raise AssertionError("三家还有一家没试，不该走到本地兜底")

    monkeypatch.setattr(web, "_search_ddg", no_local)
    seen: list = []

    def fake_api(provider, query, n):
        seen.append(provider)
        if provider == "bocha":
            return "bocha API 失败（HTTP 429：配额用尽或被限流）。"
        if provider == "serpapi":
            return "serpapi API 失败（HTTP 500：接口报错）。"
        return [("第三个才成功", "https://exa.example", "摘要")]

    monkeypatch.setattr(web, "_api_search", fake_api)
    out = tools_module.web_search("q")
    assert seen == ["bocha", "serpapi", "exa"]
    assert "第三个才成功" in out and "引擎: exa" in out


class _FakeResp:
    """最小响应替身：`_open_url` 的返回被当作上下文管理器用。"""

    def __init__(self, body: bytes, ctype: str = "text/html"):
        self._body = body
        self.headers = {"Content-Type": ctype}

    def read(self, n=None):
        return self._body[:n] if n else self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_web_fetch_falls_back_to_firecrawl_instead_of_a_blank_page(monkeypatch, tmp_path):
    """JS 渲染站的兜底：自己只抽到几个字符时问 Firecrawl 要 Markdown。

    用户口径里的"搜索 API 找 URL、Firecrawl 取内容"接在**同一跳**里完成——
    agent 不必自己串两步，也不必为此写脚本。
    """
    from wovra import tools as tools_module

    web = tools_module.web
    monkeypatch.setattr(tools_module.safety, "_audit", lambda detail: None)
    monkeypatch.setattr(tools_module.safety, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(web, "_assert_public_url", lambda url: None)
    monkeypatch.setattr(web, "_llms_txt_url", lambda url: None)
    monkeypatch.setattr(web, "_fetch_with_accept", lambda u, timeout=30: None)
    monkeypatch.setattr(
        web, "_open_url",
        lambda url, timeout=30, max_hops=5: (
            _FakeResp(b"<html><body><script>var app=1;</script></body></html>"), None))
    monkeypatch.setattr(web, "_firecrawl_markdown",
                        lambda url: "# 干净正文\n\n来自 Firecrawl 的 Markdown。")
    out = web.web_fetch("https://spa.example.com/page")
    assert "Firecrawl 提取" in out and "干净正文" in out

    # 没配 Firecrawl（或它失败）时回到原路径：明说没有文本内容，不编
    monkeypatch.setattr(web, "_firecrawl_markdown", lambda url: None)
    out = web.web_fetch("https://spa.example.com/page2")
    assert "无文本内容" in out


def test_firecrawl_markdown_returns_none_without_key(monkeypatch):
    """没配 Firecrawl 时静默降级（返回 None），不抛、不发请求。"""
    from wovra import tools as tools_module

    web = tools_module.web
    _clean_search_env(monkeypatch)

    def no_http(*args, **kwargs):
        raise AssertionError("没配密钥不该发请求")

    monkeypatch.setattr(web, "_http_json", no_http)
    assert web._firecrawl_markdown("https://example.com/") is None


def test_web_search_without_api_says_how_to_configure(monkeypatch):
    """没配 API 时，失败信息要给出配置方式（否则用户不知道为何只有本地兜底）。"""
    from wovra import tools as tools_module

    web = tools_module.web
    monkeypatch.setattr(tools_module.safety, "_audit", lambda detail: None)
    monkeypatch.setattr(web, "_cache_get", lambda k, t: None)
    monkeypatch.setattr(web, "_cache_put", lambda k, t, v: None)
    monkeypatch.setattr(web, "_search_order", lambda: [])
    monkeypatch.setattr(web, "_search_ddg", lambda q, n: "duckduckgo 无结果或被限流。")
    out = tools_module.web_search("q")
    assert "未配置检索 API" in out and ".env.example" in out and "本地兜底" in out


def test_web_search_caches_success_but_not_failure(monkeypatch):
    """失败不落缓存：一次 429 不该被记一小时，让后续重试永远拿到旧结论。"""
    from wovra import tools as tools_module

    web = tools_module.web
    monkeypatch.setattr(tools_module.safety, "_audit", lambda detail: None)
    puts: list = []
    monkeypatch.setattr(web, "_cache_get", lambda k, t: None)
    monkeypatch.setattr(web, "_cache_put", lambda k, t, v: puts.append(k))
    monkeypatch.setattr(web, "_search_order", lambda: [])
    monkeypatch.setattr(web, "_search_ddg", lambda q, n: "duckduckgo 失败: 限流")
    tools_module.web_search("q")
    assert puts == []
    monkeypatch.setattr(web, "_search_ddg",
                        lambda q, n: ([("T", "https://t.example", "")], 0, ""))
    tools_module.web_search("q")
    assert len(puts) == 1


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
