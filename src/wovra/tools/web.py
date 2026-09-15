"""网络工具：web_search（引擎注册表 + 回退）与 web_fetch（含 SSRF 防护）。

2026-09-14 一步到位升级（参考 crawl4ai 的"干净文本"思路，零依赖）：
* 正文提取改为启发式密度打分（去导航/页脚，crawl4ai 的 clean-Markdown 思路）
* 结果缓存 output/cache/（URL→正文、query→搜索，TTL 控制）
* 内容协商：抓取前探 Accept: text/markdown 与 /llms.txt（crawl4ai 同款）
* 搜索引擎注册表化：DDG → Bing → 备用引擎，失败自动降级
"""

import ipaddress
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import limits, safety


# ---- 结果缓存（磁盘，output/cache/）-----------------------------------------

_CACHE_DIR = "output/cache"
_CACHE_TTL = int(os.environ.get("WOVRA_WEB_CACHE_TTL", "3600"))  # 秒，默认 1h
_CACHE_MAX_ENTRIES = 500


def _cache_dir() -> Path:
    path = Path(safety.PROJECT_ROOT) / _CACHE_DIR
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return path


def _cache_key(kind: str, text: str) -> str:
    import hashlib

    return f"{kind}-{hashlib.sha256(text.encode('utf-8', errors='replace')).hexdigest()[:16]}"


def _cache_get(kind: str, key_text: str) -> str | None:
    """命中且未过期返回缓存内容；否则 None。过期条目顺手删。"""
    try:
        target = _cache_dir() / (_cache_key(kind, key_text) + ".txt")
        if not target.exists():
            return None
        try:
            age = time.time() - target.stat().st_mtime
        except OSError:
            return None
        if age > _CACHE_TTL:
            try:
                target.unlink()
            except OSError:
                pass
            return None
        return target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _cache_put(kind: str, key_text: str, value: str) -> None:
    """写入缓存；超过条数上限时清掉最老的（按 mtime）。失败静默。"""
    try:
        d = _cache_dir()
        target = d / (_cache_key(kind, key_text) + ".txt")
        target.write_text(value, encoding="utf-8")
        try:
            entries = sorted(d.glob("*.txt"), key=lambda p: p.stat().st_mtime)
        except OSError:
            entries = []
        if len(entries) > _CACHE_MAX_ENTRIES:
            for old in entries[: len(entries) - _CACHE_MAX_ENTRIES]:
                try:
                    old.unlink()
                except OSError:
                    pass
    except OSError:
        pass


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


def _block_text_density(block: str) -> float:
    """启发式：文本密度 = 有效字符数 / 总长度。

    导航/页脚/侧栏的典型特征是"链接密集、正文稀疏"——文本密度低。
    crawl4ai 的 clean-Markdown 也是这个思路（按内容块打分，去噪留正文）。
    块内 `a` 标签越少、纯文本越多，密度越接近 1。
    """
    text_only = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", block)
    text_only = re.sub(r"(?is)<a\b[^>]*>.*?</a>", " ", text_only)  # 链接文本不算正文
    text_only = re.sub(r"(?s)<[^>]+>", " ", text_only)
    text_only = " ".join(text_only.split())
    total = len(re.sub(r"\s+", "", block))
    if total == 0:
        return 0.0
    return len(text_only) / total


def _html_to_text(raw: bytes, ctype: str = "") -> str:
    """正文提取：按文本密度打分的启发式（去导航/页脚），零依赖。

    1. 先用内容协商拿到的干净文本（_fetch_markdown / _fetch_llms_txt）
       优先返回——那些是站点自己给的正文，质量最高。
    2. 否则按块级标签切段，逐段做文本密度打分：密度低于阈值
       （链接/导航密集）的段落丢弃，高于阈值的拼成正文。
    """
    from html import unescape

    text = _decode_body(raw, ctype)
    text = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", text)
    # 块级标签切段，逐段打分
    blocks = re.split(r"(?is)(</?(?:p|div|section|article|main|li|h[1-6]|tr|blockquote)[^>]*>)", text)
    kept: list[str] = []
    current = ""
    for piece in blocks:
        if re.match(r"(?is)^</?(?:p|div|section|article|main|li|h[1-6]|tr|blockquote)[^>]*>$", piece):
            if current.strip() and _block_text_density(current) >= 0.25:
                kept.append(current)
            current = ""
        else:
            current += piece
    if current.strip() and _block_text_density(current) >= 0.25:
        kept.append(current)
    if kept:
        text = "\n".join(kept)
    else:  # 打分全不过（极端页面），退回原逻辑
        text = re.sub(r"(?i)<(br|/p|/div|/li|/h[1-6]|/tr)[^>]*>", "\n", text)
        text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = unescape(text)
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


