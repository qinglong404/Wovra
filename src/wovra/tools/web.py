"""网络工具：web_search（检索 API 优先，本地单通道兜底）与 web_fetch（SSRF 防护）。

2026-09-14 升级（参考 crawl4ai 的"干净文本"思路，零依赖）：
* 正文提取改为启发式密度打分（去导航/页脚，crawl4ai 的 clean-Markdown 思路）
* 结果缓存 output/cache/（URL→正文、query→搜索，TTL 控制）
* 内容协商：抓取前探 Accept: text/markdown 与 /llms.txt（crawl4ai 同款）

2026-09-16 检索改走专业 API（用户口径："检索好好写，走专业 API，本地的只保留
简单稳定功能"）：GAIA 评测（output/gaia/FINDINGS.md §1）证明自研抓取两头都烧——
DDG 遇 TLS 抖动、Bing 对中文长查询返无关页，再叠一层词面过滤后模型会收到
"网上没有资料"这种**错误结论**。现在：配了密钥只走供应商接口（排序交给供应商，
不再做词面相关性过滤）；没配或 API 挂了才退回单通道本地抓取，并把"可能有噪声"
显式写给模型。**Bing 裸抓已删除**——它是无关页的主要来源。
"""

import ipaddress
import json
import os
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from html import unescape
from pathlib import Path

from . import abort, limits, safety


# ---- 结果缓存（磁盘，output/cache/）-----------------------------------------

_CACHE_DIR = "output/cache"
_CACHE_TTL = int(os.environ.get("WOVRA_WEB_CACHE_TTL", "3600"))  # 秒，默认 1h
_CACHE_MAX_ENTRIES = 500

# 缓存键版本号（2026-09-15，other/TOOLING_REVIEW.md §1 附带问题）。
# 旧版把**截断后**的渲染结果写进缓存，且键里没有 max_chars/体量维度——
# 于是"再抓一次试试"永远拿到同一份残缺内容，把真问题盖住了。现在改为
# 缓存**完整正文**（meta + body 的 JSON），键加版本前缀，旧条目自然失效。
_CACHE_VERSION = "v3"


def _cache_dir() -> Path:
    path = Path(safety.workspace_root()) / _CACHE_DIR
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


def _fetch_cache_get(url: str) -> dict | None:
    """取抓取缓存：{"meta": …, "body": …}，**body 是完整正文**（不截断）。

    为什么单独一套（而不是直接用 `_cache_get`）：抓取缓存要存两段——原始
    体量/编码（meta）与完整正文（body）。截断只发生在**渲染给模型看**的那
    一步（`_render_fetch`），故同一次抓取能被任意 max_chars 复用，不会像
    旧实现那样被第一次调用的小额度"固化"成残缺副本。
    """
    raw = _cache_get("fetch", f"{_CACHE_VERSION}:{url}")
    if raw is None:
        return None
    try:
        record = json.loads(raw)
    except ValueError:
        return None
    return record if isinstance(record, dict) and "body" in record else None


def _fetch_cache_put(url: str, meta: str, body: str) -> None:
    """写抓取缓存（完整正文 + meta）。"""
    _cache_put("fetch", f"{_CACHE_VERSION}:{url}",
               json.dumps({"meta": meta, "body": body}, ensure_ascii=False))


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


