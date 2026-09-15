"""网络工具：web_search（引擎注册表 + 回退）与 web_fetch（含 SSRF 防护）。

2026-09-14 一步到位升级（参考 crawl4ai 的"干净文本"思路，零依赖）：
* 正文提取改为启发式密度打分（去导航/页脚，crawl4ai 的 clean-Markdown 思路）
* 结果缓存 output/cache/（URL→正文、query→搜索，TTL 控制）
* 内容协商：抓取前探 Accept: text/markdown 与 /llms.txt（crawl4ai 同款）
* 搜索引擎注册表化：DDG → Bing → 备用引擎，失败自动降级
"""

import ipaddress
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from html import unescape
from pathlib import Path

from . import limits, safety


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
    if not text.strip():
        return f"URL 无文本内容（Content-Type: {ctype}）。"
    meta = f"[{url}] Content-Type: {ctype or '未知'}，抓取 {len(raw)} 字节"
    _fetch_cache_put(url, meta, text)
    return _render_fetch(url, meta, text, budget)


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
# 偶然命中不足以判相关，而真实相关的条目标题/摘要里往往有一串词。宁可漏报
# 让**所有**引擎结果都被判无关（返回"未找到相关结果"）也不错报成噪声。
_MIN_HITS = 2


def _query_terms(query: str) -> tuple[set[str], set[str]]:
    """查询词 → (中文词集合, 非中文词集合)。CJK 按整个连续串收（粗但有效）。"""
    cjk = {run for run in _CJK_RUN_RE.findall(query) if run not in _STOPWORDS}
    ascii_words = {w.lower() for w in _ASCII_TOKEN_RE.findall(query)
                   if w.lower() not in _STOPWORDS and len(w) > 1}
    return cjk, ascii_words


# 广告/推广行的 host 特征（2026-09-15 实测）：DDG 的广告是
# `duckduckgo.com/y.js?ad_domain=…`（点进去才跳外部站），Bing 是 `bing.com/aclick`。
# 它们是**买来的位置**，不是检索结果——混在结果里比无关结果更坏（看起来最相关）。
_AD_HOSTS = (
    "duckduckgo.com/y.js", "bing.com/aclick", "bing.com/ck/a?!&&p=",
    "googleadservices.com", "doubleclick.net",
)


def _is_ad(link: str) -> bool:
    """结果链接是否是广告/推广（不是自然结果）。"""
    low = (link or "").lower()
    return any(marker in low for marker in _AD_HOSTS)


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
                    filtered: int = 0) -> str:
    """结果行 → 给模型看的文本（含被相关性过滤掉几条的说明）。"""
    lines = []
    for i, (title, link, snippet) in enumerate(rows, start=1):
        lines.append(f"{i}. {title}\n   {link}" + (f"\n   {snippet}" if snippet else ""))
    head = f"（引擎: {engine}）搜索 {query!r} 的结果（前 {len(lines)} 条"
    if filtered:
        head += f"，另有 {filtered} 条判定为无关已过滤"
    head += "）：\n\n"
    return head + "\n\n".join(lines)


def _search_ddg(query: str, max_results: int) -> tuple[list, int] | str:
    """DDG 检索（lite 端点优先，html 端点兜底）；返回 (结果行, 过滤条数) 或失败文本。

    2026-09-15：主端点 `html.duckduckgo.com` 已被反爬拿下——实测对请求
    返回 **HTTP 202 + 空壳页**（14KB、零个结果节点），旧解析器于是永远报
    "无结果或被限流"，把检索能力整条让给 Bing（而 Bing 对中文长查询给的是
    无关噪声，见 §2）。`lite.duckduckgo.com` 同源、结构更简单且实测可用，
    故改成 lite 优先。
    """
    for endpoint in ("https://lite.duckduckgo.com/lite/",
                     "https://html.duckduckgo.com/html/"):
        url = endpoint + "?q=" + urllib.parse.quote_plus(query)
        request = urllib.request.Request(url, headers={"User-Agent": _WEB_UA})
        try:
            with urllib.request.urlopen(request, timeout=30) as resp:
                html = resp.read(1_000_000).decode("utf-8", errors="replace")
        except Exception as error:  # noqa: BLE001——换端点再试
            last = f"duckduckgo 失败: {error!r}"
            continue
        rows = _parse_ddg(html)
        if rows:
            return _screen(query, rows, max_results)
        last = "duckduckgo 无结果或被限流。"
    return last


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