def _fetch_with_accept(url: str, timeout: int = 30) -> tuple[str, str] | None:
    """内容协商：先探 `Accept: text/markdown`（crawl4ai 同款，E 档调研）。

    站点支持时直接返回干净 Markdown，绕过 HTML 解析。返回 (内容, 描述)
    或 None（不支持/失败）。SSRF 校验在调用前已做，重定向仍逐跳校验。
    """
    try:
        with _open_url(url, timeout=timeout)[0] as resp:
            ctype = (resp.headers.get("Content-Type") or "").lower()
            if "text/markdown" in ctype or "text/x-markdown" in ctype:
                return _decode_body(resp.read(8_000_000), ctype), \
                    f"站点返回 Markdown（Content-Type: {ctype}）"
    except Exception:  # noqa: BLE001——内容协商失败不致命，退回 HTML
        return None
    return None


def _llms_txt_url(url: str) -> str | None:
    """取站点的 /llms.txt 候选地址（E 档调研的 llms.txt 约定）。

    只在根路径的 URL 上探（llms.txt 是站点级约定，挂在域名根），
    避免对每个深层页都多发一次请求。
    """
    parsed = urllib.parse.urlparse(url)
    if parsed.path not in ("", "/"):
        return None
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, "/llms.txt", "", "", ""))


def web_fetch(url: str, max_chars: int = 0) -> str:
    """抓取一个 http(s) 网页，去除 HTML 标签后返回正文文本。

    适合查 API 文档、技术资料。仅 http/https，拒绝内网地址（防 SSRF），
    30 秒超时。正文默认全量返回（2026-09-11 放开，worklog §25：原先
    8000 字符硬上限，抓一份长文档要反复重试）；真超限时只内联开头预览
    并报出原文体量，完整内容落盘 output/spill/ 可随时取回（worklog §26）。
    max_chars>0 时才按该值截断。

    2026-09-14 升级（参考 crawl4ai 干净文本思路）：
    * 结果缓存 output/cache/：同一 URL 短时间重复抓不再走网络（TTL 默认 1h）
    * 内容协商：站点支持 text/markdown 或 /llms.txt 时直接拿干净文本
    * 正文提取：启发式密度打分去导航/页脚（见 _html_to_text）
    找资料的入口用 web_search。
    """
    safety._audit(f"[web_fetch] {url}")
    blocked = _assert_public_url(url)
    if blocked:
        return blocked
    cached = _cache_get("fetch", url)
    if cached is not None:
        return f"[缓存命中 {url}]\n{cached}"
    try:
        # ① 站点根页先探 /llms.txt（llms.txt 约定，站点级入口）
        llms_url = _llms_txt_url(url)
        if llms_url:
            blocked = _assert_public_url(llms_url)
            if not blocked:
                llms = _fetch_with_accept(llms_url)
                if llms:
                    head = f"[{url}] {llms[1]}\n\n"
                    body = llms[0] if max_chars <= 0 else llms[0][:int(max_chars)]
                    out = limits.clip(head + body, "web_fetch")
                    _cache_put("fetch", url, out)
                    return out
        # ② 目标页本身内容协商
        md = _fetch_with_accept(url)
        if md:
            head = f"[{url}] {md[1]}\n\n"
            body = md[0] if max_chars <= 0 else md[0][:int(max_chars)]
            out = limits.clip(head + body, "web_fetch")
            _cache_put("fetch", url, out)
            return out
        # ③ 普通 HTML 抓取
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
    out = limits.clip(head + body, "web_fetch")
    _cache_put("fetch", url, out)
    return out


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


def _search_engine(query: str, max_results: int, engine: str) -> str:
    """按引擎名分发。失败统一返回以"… 失败/无结果"开头的可识别文本。"""
    if engine == "ddg":
        return _search_ddg(query, max_results)
    if engine == "bing":
        return _search_bing(query, max_results)
    return f"{engine} 未知引擎"


def web_search(query: str, max_results: int = 8) -> str:
    """网页搜索，返回标题、链接与摘要。用于查技术文档与解决方案。

    引擎按注册表顺序尝试（DDG → Bing → 备用），失败自动降级到下一个；
    全部失败时回传各自原因。结果缓存 output/cache/（TTL 默认 1h）：
    同一 query 短时间重复搜直接命中缓存，不再打网络。
    """
    safety._audit(f"[web_search] {query}")
    max_results = max(1, min(int(max_results), 20))
    cached = _cache_get("search", query)
    if cached is not None:
        return f"[缓存命中] {cached}"
    engines = ("ddg", "bing")
    errors = []
    for engine in engines:
        result = _search_engine(query, max_results, engine)
        if result.startswith(("duckduckgo 失败", "duckduckgo 无结果",
                              "bing 失败", "bing 无结果", f"{engine} 未知引擎")):
            errors.append(result)
            continue
        out = f"（引擎: {engine}）{result}"
        _cache_put("search", query, out)
        return out
    out = ("所有搜索通道都失败了：\n" + "\n".join(errors)
           + "\n可稍后重试，或用 web_fetch 直接抓取已知网址。")
    _cache_put("search", query, out)
    return out