_TAG_RE = re.compile(r"(?s)<[^>]+>")
_SCRIPT_RE = re.compile(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>")
# 块级标签：切段的边界。导航/页脚/侧栏**必须**在列——它们是纯链接块，
# 单独切出来才能按"链接占比"判掉（TOOLING_REVIEW.md §1 的正文提取精度）。
_BLOCK_TAGS = r"p|div|section|article|main|nav|header|footer|aside|li|h[1-6]|tr|blockquote"
_BLOCK_SPLIT_RE = re.compile(rf"(?is)(</?(?:{_BLOCK_TAGS})[^>]*>)")
_BLOCK_MARKER_RE = re.compile(rf"(?is)^</?(?:{_BLOCK_TAGS})[^>]*>$")


def _visible_text(fragment: str) -> str:
    """片段 → 可见文本：去脚本/样式与**所有**标签、HTML 反转义、空白收敛。

    2026-09-15：旧实现只在"打分全不过"的兜底分支里剥标签，正文分支保留
    原始 HTML——`<td style=…>`、`<span class=…>` 这类行内标签的原文直接
    漏进正文（TOOLING_REVIEW.md §1 记的"错乱内联样式"）。
    """
    text = _SCRIPT_RE.sub(" ", fragment or "")
    text = _TAG_RE.sub(" ", text)
    return " ".join(unescape(text).split())


def _link_text(block: str) -> str:
    """块内**链接文本**——判定"这块像不像导航"的信号。"""
    return " ".join(_visible_text(m.group(0))
                    for m in re.finditer(r"(?is)<a\b.*?</a>", block))


def _block_text_density(block: str) -> float:
    """正文密度（0–1）：可见文本里"非链接"的占比 × 短块衰减。

    2026-09-15 改口径（TOOLING_REVIEW.md §1 的 P0 缺陷）：旧公式是
    `有效字符数 / 块总长度`，**分母含标签与内联样式**。厂商文档的 Modbus
    寄存器表每格都带 `style="padding:4px"`，整块密度落到 0.07 → 整张协议
    表被当成导航丢弃（实测 25/25 行丢失，寄存器地址表全没了）。

    密度要度量的是"这块像不像导航"，而导航的特征是**链接密集**，不是
    "标签多"。故分母改成可见文本长度，分子改成"非链接文本"。

    短块衰减：只压掉 1–2 个字的碎片（"•"、"©"），**不再压掉短标题**——
    page_text 场景下"渲染标题"这类 4–6 字的块正是要读的内容，衰减太重会把
    它当噪声丢掉（实测：`min(1, len/30)` 会把 `<h1>渲染标题</h1>` 整块滤掉）。
    """
    text = _visible_text(block)
    if not text:
        return 0.0
    link_len = len(_link_text(block))
    return (1.0 - link_len / len(text)) * min(1.0, len(text) / 6.0)


def _html_to_text(raw: bytes, ctype: str = "") -> str:
    """正文提取：按正文密度打分的启发式（去导航/页脚），零依赖。

    1. 内容协商拿到的干净文本（`_fetch_with_accept`）在 web_fetch 里优先，
       不走这里；这里是 HTML 通道。
    2. 按块级标签切段、逐段打分：链接密集的（导航/页脚/侧栏）丢弃，其余
       保留。**保留块一律剥成可见文本**——正文里不再混进标签原文。
    3. 表结构扁平化成"每格一行"（`<table>` 内的标签换成空字符串而不是
       行分隔），寄存器表/参数表这类**协议数据**才不会在切段时连成一片。
    """
    text = _decode_body(raw, ctype)
    text = _SCRIPT_RE.sub(" ", text)
    sections = _preserve_tables(text)
    kept: list[str] = []
    for section in sections:
        if section.startswith("\x00"):          # 表格：已扁平化成文本，不打分
            body = section[1:]
        else:
            chunks: list[str] = []
            for raw_piece in _BLOCK_SPLIT_RE.split(section):
                # 打分必须在**原始片段**上做：剥成可见文本后链接信息就没了，
                # 导航块会被误判成正文（2026-09-15 实施中真踩到）
                if _block_text_density(raw_piece) < 0.25:
                    continue
                visible = _visible_text(raw_piece)
                if visible:
                    chunks.append(visible)
            body = "\n".join(chunks)
        body = body.strip()
        if body:
            kept.append(body)
    if not kept:  # 打分全不过（极端页面），退回粗剥标签
        text = re.sub(r"(?i)<(br|/p|/div|/li|/h[1-6]|/tr)[^>]*>", "\n", text)
        kept = [_visible_text(text)]
    return "\n".join(kept)


def _preserve_tables(text: str) -> list[str]:
    """把 `<table>…</table>` 摘出来**扁平化**，其余部分原样返回。

    表格是文档里信息密度最高、也最容易被"按块打分"整块丢掉的形态
    （TOOLING_REVIEW.md §1：Modbus 寄存器表全丢）。这里只做三件事：
    单元格标签（td/th/tr…）换成空字符串 → 每格自然占一行；其余标签换
    行分隔符保住块边界；脚本/样式已在上游剔除。

    返回列表元素以 `\\x00` 开头的是**已完成扁平化**的表格文本，其余是
    仍需切段打分的普通 HTML。
    """
    parts: list[str] = []
    pos = 0
    for match in re.finditer(r"(?is)<table\b.*?</table>", text):
        parts.append(text[pos:match.start()])
        table = match.group(0)
        for tag in ("td", "th", "tr", "thead", "tbody", "tfoot", "th", "caption"):
            table = re.sub(rf"(?is)</?{tag}\b[^>]*>", "", table)
        table = re.sub(r"(?is)</?(?:table|col|colgroup)\b[^>]*>", "\n", table)
        flat = "\n".join(line for line in (_visible_text(part)
                                           for part in re.split(r"(?i)<br\s*/?>", table))
                         if line.strip())
        parts.append("\x00" + flat)
        pos = match.end()
    parts.append(text[pos:])
    return parts


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
    """抓取一个 http(s) 网页，去除 HTML 标签后返回正文文本。JS 渲染站（自己
    抽不到正文时）会自动换用外部抓取服务，结果里会标明正文来自谁。

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
    budget = int(max_chars) if max_chars and int(max_chars) > 0 else 0
    cached = _fetch_cache_get(url)
    if cached is not None:
        return _render_fetch(url, cached.get("meta", ""), cached.get("body", ""),
                             budget, from_cache=True)
    try:
        # ① 站点根页先探 /llms.txt（llms.txt 约定，站点级入口）
        llms_url = _llms_txt_url(url)
        if llms_url:
            blocked = _assert_public_url(llms_url)
            if not blocked:
                llms = _fetch_with_accept(llms_url)
                if llms:
                    meta = f"[{url}] {llms[1]}"
                    _fetch_cache_put(url, meta, llms[0])
                    return _render_fetch(url, meta, llms[0], budget)
        # ② 目标页本身内容协商
        md = _fetch_with_accept(url)
        if md:
            meta = f"[{url}] {md[1]}"
            _fetch_cache_put(url, meta, md[0])
            return _render_fetch(url, meta, md[0], budget)
        # ③ 普通 HTML 抓取
        with _open_url(url, timeout=30)[0] as resp:
            ctype = (resp.headers.get("Content-Type") or "").lower()
            raw = resp.read(8_000_000)
    except ValueError as error:  # 重定向跳到内网
        return str(error)
    except Exception as error:  # noqa: BLE001——网络错误回传给模型自行调整
        return f"抓取失败: {error!r}"
    text = _html_to_text(raw, ctype) if ("html" in ctype or not ctype) else _decode_body(raw, ctype)
    # 渲染兜底（2026-09-16）：自己的抽取拿不到像样正文时（典型是 JS 渲染站、或正文全在
    # 脚本里），按顺序问两个外部服务要 Markdown——**TinyFish Fetch 优先**（免费、150
    # URL/分、能渲染 JS），没配或失败才落到 Firecrawl（按次计费）。这个顺序的意义就是把
    # 付费那条降成最后手段。用户口径：搜索 API 找 URL、抓取服务取内容——接在**同一次**
    # web_fetch 里，agent 不必自己串两步。
    if len(text.strip()) < _FIRECRAWL_MIN_CHARS:
        thin = len(text.strip())
        rich, source = _tinyfish_fetch(url), "TinyFish Fetch"
        if rich is None:
            rich, source = _firecrawl_markdown(url), "Firecrawl"
        if rich:
            meta = (f"[{url}] {source} 提取（本地抽取只得 {thin} 字符，"
                    f"判为 JS 渲染或正文在脚本里）")
            _fetch_cache_put(url, meta, rich)
            return _render_fetch(url, meta, rich, budget)
    if not text.strip():
        return f"URL 无文本内容（Content-Type: {ctype}）。"
    meta = f"[{url}] Content-Type: {ctype or '未知'}，抓取 {len(raw)} 字节"
    _fetch_cache_put(url, meta, text)
    return _render_fetch(url, meta, text, budget)


def _tinyfish_fetch(url: str) -> str | None:
    """用 TinyFish Fetch 抓单页 Markdown（**免费**，能渲染 JS）；失败/没配返回 None。

    与 Firecrawl 是同类角色（内容提取），但按文档 "Fetch never draws from your
    wallet"（任何余额下都免费），限流 150 URL/分按 key 计、单次最多 10 个 URL，
    `ttl=0` 表示不吃缓存。所以它排在同一位置的**前面**——付费那条降成最后手段。
    """
    key = _search_key("tinyfish")
    if not key:
        return None
    try:
        data = _http_json(
            "https://api.fetch.tinyfish.ai",
            payload={"urls": [url], "format": "markdown", "ttl": 0},
            headers={"X-API-Key": key})
    except Exception:  # noqa: BLE001——兜底通道，任何失败都当作"没这回事"
        return None
    results = data.get("results") or []
    if not results:
        return None
    text = str((results[0] or {}).get("text") or "").strip()
    return text or None


def _firecrawl_markdown(url: str) -> str | None:
    """用 Firecrawl 把单页转成干净 Markdown；没配密钥或失败都返回 None（静默降级）。

    Firecrawl 严格说不是搜索服务而是**内容提取**服务（scrape/crawl/map/extract），
    与检索 API 是搭档关系：搜到 URL 之后用它取正文。故它同时出现在两个位置——
    `web_search` 的 `/v2/search`，以及这里的 `/v2/scrape`。
    """
    key = _search_key("firecrawl")
    if not key:
        return None
    try:
        data = _http_json(
            "https://api.firecrawl.dev/v2/scrape",
            payload={"url": url, "formats": ["markdown"], "onlyMainContent": True},
            headers={"Authorization": f"Bearer {key}"})
    except Exception:  # noqa: BLE001——兜底通道，任何失败都当作"没这回事"
        return None
    payload = data.get("data") if isinstance(data.get("data"), dict) else {}
    markdown = str(payload.get("markdown") or "").strip()
    return markdown or None


def _render_fetch(url: str, meta: str, body: str, budget: int,
                  from_cache: bool = False) -> str:
    """把**完整正文**渲染成给模型看的文本：源体量 → 截断提示 → 按需取回。

    2026-09-15（TOOLING_REVIEW.md §1）：旧实现有两处让调用方无法判断
    信息是否完整——① 只报"抓取 N 字节"（那是原始 HTML 体量，不是正文），
    ② `max_chars` 砍掉的部分静默消失。现在两者都明写：

        [url] Content-Type: …，原始 353,442 字节 → 正文 23,291 字符
        ⚠ 本次返回 4,000 字符（已按 max_chars 截断）；完整正文 23,291 字符
          在 output/spill/…：read_file(path, start_line=…, num_lines=…) 可取回。

    工具结果超长时仍走 `limits.clip` 的落盘预览通道（worklog §26），
    但**截断提示在渲染期就给出**，不依赖那个上限是否被撞到。
    """
    head = meta
    if from_cache:
        head = f"[缓存命中 {url}]\n{meta}"
    note = ""
    if budget and len(body) > budget:
        saved = limits.spill(body, "web_fetch")
        where = (f"完整正文已落盘 {saved}：read_file('{saved}', start_line=…, "
                 f"num_lines=…) 可取回任意区间。" if saved else
                 "落盘失败；请调大 max_chars 或去掉该参数重抓（默认全量）。")
        note = (f"⚠ 本次返回 {budget:,} 字符（已按 max_chars 截断）；"
                f"完整正文 {len(body):,} 字符。{where}")
    elif not budget:
        head += f" → 正文 {len(body):,} 字符"
    out = f"{head}\n\n{body}"
    if note:
        out = f"{note}\n\n{out}"
    return limits.clip(out, "web_fetch")

# ---- 搜索：相关性判定 ---------------------------------------------------------

# 停用词（判相关性时用，中英各一份）。只挡"这种词人人都命中"，不做词干化——
# 无依赖、可解释，效果足够。
# 短功能词必须在内（2026-09-15 实测）：查询 `how to parse PNG header in pure
# python` 的广告条目靠 "python" + "in" 凑够 2 个命中就混过了闸——去掉这类词
# 之后它只剩 1 个有效命中，被正确滤掉。
_STOPWORDS = {
    "the", "and", "for", "with", "how", "what", "why", "you", "your", "from",
    "that", "this", "are", "was", "were", "into", "not", "but", "its", "it's",
    "in", "to", "of", "on", "is", "as", "at", "by", "or", "an", "be", "we",
    "no", "so", "if", "do", "up", "out", "off", "over", "use", "using", "via",
    "per", "new", "can", "get", "all", "any", "may", "also", "than", "then",
    "官方", "文档", "说明", "教程", "怎么", "如何", "什么", "为什么", "以及",
}
_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]{2,}")
_ASCII_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.+-]{1,}")

# 判相关性的门（TOOLING_REVIEW.md §2）：2 个查询词命中即算相关——一个常见词
# 偶然命中不足以判相关，而真实相关的条目标题/摘要里往往有一串词。
#
# 2026-09-16 改口径：这个门**不再当硬闸**用。GAIA 实测（FINDINGS.md §1）里
# Bing 的 10 条候选被它全滤掉，于是返回"未找到相关结果"，模型据此判定"查不到"
# 而放弃——把"我的词面判据太糙"读成了"网上没有"。现在它只用来**标注**本地兜底
# 结果的可信度（见 _screen_local），硬剔除只留给广告/引擎壳页。
_MIN_HITS = 2


def _query_terms(query: str) -> tuple[set[str], set[str]]:
    """查询词 → (中文词集合, 非中文词集合)。CJK 按整个连续串收（粗但有效）。"""
    cjk = {run for run in _CJK_RUN_RE.findall(query) if run not in _STOPWORDS}
    ascii_words = {w.lower() for w in _ASCII_TOKEN_RE.findall(query)
                   if w.lower() not in _STOPWORDS and len(w) > 1}
    return cjk, ascii_words


# 广告/推广行的特征（2026-09-15 实测）：DDG 的广告是
# `duckduckgo.com/y.js?ad_domain=…`（点进去才跳外部站），Bing 是 `bing.com/aclick`。
# 它们是**买来的位置**，不是检索结果——混在结果里比无关结果更坏（看起来最相关）。
_AD_MARKERS = (
    "duckduckgo.com/y.js", "bing.com/aclick", "bing.com/ck/a?!&&p=",
    "googleadservices.com", "doubleclick.net",
    # 引擎自家的推广/帮助壳页（2026-09-16 实测）：DDG 把广告位伪装成
    # `duckduckgo.com/duckduckgo-help-pages/company/ads-by-microsoft-…`，
    # 正文是广告词且含查询词，路径名单认不全 → 补上。
    "duckduckgo.com/duckduckgo-help-pages", "duckduckgo.com/about",
)
# 引擎自家域：检索引擎站内的页面**本来就不该**是检索答案（是壳页或导航页）。
# 这条是上面路径名单的兜底——对方换一个 help 路径就绕过了。
_AD_HOSTNAMES = (
    "duckduckgo.com", "bing.com", "google.com",
    "googleadservices.com", "doubleclick.net",
)


def _is_ad(link: str) -> bool:
    """结果链接是否是广告/推广或引擎自家壳页（都不是自然结果）。"""
    low = (link or "").lower()
    if any(marker in low for marker in _AD_MARKERS):
        return True
    host = urllib.parse.urlparse(low).hostname or ""
    return any(host == h or host.endswith("." + h) for h in _AD_HOSTNAMES)


def _relevant(query: str, title: str, snippet: str, url: str = "") -> bool:
    """结果与查询词是否有词面重叠（大小写不敏感；URL 也算证据）。

    2026-09-15（TOOLING_REVIEW.md §2）：Bing 对中文长查询会返回**完全无关**
    的条目（实测查询"智元 OmniPicker 夹爪 Modbus 485 通信协议 寄存器 说明"
    返回日本汉字字典页），旧实现把这些当合法结果交出去——比"未找到"更有害，
    模型会据此判定"网上没有资料"。这里做最后一道闸：不相关的条目**不呈现**。
    """
    haystack = f"{title} {snippet} {url}".lower()
    if _is_ad(url):
        return False
    cjk, ascii_words = _query_terms(query)
    hits = 0
    for run in cjk:
        if run in haystack:                      # 中文：整串出现才算命中
            hits += 1
    for word in ascii_words:
        if re.search(rf"(?<![a-z0-9]){re.escape(word)}(?![a-z0-9])", haystack):
            hits += 1
    if not cjk and not ascii_words:              # 查询没有可用词（极短）：不筛
        return True
    return hits >= _MIN_HITS


def _format_results(query: str, engine: str, rows: list[tuple[str, str, str]],
                    filtered: int = 0, note: str = "") -> str:
    """结果行 → 给模型看的文本（可选一条前置提示 note）。"""
    lines = []
    for i, (title, link, snippet) in enumerate(rows, start=1):
        lines.append(f"{i}. {title}\n   {link}" + (f"\n   {snippet}" if snippet else ""))
    head = f"（引擎: {engine}）搜索 {query!r} 的结果（前 {len(lines)} 条"
    if filtered:
        head += f"，另有 {filtered} 条广告/壳页已剔除"
    head += "）：\n\n"
    return (f"⚠ {note}\n\n" if note else "") + head + "\n\n".join(lines)


def _search_ddg(query: str, max_results: int) -> tuple[list, int, str] | str:
    """本地兜底通道：**只**打 DDG lite 一个端点；返回 (结果行, 剔除数, 提示) 或失败文本。

    2026-09-16 改成单端点 + 短超时（`WOVRA_LOCAL_SEARCH_TIMEOUT`，默认 5s，最坏
    约 10 秒——urllib 的 timeout 实测按连接/读取各算一次）。原先 lite/html 两端点
    各 30s，DDG 抖动时一次失败要 **120s**；这条通道只是兜底，为它付两分钟是错的。
    html 端点此前已被反爬拿下（HTTP 202 + 空壳页，零结果节点），留着只会多一段等待。

    2026-09-15 的历史：主端点 `html.duckduckgo.com` 被反爬拿下后，旧解析器永远
    报"无结果或被限流"，把检索能力整条让给 Bing（而 Bing 对中文长查询给的是
    无关噪声）。lite 同源、结构更简单，故改为 lite。
    """
    url = "https://lite.duckduckgo.com/lite/?q=" + urllib.parse.quote_plus(query)
    request = urllib.request.Request(url, headers={"User-Agent": _WEB_UA})
    try:
        with urllib.request.urlopen(request, timeout=_LOCAL_SEARCH_TIMEOUT) as resp:
            html = resp.read(1_000_000).decode("utf-8", errors="replace")
    except Exception as error:  # noqa: BLE001——网络错误回传给模型自行调整
        return f"duckduckgo 失败: {error!r}"
    rows = _parse_ddg(html)
    if not rows:
        return "duckduckgo 无结果或被限流。"
    return _screen_local(query, rows, max_results)


def _parse_ddg(html: str) -> list[tuple[str, str, str]]:
    """解析 DDG 结果页（lite 与 html 两种结构都认）。

    lite 的链接带 `class='result-link'`（单引号）且套在 `<td>` 里、末尾带
    `rel="nofollow"`；html 版是 `class="result__a"`——旧正则要求
    `class` 在前、href 在后且用双引号，lite 一个都匹配不上。这里改成
    位置无关（href 与 class 顺序任意、引号单双都收）。
    """
    rows: list[tuple[str, str, str]] = []
    body = re.sub(r"(?is)<link\b[^>]*>|<link[^>]*/>", " ", html)  # <link rel=…> 不是结果
    pattern = re.compile(
        r"<a\b(?=[^>]*class=['\"](?:result-link|result__a)['\"])"
        r"(?=[^>]*href=['\"]([^'\"]+)['\"])[^>]*>(.*?)</a>", re.S | re.I)
    for match in pattern.finditer(body):
        href, title = match.group(1), _visible_text(match.group(2))
        link = _ddg_target(href)
        window = body[match.end():match.end() + 3000]
        snippet = ""
        for marker in (r"class=['\"]result-snippet['\"]", r"class=['\"]result__snippet['\"]"):
            m = re.search(marker + r"[^>]*>(.*?)</(?:td|a|div)>", window, re.S | re.I)
            if m:
                snippet = _visible_text(m.group(1))[:200]
                break
        if title and link.startswith(("http://", "https://")):
            rows.append((title, link, snippet))
    return rows


def _ddg_target(href: str) -> str:
    """DDG 结果链接 → 真实目标 URL。

    两种形态都要还原：`//duckduckgo.com/l/?uddg=<urlencoded>`（协议相对 +
    跳转壳）与直接的 `https://…`。旧实现只认 `uddg=`，而 lite 端点给的是
    协议相对地址，不补 `https:` 前缀整批结果都会被当成非 http 链接丢掉。
    """
    href = unescape(href)
    m = re.search(r"[?&]uddg=([^&]+)", href)
    if m:
        href = urllib.parse.unquote(m.group(1))
    if href.startswith("//"):
        href = "https:" + href
    return _unwrap_redirect(href)


# ---- 检索 API（专业后端，2026-09-16，用户口径"检索走专业 API"）-------------
#
# 六家支持：都只要一个密钥、都是 JSON over HTTPS，stdlib urllib 就够，
# **不引入新依赖**（项目口径：6 个直接依赖、网络层零依赖）。
#   tavily    POST api.tavily.com/search          body.api_key + Bearer
#   serper    POST google.serper.dev/search       X-API-KEY
#   exa       POST api.exa.ai/search              x-api-key
#   bocha     POST api.bochaai.com/v1/web-search  Bearer（中文检索）
#   serpapi   GET  serpapi.com/search             api_key 查询参数（供应商约定）
#   firecrawl POST api.firecrawl.dev/v2/search    Bearer
#   tinyfish  GET  api.search.tinyfish.ai         X-API-Key（**免费**，30 次/分）
# 供应商给的排序就是结论，**不再做词面相关性过滤**——那是本地兜底才需要的补丁。
#
# 默认**随机**挑一家先试，失败换下一家，全都不行才本地兜底（用户口径
# 2026-09-16："其它默认随机选一个，如果失败换下一个。最后由本地保底"）。
# 随机的意义是把调用摊到各家额度上——免费档都不大，不该只烧第一家。
# 用 `Wovra_SEARCH_PROVIDER` 可以把某家钉在第一位（它挂了仍会往后轮）。

_SEARCH_KEY_VARS: dict[str, tuple[str, ...]] = {
    "tavily": ("Wovra_SEARCH_KEY", "Wovra_Tavily", "TAVILY_API_KEY"),
    "serper": ("Wovra_SEARCH_KEY", "Wovra_Serper", "SERPER_API_KEY"),
    "exa": ("Wovra_SEARCH_KEY", "Wovra_Exa", "EXA_API_KEY"),
    "bocha": ("Wovra_SEARCH_KEY", "Wovra_Bocha", "BOCHA_API_KEY"),
    "serpapi": ("Wovra_SEARCH_KEY", "Wovra_SerpAPI", "Wovra_Serpapi",
                "SERPAPI_API_KEY"),
    "firecrawl": ("Wovra_SEARCH_KEY", "Wovra_Firecrawl", "FIRECRAWL_API_KEY"),
    "tinyfish": ("Wovra_SEARCH_KEY", "Wovra_TinyFish", "Wovra_Tinyfish",
                 "TINYFISH_API_KEY"),
}
_SEARCH_TIMEOUT = int(os.environ.get("WOVRA_SEARCH_TIMEOUT", "30"))
# 本地兜底通道的超时。实测 urllib 的 timeout 会被算**两次**（连接与读取各一次，
# 本机代理路径下抓包确认：timeout=10 → 实测 20.0s），所以这里取 5s，最坏约 10 秒
# 就有结论——兜底通道不该让人等两分钟（改前的 lite+html 双端点各 30s 实测 120s）。
_LOCAL_SEARCH_TIMEOUT = int(os.environ.get("WOVRA_LOCAL_SEARCH_TIMEOUT", "5"))

# 自己的正文抽取少于这么多字符，就判为"没抽到东西"，改问 Firecrawl 要 Markdown
# （它按次计费，普通页面不惊动它）
_FIRECRAWL_MIN_CHARS = int(os.environ.get("WOVRA_FIRECRAWL_MIN_CHARS", "200"))
_dotenv_loaded = False


def _ensure_dotenv() -> None:
    """懒加载仓库根 .env（与 llm.py 同一个文件、同幂等语义）。

    工具读的是**调用期**环境变量，而 .env 由 llm.py 在导入期加载；脚本或测试
    直接调 web_search 时那条路径可能还没跑过，密钥就白配了。

    路径按**向上找 pyproject.toml**定位，不写死层数：本文件在
    `src/wovra/tools/` 下，比 `llm.py` 深一层，写死 `parents[2]` 会指到
    `src/`（实测踩到：密钥全读不到，检索静默退回本地兜底）。
    """
    global _dotenv_loaded
    if _dotenv_loaded:
        return
    _dotenv_loaded = True
    try:
        from dotenv import load_dotenv
        here = Path(__file__).resolve()
        root = next((parent for parent in here.parents
                     if (parent / "pyproject.toml").exists()),
                    here.parents[1])
        load_dotenv(dotenv_path=root / ".env")
    except Exception:  # noqa: BLE001——缺 dotenv 或缺 .env 都不该让检索挂掉
        pass


def search_provider() -> str:
    """本次实际要用的检索供应商（**只用于展示**，真跑用 `_search_order`）。

    `Wovra_SEARCH_PROVIDER` 显式指定即以它为准；否则给出已配密钥里的第一家
    （这里的"第一"按注册表顺序，是确定性的，方便探针脚本打印口径一致）。
    """
    _ensure_dotenv()
    picked = (os.environ.get("Wovra_SEARCH_PROVIDER") or "").strip().lower()
    if picked:
        return picked
    configured = [name for name in _SEARCH_KEY_VARS if _search_key(name)]
    return configured[0] if configured else ""


def _search_order() -> list[str]:
    """本次要依次尝试的供应商：显式指定的排第一，其余已配密钥的**随机**排后。

    随机的意义（用户口径 2026-09-16）：把调用摊到各家额度上——免费档都不大，
    只烧第一家既浪费额度也把风险集中在一家。失败就换下一家，全试完才本地兜底。
    """
    _ensure_dotenv()
    configured = [name for name in _SEARCH_KEY_VARS if _search_key(name)]
    random.shuffle(configured)
    explicit = (os.environ.get("Wovra_SEARCH_PROVIDER") or "").strip().lower()
    if explicit:
        configured = [explicit] + [name for name in configured if name != explicit]
    return configured


def _search_key(provider: str) -> str:
    """取某供应商的密钥（永不写进审计行或结果文本）。"""
    _ensure_dotenv()
    for var in _SEARCH_KEY_VARS.get(provider, ()):
        value = (os.environ.get(var) or "").strip()
        if value:
            return value
    return ""


def _http_json(url: str, *, payload: dict | None = None,
               headers: dict | None = None) -> dict:
    """发一次 JSON 请求（无 payload 走 GET，有则 POST）；HTTP 错误交给调用方翻译。"""
    head = {"User-Agent": _WEB_UA, "Accept": "application/json"}
    head.update(headers or {})
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        head["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=head)
    with urllib.request.urlopen(request, timeout=_SEARCH_TIMEOUT) as resp:
        body = resp.read(2_000_000).decode("utf-8", errors="replace")
    return json.loads(body or "{}")


def _api_rows(provider: str, data: dict) -> list[tuple[str, str, str]]:
    """供应商响应 → (标题, 链接, 摘要)。各家字段名不同，只在这一处适配。"""
    def _clean(value) -> str:
        return _visible_text(unescape(str(value or "")))[:300]

    if provider == "tavily":
        return [(_clean(r.get("title")), r.get("url") or "", _clean(r.get("content")))
                for r in data.get("results") or []]
    if provider == "serper":
        return [(_clean(r.get("title")), r.get("link") or "", _clean(r.get("snippet")))
                for r in data.get("organic") or []]
    if provider == "serpapi":
        return [(_clean(r.get("title")), r.get("link") or "", _clean(r.get("snippet")))
                for r in data.get("organic_results") or []]
    if provider == "bocha":
        # 博查把内容套在 data 里（也可能直接给顶层），两种都认
        payload = data.get("data") if isinstance(data.get("data"), dict) else data
        raw = ((payload.get("webPages") or {}).get("value")) or []
        return [(_clean(r.get("name")), r.get("url") or "",
                 _clean(r.get("summary") or r.get("snippet"))) for r in raw]
    if provider == "firecrawl":
        payload = data.get("data")
        raw = payload.get("web") if isinstance(payload, dict) else payload
        return [(_clean(r.get("title")), r.get("url") or "",
                 _clean(r.get("description"))) for r in raw or []]
    if provider == "tinyfish":
        return [(_clean(r.get("title")), r.get("url") or "", _clean(r.get("snippet")))
                for r in data.get("results") or []]
    if provider == "exa":
        rows = []
        for r in data.get("results") or []:
            snippet = r.get("text") or " ".join(r.get("highlights") or [])
            rows.append((_clean(r.get("title")), r.get("url") or "", _clean(snippet)))
        return rows
    return []


def _api_search(provider: str, query: str, max_results: int) -> list | str:
    """调供应商检索：成功返回结果行，失败返回可读文本（由调用方决定是否兜底）。"""
    if provider not in _SEARCH_KEY_VARS:
        return (f"未知检索供应商 {provider!r}：Wovra_SEARCH_PROVIDER 只支持 "
                f"{'/'.join(_SEARCH_KEY_VARS)}。")
    key = _search_key(provider)
    if not key:
        last_var = _SEARCH_KEY_VARS.get(provider, ("Wovra_SEARCH_KEY",))[-1]
        return (f"{provider} 没配密钥：在 .env 里填 Wovra_SEARCH_KEY（或 {last_var}），"
                f"参考 .env.example。")
    quoted = urllib.parse.quote_plus(query)
    try:
        if provider == "tavily":
            data = _http_json(
                "https://api.tavily.com/search",
                payload={"api_key": key, "query": query, "max_results": max_results,
                         "search_depth": "basic", "include_answer": False},
                headers={"Authorization": f"Bearer {key}"})
        elif provider == "serper":
            data = _http_json(
                "https://google.serper.dev/search",
                payload={"q": query, "num": max_results},
                headers={"X-API-KEY": key})
        elif provider == "serpapi":
            # SerpAPI 的约定就是把密钥放**查询参数**（不是我们的选择）。它是唯一
            # 这样的后端；结果渲染只取标题/链接/摘要，不回显请求 URL，故密钥不会
            # 进给模型的文本。
            data = _http_json(
                "https://serpapi.com/search"
                f"?engine=google&q={quoted}&num={max_results}&api_key={key}")
        elif provider == "bocha":
            data = _http_json(
                "https://api.bochaai.com/v1/web-search",
                payload={"query": query, "count": max_results, "summary": True},
                headers={"Authorization": f"Bearer {key}"})
        elif provider == "firecrawl":
            data = _http_json(
                "https://api.firecrawl.dev/v2/search",
                payload={"query": query, "limit": max_results},
                headers={"Authorization": f"Bearer {key}"})
        elif provider == "tinyfish":
            # 免费通道（30 请求/分，按 key 计）。它还有 domain_type/recency_minutes/
            # include_domains 这些专属过滤（论文按 pub_year 筛），暂未接到工具签名上
            # ——签名一变就是一次前缀冷启，等真有需求再开。
            data = _http_json(
                "https://api.search.tinyfish.ai"
                f"?query={quoted}&result_limit={max_results}",
                headers={"X-API-Key": key})
        else:                                   # exa
            data = _http_json(
                "https://api.exa.ai/search",
                payload={"query": query, "numResults": max_results,
                         "contents": {"text": {"maxCharacters": 300}}},
                headers={"x-api-key": key})
    except urllib.error.HTTPError as error:
        hint = {401: "密钥无效或未授权", 403: "密钥无权访问该接口",
                429: "配额用尽或被限流"}.get(error.code, "接口报错")
        return f"{provider} API 失败（HTTP {error.code}：{hint}）。"
    except Exception as error:  # noqa: BLE001——网络/JSON 异常都回传给模型自行调整
        return f"{provider} API 失败: {error!r}"
    rows = [row for row in _api_rows(provider, data) if row[0] and row[1]]
    if not rows:
        return f"{provider} API 返回了 0 条结果。"
    return rows[:max_results]


def _screen_local(query: str, rows: list[tuple[str, str, str]],
                  max_results: int) -> tuple[list, int, str]:
    """本地抓取的结果筛选：(结果行, 剔除条数, 给模型的提示)。

    硬动作只留一件：**剔广告与引擎壳页**（买来的位置、导航页冒充答案，比无关页更坏）。
    词面相关性降级为**标注**——GAIA 实测（FINDINGS.md §1）证明把它当硬闸会让模型
    得出"网上没有资料"的错误结论：Bing 的 10 条候选被全滤掉后，那句"未找到相关
    结果"读起来就是"查过了，没有"。现在不够相关的行照给，但配一条显式警告，把
    判断权交回模型。
    """
    kept, ads, off_topic = [], 0, 0
    for title, link, snippet in rows:
        if _is_ad(link):
            ads += 1
            continue
        if not _relevant(query, title, snippet, link):
            off_topic += 1
        if len(kept) < max_results:
            kept.append((title, link, snippet))
    note = ""
    if kept and off_topic >= len(kept):
        note = (f"本地抓取**没做相关性保证**（{off_topic} 条与查询词无词面重叠，可能是"
                f"噪声）；**不要据此判定「网上没有资料」**。要更稳请在 .env 里配置"
                f"检索 API（见 .env.example）。")
    return kept, ads, note


def web_search(query: str, max_results: int = 8) -> str:
    """网页搜索，返回标题、链接与摘要。用于查技术文档与解决方案。

    配了检索 API（`.env` 里 `Wovra_Tavily` / `Wovra_Serper` / `Wovra_Exa` /
    `Wovra_Bocha` / `Wovra_SerpAPI` / `Wovra_Firecrawl` / `Wovra_TinyFish` 任一，
    见 .env.example）就轮流调那几家接口：每次随机挑一家先试，失败换下一家。
    返回的排序即结论，
    不做词面过滤。全都不行、或一家都没配时才退回本地抓取，结果会标注"本地兜底"
    并声明"没有相关性保证"——那种结果只能当参考，**不能**用来判定"网上没有资料"。
    结果缓存 output/cache/（TTL 默认 1h）；只缓存成功结果，失败不缓存（否则
    一次限流会被记一小时）。
    """
    safety._audit(f"[web_search] {query}")
    max_results = max(1, min(int(max_results), 20))
    order = _search_order()
    # 缓存键 = 版本 + 通道类型 + query：随机轮换下具体是哪家服务的不进键
    # （换了家也还是"API 检索结果"，否则同一 query 会为每家各存一份）；
    # 解析/过滤口径一变旧条目自然失效（旧版只带版本，实测踩到过回显噪声）
    cache_key = f"{_CACHE_VERSION}:{'api' if order else 'local'}:{query}"
    cached = _cache_get("search", cache_key)
    if cached is not None:
        return f"[缓存命中] {cached}"
    signals: list[str] = []
    for provider in order:
        outcome = _api_search(provider, query, max_results)
        if not isinstance(outcome, str):
            out = _format_results(query, provider, outcome)
            _cache_put("search", cache_key, out)
            return out
        signals.append(outcome)             # 这家失败：记下原因，换下一家
    local = _search_ddg(query, max_results)
    if isinstance(local, str):
        signals.append(local)
    else:
        rows, dropped, note = local
        if rows:
            engine = ("ddg 本地兜底（API 都不可用）" if order
                      else "ddg（本地，未配置检索 API）")
            if signals:                 # API 挂在哪一步要让用户看见（否则以为兜底是常态）
                note = signals[0] + (("\n" + note) if note else "")
            out = _format_results(query, engine, rows, dropped, note)
            _cache_put("search", cache_key, out)
            return out
        signals.append("本地兜底只拿到广告位/引擎壳页，已全部剔除。")
    return _no_result_text(bool(order), signals)


def _no_result_text(has_api: bool, signals: list[str]) -> str:
    """所有通道都没内容时的说明（一家都没配时顺带告诉用户怎么配）。"""
    head = ("检索失败：配置的 API 与本地兜底都没拿到内容。"
            if has_api else
            "未配置检索 API，本地兜底通道也没拿到内容。")
    tail = "可换更短的查询词重试，或用 web_fetch 直接抓已知网址（比如厂商官网页）。"
    if not has_api:
        tail = ("在 .env 里配置检索 API 会稳得多：Wovra_Tavily / Wovra_Serper / "
                "Wovra_Exa / Wovra_Bocha / Wovra_SerpAPI / Wovra_Firecrawl 任填其一"
                f"即可自动生效（见 .env.example）。{tail}")
    return head + "\n" + "\n".join(signals) + "\n" + tail


# ---- 云浏览器自动化（TinyFish Agent API，2026-09-16）------------------------
#
# 与上面两条的区别：search/fetch 是"取信息"，这个是"让浏览器替你操作"——按自然语言
# 目标点页面、翻页、填表、读 SPA 渲染出来的东西。**它要花钱**（$0.016/步，走 TinyFish
# wallet），所以不做任何自动轮换/降级，只在你或 agent 显式调用时跑。
# 端点三选一（/run 同步、/run-async 起后轮询、/run-sse 流式），这里用流式：能拿到
# 逐步轨迹（进账本），也能在"停止本轮"时立刻断开。

_AGENT_ENDPOINT = "https://agent.tinyfish.ai/v1/automation/run-sse"
# 读流时单次 socket 等待上限：服务端有 HEARTBEAT，正常远小于它
_AGENT_READ_TIMEOUT = int(os.environ.get("WOVRA_AGENT_TIMEOUT", "60"))
_AGENT_MAX_SECONDS = 1800


def _sse_events(resp):
    """从 SSE 响应里逐行解出 `data:` 事件（非 JSON 行直接跳过，心跳也不占内存）。"""
    for raw in resp:
        line = raw.decode("utf-8", errors="replace").strip()
        if not line.startswith("data:"):
            continue
        try:
            event = json.loads(line[5:].strip())
        except ValueError:
            continue
        if isinstance(event, dict):
            yield event


def _render_automation(url: str, goal: str, event: dict, trail: list[str],
                       elapsed: float) -> str:
    """把 COMPLETE 事件渲染成给模型看的文本（轨迹 + 结果，或失败原因 + 需要用户做什么）。"""
    status = str(event.get("status") or "未知")
    result = event.get("result")
    if isinstance(result, dict) and "result" in result:
        result = result.get("result")
    lines = [
        f"TinyFish 浏览器自动化：{status}（耗时 {elapsed:.0f}s，{len(trail)} 步）",
        f"目标：{goal}",
        f"页面：{url}",
    ]
    if trail:
        lines.append("执行轨迹：\n  - " + "\n  - ".join(trail[-12:]))
    if status == "COMPLETED":
        lines.append("结果：\n" + (str(result).strip() if result else "（接口没给结果文本）"))
    else:
        lines.append(f"失败原因：{str(event.get('error') or '').strip() or '（未提供）'}")
        hint = event.get("profile_hint")
        if isinstance(hint, dict) and hint.get("message"):
            lines.append("需要你操作：" + str(hint["message"])
                         + (f"（{hint.get('setup_url')}）" if hint.get("setup_url") else ""))
    return "\n".join(lines)


def web_automate(url: str, goal: str, max_duration_seconds: int = 300) -> str:
    """用云浏览器按自然语言目标完成一次网页操作（TinyFish，**按步计费**）。

    适用场景：页面必须真交互才拿得到东西——登录后翻页、点筛选/展开、填表提交、
    读 SPA 渲染出来的数据、必须真实浏览器才成立的流程。纯静态取正文别用它，用
    web_fetch（更快、更省，也不花钱）。

    目标是自然语言，写清"要看什么/要做什么"；`max_duration_seconds` 是墙上时间
    上限（默认 300 秒，最大 1800）。它跑在对方的云浏览器上，所以：**停止本轮会直接
    断开这条流**；页面内容会经过对方基础设施（涉密页面不要用）；失败时会说明原因，
    需要你登录的站点会给出提示而不是硬试。
    """
    safety._audit(f"[web_automate] {url} :: {goal[:120]}")
    blocked = _assert_public_url(url)
    if blocked:
        return blocked
    if not goal.strip():
        return "缺少 goal：请用自然语言说明要在这个页面上做什么。"
    key = _search_key("tinyfish")
    if not key:
        return ("web_automate 需要 TinyFish 密钥：在 .env 里填 Wovra_TinyFish"
                "（或 TINYFISH_API_KEY），见 .env.example。只要页面的静态正文用 "
                "web_fetch 即可，那条不需要密钥。")
    seconds = max(30, min(int(max_duration_seconds), _AGENT_MAX_SECONDS))
    payload = {"url": url, "goal": goal,
               "agent_config": {"max_duration_seconds": seconds}}
    request = urllib.request.Request(
        _AGENT_ENDPOINT, data=json.dumps(payload).encode("utf-8"),
        headers={"X-API-Key": key, "Content-Type": "application/json",
                 "Accept": "text/event-stream", "User-Agent": _WEB_UA})
    trail: list[str] = []
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=_AGENT_READ_TIMEOUT) as resp:
            for event in _sse_events(resp):
                if abort.abort_requested():          # 停止本轮：断开这条流
                    walked = " / ".join(trail[-6:]) or "（无）"
                    return (f"已中止（用户停止本轮）：TinyFish 运行已断开"
                            f"（run_id={event.get('run_id') or '未知'}，"
                            f"已走 {len(trail)} 步：{walked}）。")
                kind = str(event.get("type") or "")
                if kind == "PROGRESS":
                    step = str(event.get("purpose") or "").strip()
                    if step:
                        trail.append(step)
                        safety._audit(f"[web_automate][progress] {step[:120]}")
                elif kind == "COMPLETE":
                    return _render_automation(url, goal, event, trail,
                                              time.monotonic() - started)
    except urllib.error.HTTPError as error:
        hint = {401: "密钥无效", 402: "余额不足（它按步计费，$0.016/步）",
                403: "账号未开通该能力（capture_config / max_steps 都要 beta）",
                404: "运行不存在或接口不可用",
                429: "限流"}.get(error.code, "接口报错")
        return f"TinyFish 自动化失败（HTTP {error.code}：{hint}）。"
    except Exception as error:  # noqa: BLE001——网络/流异常都回传给模型
        return f"TinyFish 自动化失败: {error!r}"
    return (f"TinyFish 自动化流已结束但没等到 COMPLETE（等了 "
            f"{time.monotonic() - started:.0f}s）。已走轨迹："
            f"{' / '.join(trail[-6:]) or '（无）'}")