def _search_bing(query: str, max_results: int) -> tuple[list, int] | str:
    """Bing 检索；返回 (结果行, 过滤条数) 或失败文本。

    无结果页要能认出来：Bing 在查无结果时会返回 `b_no` 提示块（旧实现只
    找 `b_algo`，会把提示块里的 `b_algo` 片段当成结果节出来）。故：
    有 `b_no` 标记 → 直接报"无结果"，不再解析。
    """
    url = ("https://www.bing.com/search?q=" + urllib.parse.quote_plus(query)
           + "&setlang=zh-hans")
    request = urllib.request.Request(url, headers={
        "User-Agent": _WEB_UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"})
    try:
        with urllib.request.urlopen(request, timeout=30) as resp:
            html = resp.read(1_000_000).decode("utf-8", errors="replace")
    except Exception as error:  # noqa: BLE001
        return f"bing 失败: {error!r}"
    if 'class="b_no"' in html or "b_no " in html:
        return "bing 无结果（查询词未命中任何索引页）。"
    rows: list[tuple[str, str, str]] = []
    for chunk in html.split('<li class="b_algo"')[1:]:
        m = re.search(r'<h2[^>]*><a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', chunk, re.S | re.I)
        if not m:
            continue
        link = _unwrap_redirect(m.group(1))
        title = _visible_text(m.group(2))
        p = re.search(r"<p[^>]*>(.*?)</p>", chunk, re.S | re.I)
        snippet = _visible_text(p.group(1))[:200] if p else ""
        if title:
            rows.append((title, link, snippet))
    if not rows:
        return "bing 无结果或返回了无法解析的页面。"
    return _screen(query, rows, max_results)


def _screen(query: str, rows: list[tuple[str, str, str]],
            max_results: int) -> tuple[list, int] | str:
    """相关性过滤 + 截断条数；全被滤掉时返回"未找到相关结果"（宁缺毋滥）。

    TOOLING_REVIEW.md §2 的改进建议 3：**宁缺毋滥**——没有相关结果就明说，
    不拿无关内容充数，因为"搜过了但没有"是模型做后续决策的依据。
    """
    kept, filtered = [], 0
    for title, link, snippet in rows:
        if _relevant(query, title, snippet, link):
            kept.append((title, link, snippet))
        else:
            filtered += 1
        if len(kept) >= max_results:
            break
    if not kept:
        return (f"未找到相关结果（{len(rows)} 条候选与查询词无词面重叠，已全部过滤）。"
                f"可换更短的查询词，或直接用 web_fetch 抓已知网址。")
    return kept, filtered


def _search_engine(query: str, max_results: int, engine: str):
    """按引擎名分发。失败返回可识别文本；成功返回 (结果行, 过滤条数)。"""
    if engine == "ddg":
        return _search_ddg(query, max_results)
    if engine == "bing":
        return _search_bing(query, max_results)
    return f"{engine} 未知引擎"


def web_search(query: str, max_results: int = 8) -> str:
    """网页搜索，返回标题、链接与摘要。用于查技术文档与解决方案。

    引擎按注册表顺序尝试（DDG → Bing），失败或**结果全被相关性过滤**时
    自动降级到下一个；全部无果时明说"未找到相关结果"，不拿无关内容充数
    （2026-09-15，TOOLING_REVIEW.md §2）。结果缓存 output/cache/（TTL 默认
    1h）：同一 query 短时间重复搜直接命中缓存，不再打网络。
    """
    safety._audit(f"[web_search] {query}")
    max_results = max(1, min(int(max_results), 20))
    # 缓存键带版本（同 fetch）：解析器/过滤口径一变，旧条目就不可用——
    # 否则"再搜一次"拿到的是上一版代码留下的噪声（实测踩到：修完解析器
    # 仍回显旧的无关结果，把修复盖住了）
    cache_key = f"{_CACHE_VERSION}:{query}"
    cached = _cache_get("search", cache_key)
    if cached is not None:
        return f"[缓存命中] {cached}"
    signals = []
    for engine in ("ddg", "bing"):
        outcome = _search_engine(query, max_results, engine)
        if isinstance(outcome, str):            # 失败/无结果：记下原因换下一家
            signals.append(outcome)
            continue
        rows, filtered = outcome
        out = _format_results(query, engine, rows, filtered)
        _cache_put("search", cache_key, out)
        return out
    out = ("未找到相关结果：所有搜索通道都没给出与查询词相关的内容。\n"
           + "\n".join(signals)
           + "\n可换更短的查询词重试，或用 web_fetch 直接抓已知网址"
             "（比如厂商官网页）。")
    _cache_put("search", cache_key, out)
    return out
