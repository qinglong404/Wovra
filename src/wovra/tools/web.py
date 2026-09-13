"""网络工具：web_search（DuckDuckGo → Bing 回退）与 web_fetch（含 SSRF 防护）。"""

import ipaddress
import re
import urllib.error
import urllib.parse
import urllib.request

from . import limits, safety


# ---- 网络与用户交互 ----------------------------------------------------------

_WEB_UA = "Mozilla/5.0 (X11; Linux x86_64) Wovra/0.1"


# Fake-IP 劫持伪影段：clash 等代理开 TUN 时把所有域名都解析到这些段，
# 但连接实际由代理路由到真实目标。把它们当内网会把整个互联网拦在门外
# （实测教训，2026-09-11）。代理的段可配置，故做成清单 + 环境变量可扩。
_FAKE_IP_NETS = (
    "198.18.0.0/15",        # RFC 2544 基准测试段（clash IPv4 fake-ip 默认）
    "fdfe:dcba:9876::/48",  # clash IPv6 fake-ip 默认段（与上一行同一机制）
)


def _fake_ip_nets() -> tuple:
    """伪影段清单；WOVRA_FAKE_IP_NETS 可用逗号分隔追加自定义段。"""
    import os

    extra = os.environ.get("WOVRA_FAKE_IP_NETS", "")
    nets = list(_FAKE_IP_NETS) + [s.strip() for s in extra.split(",") if s.strip()]
    out = []
    for net in nets:
        try:
            out.append(ipaddress.ip_network(net))
        except ValueError:
            continue  # 配置写错不致命，忽略该条
    return tuple(out)


def _is_internal(ip: "ipaddress.IPv4Address | ipaddress.IPv6Address") -> bool:
    """判定 IP 是否内网/回环/保留地址（SSRF 防护的判定核心）。

    Fake-IP 伪影段（见 _FAKE_IP_NETS）不算内网——它们是代理的解析劫持，
    不是真实目的地址。注意 IPv4 与 IPv6 段必须同时放行：getaddrinfo 双栈
    返回两族，_assert_public_url 逐条检查、一条判死即整体拒绝，只放行
    v4 会被 v6 抵消掉（2026-09-13 实测：v4 放行 + v6 拒绝 = 全网打不开）。
    """
    for net in _fake_ip_nets():
        if ip.version == net.version and ip in net:
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


def _unwrap_redirect(href: str) -> str:
    """把搜索引擎的跳转壳还原成真实目标 URL（拿不到就原样返回）。

    Bing 的结果链接是 `https://www.bing.com/ck/a?...&u=a1<base64url>`，
    base64 段以 `a1` 前缀标记——直接抽 `u=` 参数解出来，用户/模型看到的就是
    目标网址，而不是一长串几百字符的跳转壳（2026-09-13 实测：搜索结果里
    全是 `bing.com/ck/a?...` 噪声）。DDG 的 `uddg=` 已是明文，不走这里。
    """
    import base64
    from html import unescape

    href = unescape(href)  # HTML 里 & 写成 &amp;，不还原则 u 键变成 "amp;u" 取不到值
    try:
        query = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
    except ValueError:
        return href
    raw = (query.get("u") or [""])[0]
    if not raw:
        return href
    payload = raw[2:] if raw.startswith("a1") else raw
    try:
        decoded = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)).decode(
            "utf-8", errors="replace")
    except Exception:  # noqa: BLE001——解不开就退回原链，不影响搜索
        return href
    return decoded if decoded.startswith(("http://", "https://")) else href


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """禁止 urlopen 自动跟随重定向——每一跳都要重新过 SSRF 校验。

    urllib 默认自动跟随 3xx，且**不再校验新目标**：一个公网 URL 302 跳到
    http://169.254.169.254/latest/meta-data/ 就绕过了 _assert_public_url
    （云元数据窃取的经典 SSRF 手法）。这里让 3xx 直接抛出，由 _open_url
    逐跳校验后再跳。
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


def _open_url(url: str, timeout: int = 30, max_hops: int = 5):
    """打开 URL 并手动跟随重定向，逐跳做 SSRF 校验。

    返回 (response, 最终 URL)。目标被校验为内网时抛 ValueError，
    网络层失败抛 OSError（由调用方转成可读消息）。
    """
    opener = urllib.request.build_opener(_NoRedirect)
    current = url
    for _ in range(max_hops + 1):
        request = urllib.request.Request(current, headers={"User-Agent": _WEB_UA})
        try:
            return opener.open(request, timeout=timeout), current
        except urllib.error.HTTPError as error:
            if error.code not in (301, 302, 303, 307, 308):
                raise
            location = error.headers.get("Location")
            if not location:
                raise
            current = urllib.parse.urljoin(current, location)
            blocked = _assert_public_url(current)
            if blocked:  # 跳转目标指向内网 → 拒绝，不发请求
                raise ValueError(f"重定向被拦截：{blocked}")
    raise OSError(f"重定向次数超过 {max_hops} 次，已中止")


def _decode_body(raw: bytes, ctype: str) -> str:
    """按响应声明的编码解码正文；GBK 等中文页面不再乱码。

    优先级：Content-Type 的 charset → HTML 内 <meta charset> → UTF-8 兜底。
    2026-09-13 之前固定 UTF-8 解码，GBK 页面整页乱码。
    """
    charset = ""
    match = re.search(r"charset=([\w\-]+)", ctype or "", re.I)
    if match:
        charset = match.group(1)
    if not charset:
        head = raw[:4096].decode("ascii", errors="ignore")
        meta = re.search(r'(?i)<meta[^>]+charset=["\']?([\w\-]+)', head)
        if meta:
            charset = meta.group(1)
    for candidate in (charset, "utf-8"):
        if not candidate:
            continue
        try:
            return raw.decode(candidate, errors="strict")
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", errors="replace")


def _html_to_text(raw: bytes, ctype: str = "") -> str:
    from html import unescape

    text = _decode_body(raw, ctype)
    text = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", text)
    text = re.sub(r"(?i)<(br|/p|/div|/li|/h[1-6]|/tr)[^>]*>", "\n", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = unescape(text)
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


def web_fetch(url: str, max_chars: int = 0) -> str:
    """抓取一个 http(s) 网页，去除 HTML 标签后返回正文文本。

    适合查 API 文档、技术资料。仅 http/https，拒绝内网地址（防 SSRF），
    30 秒超时。正文默认全量返回（2026-09-11 放开，worklog §25：原先
    8000 字符硬上限，抓一份长文档要反复重试）；真超限时只内联开头预览
    并报出原文体量，完整内容落盘 output/spill/ 可随时取回（worklog §26）。
    max_chars>0 时才按该值截断。
    找资料的入口用 web_search。
    """
    safety._audit(f"[web_fetch] {url}")
    blocked = _assert_public_url(url)
    if blocked:
        return blocked
    try:
        with _open_url(url, timeout=30)[0] as resp:
            ctype = (resp.headers.get("Content-Type") or "").lower()
            raw = resp.read(8_000_000)
    except ValueError as error:  # 重定向跳到内网
        return str(error)
    except Exception as error:  # noqa: BLE001——网络错误回传给模型自行调整
        return f"抓取失败: {error!r}"
    text = _html_to_text(raw, ctype) if ("html" in ctype or not ctype) else _decode_body(raw, ctype)
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
        link = _unwrap_redirect(m.group(1))
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
