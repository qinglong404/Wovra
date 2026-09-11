"""网络工具：web_search（DuckDuckGo → Bing 回退）与 web_fetch（含 SSRF 防护）。"""

import ipaddress
import re
import urllib.parse
import urllib.request

from . import limits, safety


# ---- 网络与用户交互 ----------------------------------------------------------

_WEB_UA = "Mozilla/5.0 (X11; Linux x86_64) Wovra/0.1"


def _is_internal(ip: "ipaddress.IPv4Address | ipaddress.IPv6Address") -> bool:
    """判定 IP 是否内网/回环/保留地址（SSRF 防护的判定核心）。

    例外：198.18.0.0/15（基准测试段）是 clash 等代理 Fake-IP 模式的
    劫持伪影——开 TUN 时所有域名都解析到这一段，但连接实际由代理
    路由到真实目标。把它当内网会把整个互联网都拦在门外（实测教训）。
    """
    if ip.version == 4 and ip in ipaddress.ip_network("198.18.0.0/15"):
        return False
    return ip.is_private or ip.is_loopback or ip.is_reserved or ip.is_link_local


def _assert_public_url(url: str) -> str | None:
    """URL 安全校验：仅 http/https，且主机不得指向内网/回环/保留地址。"""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return f"仅支持 http/https URL: {url}"
    host = parsed.hostname or ""
    if not host:
        return f"URL 缺少主机名: {url}"
    import socket

    try:
        infos = socket.getaddrinfo(host, None)
    except OSError as error:
        return f"无法解析主机 {host}: {error}"
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if _is_internal(ip):
            return f"拒绝访问内网/保留地址（{host} → {info[4][0]}）。"
    return None


def _html_to_text(raw: bytes) -> str:
    from html import unescape

    text = raw.decode("utf-8", errors="replace")
    text = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", text)
    text = re.sub(r"(?i)<(br|/p|/div|/li|/h[1-6]|/tr)[^>]*>", "\n", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = unescape(text)
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


def web_fetch(url: str, max_chars: int = 0) -> str:
    """抓取一个 http(s) 网页，去除 HTML 标签后返回正文文本。

    适合查 API 文档、技术资料。仅 http/https，拒绝内网地址（防 SSRF），
    30 秒超时。正文默认全量返回（2026-09-11 放开，worklog §25：原先
    8000 字符硬上限，抓一份长文档要反复重试）；真超限时完整内容落盘
    output/spill/ 并给出路径。max_chars>0 时才按该值截断。
    找资料的入口用 web_search。
    """
    safety._audit(f"[web_fetch] {url}")
    blocked = _assert_public_url(url)
    if blocked:
        return blocked
    request = urllib.request.Request(url, headers={"User-Agent": _WEB_UA})
    try:
        with urllib.request.urlopen(request, timeout=30) as resp:
            ctype = (resp.headers.get("Content-Type") or "").lower()
            raw = resp.read(8_000_000)
    except Exception as error:  # noqa: BLE001——网络错误回传给模型自行调整
        return f"抓取失败: {error!r}"
    text = _html_to_text(raw) if ("html" in ctype or not ctype) else raw.decode("utf-8", errors="replace")
    if not text.strip():
        return f"URL 无文本内容（Content-Type: {ctype}）。"
    head = f"[{url}] Content-Type: {ctype or '未知'}，抓取 {len(raw)} 字节\n\n"
    body = text if max_chars <= 0 else text[:int(max_chars)]
    return limits.clip(head + body, "web_fetch")


def _search_ddg(query: str, max_results: int) -> str:
    url = "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote_plus(query)
    request = urllib.request.Request(url, headers={"User-Agent": _WEB_UA})
    try:
        with urllib.request.urlopen(request, timeout=30) as resp:
            html = resp.read(1_000_000).decode("utf-8", errors="replace")
    except Exception as error:  # noqa: BLE001
        return f"duckduckgo 失败: {error!r}"
    items = re.findall(
        r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
        html, re.S | re.I,
    )
    snippets = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', html, re.S | re.I)
    if not items:
        return "duckduckgo 无结果或被限流。"
    from html import unescape

    lines = []
    for i, (href, title) in enumerate(items[:max_results], start=1):
        title = " ".join(unescape(re.sub(r"<[^>]+>", "", title)).split())
        link = href
        m = re.search(r"[?&]uddg=([^&]+)", href)
        if m:
            link = urllib.parse.unquote(m.group(1))
        snippet = ""
        if i <= len(snippets):
            snippet = " ".join(unescape(re.sub(r"<[^>]+>", "", snippets[i - 1])).split())[:200]
        lines.append(f"{i}. {title}\n   {link}" + (f"\n   {snippet}" if snippet else ""))
    return f"搜索 {query!r} 的结果（前 {len(lines)} 条）：\n\n" + "\n\n".join(lines)


def _search_bing(query: str, max_results: int) -> str:
    url = ("https://www.bing.com/search?q=" + urllib.parse.quote_plus(query)
           + "&setlang=zh-hans")
    request = urllib.request.Request(url, headers={
        "User-Agent": _WEB_UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"})
    try:
        with urllib.request.urlopen(request, timeout=30) as resp:
            html = resp.read(1_000_000).decode("utf-8", errors="replace")
    except Exception as error:  # noqa: BLE001
        return f"bing 失败: {error!r}"
    chunks = html.split('<li class="b_algo"')[1:]
    from html import unescape

    lines = []
    for chunk in chunks:
        m = re.search(r'<h2[^>]*><a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', chunk, re.S | re.I)
        if not m:
            continue
        link = m.group(1)
        title = " ".join(unescape(re.sub(r"<[^>]+>", "", m.group(2))).split())
        p = re.search(r"<p[^>]*>(.*?)</p>", chunk, re.S | re.I)
        snippet = (" ".join(unescape(re.sub(r"<[^>]+>", "", p.group(1))).split())[:200]
                   if p else "")
        lines.append(f"{len(lines) + 1}. {title}\n   {link}"
                     + (f"\n   {snippet}" if snippet else ""))
        if len(lines) >= max_results:
            break
    if not lines:
        return "bing 无结果或返回了无法解析的页面。"
    return f"搜索 {query!r} 的结果（前 {len(lines)} 条）：\n\n" + "\n\n".join(lines)


def web_search(query: str, max_results: int = 8) -> str:
    """网页搜索，返回标题、链接与摘要。用于查技术文档与解决方案。

    引擎按序尝试：DuckDuckGo → Bing（均免 API Key；DDG 容易限流，
    失败自动换 Bing）。全部失败时回传各自原因；结果不足或被限流时，
    也可用 web_fetch 直接抓取已知网址。
    """
    safety._audit(f"[web_search] {query}")
    max_results = max(1, min(int(max_results), 20))
    errors = []
    for engine in (_search_ddg, _search_bing):
        result = engine(query, max_results)
        if result.startswith(("duckduckgo 失败", "duckduckgo 无结果",
                              "bing 失败", "bing 无结果")):
            errors.append(result)
            continue
        return result
    return ("所有搜索通道都失败了：\n" + "\n".join(errors)
            + "\n可稍后重试，或用 web_fetch 直接抓取已知网址。")
