"""Wovra 内置工具箱：文件读写、命令执行、审计与防护。

设计原则——"审计换权限"：

    * 能力上，agent 从"只读研究员"升级为能产出、能执行的执行者；
    * 代价上，每个变更类工具的完整内容都会被 Agent 记入任务历史
      （谁、何时、对哪个文件、做了什么、内容是什么），可追溯；
    * 破坏性操作不做"确认弹窗"（CLI 场景做不到良好交互），
      而是直接硬拒绝——宁可让模型换一种做法，也不赌运气。

所有路径类工具都通过 _safe_path 限制在项目根目录内，
防止模型读写项目之外的任何东西。
"""

import ipaddress
import itertools
import json
import locale
import os
import re
import subprocess
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path

# 项目根目录（工作区）：所有文件与命令都限定在这里。
# 解析顺序：
#   1. 环境变量 WOVRA_WORKSPACE（显式指定，脚本/自动化用）
#   2. 启动目录（Claude Code 惯例：在哪启动，工作区就在哪；
#      在 Wovra 仓库内启动时自动回退到仓库根，避免子目录 Surprise）
#   3. Wovra 仓库根目录（兜底）
# 注意：会话会绑定其工作区（见 task.py）——恢复旧会话时以会话记录为准。
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_cwd = Path.cwd().resolve()
if _cwd == _REPO_ROOT or _REPO_ROOT in _cwd.parents:
    PROJECT_ROOT = _REPO_ROOT
else:
    PROJECT_ROOT = _cwd
_env_ws = os.environ.get("WOVRA_WORKSPACE")
if _env_ws:
    PROJECT_ROOT = Path(_env_ws).resolve()
    PROJECT_ROOT.mkdir(parents=True, exist_ok=True)

# 搜索时跳过的噪声目录（依赖、缓存、运行时数据——搜索它们只有噪音）
_IGNORED_DIRS = {
    ".git", ".venv", "__pycache__", ".pytest_cache",
    "tasks", "output", "node_modules",
}

# 变更类工具的失败标记：ui（红色显示）和 task 报告（"失败："前缀）
# 共用这一份，判定口径保持一致
FAILURE_MARKERS = (
    "工具执行出错",
    "未知工具",
    "合法 JSON",
    "已拒绝执行危险命令",
    "命令执行失败（",
)

# 破坏性命令黑名单：子串匹配，宁可误杀不可放过。
# 有意保持保守——rm -r、git push 这类即使"看起来安全"也拒绝，
# 模型收到拒绝文本后会自行寻找替代方案（这是流式循环的好处）。
_DENIED_PATTERNS = (
    "rm -r",          # 递归删除（含 -rf/-fr）
    " -delete",       # find 的删除变体
    "sudo ",
    "mkfs",
    "dd if=",
    ":(){",           # fork 炸弹
    "git push",       # 对外发布，不由 agent 自主决定
    "git reset --hard",
    "git clean",
    "git checkout -- ",
    "git restore",
    "shutdown",
    "reboot",
    "chmod -R",
    "mv -f",
    "| sh",
    "| bash",
    "|zsh",
    "| sh;",
    "curl ",          # 下载外部内容（配合管道执行是常见攻击面），一律拒绝
    "wget ",
)

_COMMAND_TIMEOUT = 60  # 秒


# ---- 审计挂钩 ---------------------------------------------------------------
# Agent 绑定任务时通过 set_audit_recorder 注册回调；工具用它把
# "参数之外的事实"（如被覆盖文件的旧内容）写进任务历史。走旁路
# 而不是工具参数，是为了不污染模型看到的工具 schema。

_audit_recorder = None


def set_audit_recorder(fn) -> None:
    global _audit_recorder
    _audit_recorder = fn


def _audit(text: str) -> None:
    if _audit_recorder is not None:
        _audit_recorder(text)


def _safe_path(relative: str) -> Path:
    """把相对路径解析到项目根目录内，越界直接报错。"""
    path = (PROJECT_ROOT / relative).resolve()
    if not path.is_relative_to(PROJECT_ROOT):
        raise ValueError(f"路径越界，只允许访问项目目录内的文件: {relative}")
    return path


# ---- 只读工具 -------------------------------------------------------------


def list_files(directory: str = ".") -> list[str]:
    """列出项目内某个目录下的文件和子目录（不含递归）。"""
    path = _safe_path(directory)
    return sorted(p.name + ("/" if p.is_dir() else "") for p in path.iterdir())


def read_file(path: str, start_line: int = 1, num_lines: int = 200) -> str:
    """按行读取项目内一个文本文件的内容片段。需要通读整个文件时，
    按 num_lines=400 连续分段读取，不要零碎小段反复读。

    大文件请配合 search_files 先定位，再用 start_line/num_lines
    分段读取——单次最多 400 行，返回值会标明文件总行数和
    继续读取的位置。
    """
    target = _safe_path(path)
    try:
        text = target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return f"{path} 不是 UTF-8 文本文件（可能是二进制文件），无法按文本读取"
    _observe_file(target)  # 记录观察时的状态，供 edit/write 的过期保护比对
    lines = text.splitlines()
    total = len(lines)
    if total == 0:
        return f"{path} 是空文件"
    start = max(1, start_line)
    if start > total:
        return f"{path} 共 {total} 行，start_line={start} 超出范围"
    end = min(total, start + min(max(1, num_lines), 400) - 1)
    body = "\n".join(lines[start - 1:end])
    header = f"{path}（共 {total} 行，以下为第 {start}-{end} 行）"
    if end < total:
        body += f"\n...（后续还有 {total - end} 行，用 start_line={end + 1} 继续读取）"
    return f"{header}\n{body}"


def search_files(pattern: str, directory: str = ".", glob: str = "*") -> str:
    """在项目内用正则表达式搜索文本文件（类似 grep）。

    返回 `路径:行号: 行内容` 格式的匹配，最多 50 条；
    自动跳过 .git/.venv 等噪声目录。找"某个函数在哪定义"、
    "哪个文件用了某配置" 都靠它。
    """
    try:
        regex = re.compile(pattern)
    except re.error as error:
        raise ValueError(f"正则表达式无效: {error}") from error

    root = _safe_path(directory)
    matches: list[str] = []
    for path in sorted(root.rglob(glob)):
        if not path.is_file():
            continue
        if any(part in _IGNORED_DIRS for part in path.parts):
            continue
        if path.stat().st_size > 1_000_000:  # 跳过超大文件
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, PermissionError):
            continue
        for line_number, line in enumerate(text.splitlines(), start=1):
            if regex.search(line):
                # 统一用 / 分隔，输出跨平台一致（也便于回填给 read_file 等工具）
                relative = path.relative_to(PROJECT_ROOT).as_posix()
                matches.append(f"{relative}:{line_number}: {line.strip()[:200]}")
                if len(matches) >= 50:
                    return "\n".join(matches) + "\n...(已达 50 条上限，请缩小搜索范围)"
    if not matches:
        return f"无匹配：pattern={pattern!r}, directory={directory!r}, glob={glob!r}"
    return "\n".join(matches)


def glob_files(pattern: str, directory: str = ".") -> str:
    """按文件名通配模式查找文件（如 *.py、docs/**/*.md），返回相对路径。

    模式递归匹配所有子目录（*.py 等价于 **/*.py）；与 search_files
    （搜内容）互补：找"有哪些文件"用本工具，找"哪些文件里有什么
    内容"用 search_files。自动跳过 .git/.venv 等噪声目录，最多
    返回 200 条。
    """
    root = _safe_path(directory)
    filtered = [
        p for p in root.rglob(pattern) if p.is_file()
        and not any(part in _IGNORED_DIRS for part in p.relative_to(PROJECT_ROOT).parts)
    ]
    filtered.sort(key=lambda p: p.as_posix())
    if not filtered:
        return f"无匹配文件: {pattern}（directory={directory}）"
    lines = [p.relative_to(PROJECT_ROOT).as_posix() for p in filtered[:200]]
    more = f"\n…(共 {len(filtered)} 个，已显示前 200)" if len(filtered) > 200 else ""
    return "\n".join(lines) + more


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


def web_fetch(url: str, max_chars: int = 8000) -> str:
    """抓取一个 http(s) 网页，去除 HTML 标签后返回正文文本。

    适合查 API 文档、技术资料。仅 http/https，拒绝内网地址（防 SSRF），
    30 秒超时，正文最多返回 max_chars 字符。找资料的入口用 web_search。
    """
    _audit(f"[web_fetch] {url}")
    blocked = _assert_public_url(url)
    if blocked:
        return blocked
    max_chars = max(200, min(int(max_chars), 50_000))
    request = urllib.request.Request(url, headers={"User-Agent": _WEB_UA})
    try:
        with urllib.request.urlopen(request, timeout=30) as resp:
            ctype = (resp.headers.get("Content-Type") or "").lower()
            raw = resp.read(2_000_000)
    except Exception as error:  # noqa: BLE001——网络错误回传给模型自行调整
        return f"抓取失败: {error!r}"
    text = _html_to_text(raw) if ("html" in ctype or not ctype) else raw.decode("utf-8", errors="replace")
    if not text.strip():
        return f"URL 无文本内容（Content-Type: {ctype}）。"
    head = f"[{url}] Content-Type: {ctype or '未知'}，抓取 {len(raw)} 字节\n\n"
    tail = "\n…(正文已截断)" if len(text) > max_chars else ""
    return head + text[:max_chars] + tail


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
    _audit(f"[web_search] {query}")
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


def ask_user(question: str, choices: str = "") -> str:
    """就需求或编码细节向用户提问，等待用户在终端输入答案。

    choices 可选：用 | 分隔的候选项（如 "是|否|继续"）。非交互环境
    （重定向/管道）自动降级：建议模型基于已有信息继续。
    等待回答期间置 user_input_pending 标记（不计执行时长，同确认）。
    """
    import sys

    global _user_input_pending
    prompt = f"\n[模型提问] {question}"
    if choices:
        prompt += f"\n可选: {choices}"
    prompt += "\n你的回答> "
    if not sys.stdin.isatty():
        return "（非交互环境，无法获取用户输入。请基于已有信息继续，或在最终回答中说明假设。）"
    _user_input_pending = True
    try:
        answer = input(prompt)
    except EOFError:
        answer = ""
    finally:
        _user_input_pending = False
    return f"用户的回答: {answer or '（空）'}"


def list_background() -> str:
    """列出本会话启动的所有后台任务及其状态。"""
    if not _BACKGROUND_TASKS:
        return "当前没有后台任务。"
    lines = []
    for tid, entry in _BACKGROUND_TASKS.items():
        running = entry["proc"].poll() is None
        state = "运行中" if running else f"已退出（exit_code={entry['proc'].returncode}）"
        owner = entry.get("session") or "无主"
        alive = " [常驻]" if entry.get("keep_alive") else ""
        lines.append(f"{tid}  [{owner}] {state}{alive}  {entry['command'][:60]}")
    return "后台任务：\n" + "\n".join(lines)


    return "\n".join(lines) + more


# ---- 敏感操作确认 -------------------------------------------------------------
# 黑名单拦"绝对不做"的；确认层拦"有实际副作用但合法"的（安装依赖、
# 版本提交、删除/移动/权限变更）。交互环境 y/N 询问（默认拒绝）；
# 非交互环境自动放行并留审计标记——实验与脚本不被阻塞，代价记录在案。

# 工具是否正在等待用户输入（确认/提问）：等待期不算执行时长，
# 终端看门狗静默——用户的思考时间不是工具的时间，也不设超时。
_user_input_pending = False


def user_input_pending() -> bool:
    return _user_input_pending


_CONFIRM_PATTERNS = (
    r"\bgit\s+(commit|tag|merge|rebase|remote\s+add)\b",
    r"\b(pip|pip3)\s+install\b", r"\buv\s+(pip\s+)?(add|install|sync)\b",
    r"\bconda\s+(install|create|remove)\b",
    r"\bnpm\s+(install|i)\b", r"\bpnpm\s+(add|install)\b", r"\byarn\s+add\b",
    r"\b(rm|del|erase|rmdir)\b", r"\b(mv|move)\b",
    r"\b(chmod|chown|icacls)\b", r"\b(taskkill|kill)\b",
    r"\b(docker|podman)\s+(rm|rmi|system\s+prune)\b",
)


def _confirm_reason(command: str) -> str | None:
    """命令命中敏感操作 → 返回命中的模式；否则 None。"""
    for pattern in _CONFIRM_PATTERNS:
        if re.search(pattern, command):
            return pattern
    return None


def _ask_yes_no(question: str) -> bool:
    """交互环境 y/N 询问（默认拒绝）；非交互环境自动放行并留审计。

    等待用户回答期间置 user_input_pending 标记：用户思考多久都行
    （不设超时），但终端看门狗不计这段时间的执行时长。"""
    import sys

    global _user_input_pending
    if not sys.stdin.isatty():
        _audit("[确认] 非交互环境，自动放行")
        return True
    _user_input_pending = True
    try:
        answer = input(f"{question} [y/N] ").strip().lower()
    except EOFError:
        answer = ""
    finally:
        _user_input_pending = False
    # Ctrl+C 不吞：向上传播 = 打断本轮（统一的中断语义）
    return answer in ("y", "yes")


def get_current_time() -> str:
    """获取当前本地时间（ISO 格式）。"""
    from datetime import datetime

    return datetime.now().isoformat(timespec="seconds")


# ---- 文件观察注册表（过期保护） ---------------------------------------------
# 本进程读/写过的文件 → (mtime_ns, size)。edit_file/write_file 前核对：
# 文件在观察后被外部（用户、其他会话、其他进程）改动 → 拒绝执行并要求
# 重新 read_file，防止模型按过期的上下文内容覆盖用户的新修改。
# 本进程从未观察过的文件无从判断，保持原行为（write_file 有完整留底）。

_file_registry: dict[Path, tuple[int, int]] = {}


def _observe_file(path: Path) -> None:
    try:
        st = path.stat()
        _file_registry[path] = (st.st_mtime_ns, st.st_size)
    except OSError:
        _file_registry.pop(path, None)


def _stale_error(path: Path) -> str | None:
    """文件在观察后被外部修改 → 返回拒绝原因；否则 None。"""
    observed = _file_registry.get(path)
    if observed is None:
        return None
    try:
        st = path.stat()
    except OSError:
        return None
    if (st.st_mtime_ns, st.st_size) != observed:
        return (
            f"文件在你上次读取后已被外部修改（用户或其他进程）：{path}。"
            f"上下文里的内容可能已过期。请先 read_file 重新确认最新内容，"
            f"再决定如何修改。"
        )
    return None


# ---- 变更类工具（AUDITED_TOOLS，Agent 会做完整审计记录） --------------------


def write_file(path: str, content: str) -> str:
    """创建或整体覆盖项目内的一个文本文件。

    覆盖是全量的——只改一部分请用 edit_file，它要求唯一定位，
    误伤面小得多。覆盖时旧内容会通过审计挂钩完整留底，
    出问题可以对照还原。若文件在你上次读取后被外部修改过，
    会拒绝执行并要求重新确认。
    """
    target = _safe_path(path)
    stale = _stale_error(target)
    if stale:
        return stale
    existed = target.exists()
    old = None
    if existed:
        try:
            old = target.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            old = "(非 UTF-8 内容，未留底)"
    # 允许写到尚不存在的子目录（模型经常给出 "reports/xx.md" 这类路径）
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    _observe_file(target)
    action = "覆盖" if existed else "创建"
    if old is not None:
        # 旧内容完整留底（审计原则：能还原）；超大文件截断到 20000 字符
        backup = old if len(old) <= 20_000 else old[:20_000] + "\n...(已截断)"
        _audit(f"[write_file 旧内容备份] {path}:\n{backup}")
    return f"已{action} {path}（{len(content)} 字符）"


def _closest_anchor_hint(text: str, old_text: str,
                          threshold: float = 0.6) -> tuple[int, str] | None:
    """edit_file 锚点未命中时，找与 old_text 最接近的文件区域。

    返回 (行号, 该窗口首行)，供模型一次修正锚点，不必整文件重读。
    相似度用 SequenceMatcher 对等长行窗口逐一比对，阈值以下的
    完全不相关内容不产生提示。"""
    from difflib import SequenceMatcher

    lines = text.splitlines()
    if not lines or not old_text.strip():
        return None
    window = max(1, len(old_text.splitlines()))
    best_ratio, best = 0.0, None
    for start in range(0, max(1, len(lines) - window + 1)):
        chunk = "\n".join(lines[start:start + window])
        ratio = SequenceMatcher(None, chunk, old_text).ratio()
        if ratio > best_ratio:
            best_ratio, best = ratio, (start + 1, lines[start])
    if best is None or best_ratio < threshold:
        return None
    return best


def edit_file(path: str, old_text: str, new_text: str) -> str:
    """把文件中「恰好出现一次」的 old_text 替换为 new_text。

    强制唯一定位：找不到或出现多次都直接报错，让模型补充更多
    上下文再试。这是防止"替换了不想替换的地方"的关键约束。
    锚点未命中时会给出最接近内容的位置，帮助一次修正。
    若文件在你上次读取后被外部修改过，会拒绝执行并要求重新确认。
    """
    target = _safe_path(path)
    stale = _stale_error(target)
    if stale:
        return stale
    if not target.exists():
        return (
            f"文件不存在: {path}（解析为 {target}）。"
            f"先用 glob_files 确认文件的实际位置——注意 path 参数应只含路径本身。"
        )
    text = target.read_text(encoding="utf-8")
    count = text.count(old_text)
    if count == 0:
        message = f"{path} 中未找到待替换文本（前 80 字符: {old_text[:80]!r}）"
        hint = _closest_anchor_hint(text, old_text)
        if hint:
            message += f"。最接近的内容在第 {hint[0]} 行附近：{hint[1][:80]!r}"
        raise ValueError(message)
    if count > 1:
        raise ValueError(
            f"{path} 中待替换文本出现 {count} 次，请补充前后文使其唯一定位"
        )
    line_no = text.count("\n", 0, text.find(old_text)) + 1
    target.write_text(text.replace(old_text, new_text, 1), encoding="utf-8")
    _observe_file(target)
    # 替换片段对完整留底：改了哪段、改成了什么，一目了然
    _audit(f"[edit_file] {path}\n定位片段:\n{old_text}\n替换为:\n{new_text}")
    return (f"已修改 {path}（{len(old_text)} 字符 → {len(new_text)} 字符，"
            f"位于第 {line_no} 行附近）")


def _kill_process_tree(pid: int) -> None:
    """强杀 pid 及其全部后代进程。

    Windows 的 Popen.kill() 只杀 shell 本身（shell=True 时是
    cmd.exe），孙进程会变成孤儿活下来——既泄漏进程/端口，还攥着
    输出管道让清理阶段的 communicate 永久阻塞。整树强杀
    （taskkill /T）从根上杜绝这两件事。POSIX 侧配合
    start_new_session：子进程自成一个进程组，killpg 一网打尽。
    """
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True,
        )
        return
    import signal

    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except OSError:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def run_command(command: str, timeout: int | None = None) -> str:
    """在项目根目录运行一条 shell 命令，返回退出码与输出。默认 60 秒
    超时整树强杀（timeout 可调，1-600 秒）；不要运行前台常驻服务
    （http.server 之类永不退出的命令只会白等到超时）——预览网页用
    系统方式打开文件（Windows: start 文件名），长驻服务用
    run_background 后台启动。

    防护：
        * 黑名单匹配到破坏性模式时直接拒绝，不执行；
          拒绝文本会回传给模型（它能看到原因并换方案）
        * 超时强制终止整棵进程树，防止长命令卡死整个任务
        * 输出各截断 1500 字符，防止超长输出撑爆上下文
    """
    _audit(f"[run_command] {command}")  # 无论执行与否，命令原文都进审计
    for pattern in _DENIED_PATTERNS:
        if pattern in command:
            return (
                f"已拒绝执行危险命令：包含被禁止的模式 `{pattern}`。"
                f"如需完成类似效果，请使用更安全的替代方案。"
            )
    reason = _confirm_reason(command)
    if reason and not _ask_yes_no(
        f"命令包含敏感操作（命中 `{reason}`），是否允许执行？\n  {command[:200]}"
    ):
        _audit(f"[run_command][用户拒绝] {command}")
        return (
            "用户拒绝了该命令的执行。请换一种无副作用的做法，"
            "或向用户说明为什么需要它。"
        )
    wait = _COMMAND_TIMEOUT if timeout is None else max(1, min(int(timeout), 600))

    # 输出重定向到临时文件而不是 PIPE：文件没有"写端被孙进程攥住"
    # 的问题（http.server 这类孤儿进程曾把管道清理阶段永久挂死），
    # 也没有 PIPE 缓冲写满导致的子进程阻塞，超时后的清理必然可返回
    # 子进程强制 UTF-8 输出：否则中文输出按各自主观编码（gbk/utf-8）
    # 混流，父进程解码必出乱码（实测 py_compile 输出花屏）
    child_env = dict(os.environ, PYTHONUTF8="1")
    with tempfile.TemporaryFile() as out_f, tempfile.TemporaryFile() as err_f:
        proc = subprocess.Popen(
            command,
            shell=True,
            stdout=out_f,
            stderr=err_f,
            cwd=PROJECT_ROOT,  # 固定工作目录：相对路径都在项目内
            env=child_env,
            # POSIX：让子进程自成进程组，超时后 killpg 整组杀掉而不伤自身
            start_new_session=os.name != "nt",
            # Windows：新进程组默认免疫 Ctrl+C——用户中断对话不能带走
            # 正在运行的服务器/安装进程（同控制台进程会收到 CTRL_C）
            creationflags=(
                subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
            ),
        )
        try:
            proc.wait(timeout=wait)
        except subprocess.TimeoutExpired:
            _kill_process_tree(proc.pid)
            try:
                proc.wait(timeout=5)  # 整树已灭，这只是兜底限时
            except subprocess.TimeoutExpired:
                pass
            return f"命令执行失败（超时 {wait} 秒被强制终止）：{command[:200]}"

        out_f.seek(0)
        err_f.seek(0)
        stdout = out_f.read().decode("utf-8", errors="replace").strip() or "(无输出)"
        stderr = err_f.read().decode("utf-8", errors="replace").strip() or "(无输出)"

    header = (
        f"命令执行失败（exit_code={proc.returncode}）"
        if proc.returncode != 0
        else "exit_code=0"
    )
    return (
        f"{header}\n"
        f"stdout:\n{stdout[:1500]}\n"
        f"stderr:\n{stderr[:1500]}"
    )


# ---- 后台任务 ----------------------------------------------------------------
# 长驻进程（开发服务器、watcher、长安装）的后台运行与控制：启动即返回，
# 输出落日志文件，增量查看，整树强杀。控制句柄仅存活于本进程——
# Wovra 退出后进程仍在运行，日志保留在 .wovra-background/ 下。

_BACKGROUND_TASKS: dict[str, dict] = {}
_BACKGROUND_SEQ = itertools.count(1)
_CURRENT_SESSION: str | None = None


def set_current_session(task_id: str | None) -> None:
    """标记当前会话：后台任务按会话归属，防止跨会话误管。"""
    global _CURRENT_SESSION
    _CURRENT_SESSION = task_id
_BACKGROUND_LOG_DIR = PROJECT_ROOT / ".wovra-background"


def run_background(command: str, keep_alive: bool = False) -> str:
    """后台启动一条 shell 命令（服务器、监听、长安装等），立即返回任务 ID。

    输出写入日志文件；用 check_background 查看增量输出，
    stop_background 停止（整树强杀）。黑名单与 run_command 相同。
    任务归属启动它的会话：会话退出时默认一并关闭——需要跨会话存活的
    常驻进程（如长期开发服务器）设 keep_alive=True。
    """
    _audit(f"[run_background] {command}")
    for pattern in _DENIED_PATTERNS:
        if pattern in command:
            return (
                f"已拒绝执行危险命令：包含被禁止的模式 `{pattern}`。"
                f"如需完成类似效果，请使用更安全的替代方案。"
            )
    reason = _confirm_reason(command)
    if reason and not _ask_yes_no(
        f"后台命令包含敏感操作（命中 `{reason}`），是否允许启动？\n  {command[:200]}"
    ):
        _audit(f"[run_background][用户拒绝] {command}")
        return (
            "用户拒绝了该命令的启动。请换一种无副作用的做法，"
            "或向用户说明为什么需要它。"
        )
    task_id, proc = _launch_background(command, label="", keep_alive=keep_alive, shell=True)
    tag = "（常驻，会话退出后继续运行）" if keep_alive else ""
    return (
        f"后台任务 {task_id} 已启动（PID {proc.pid}）{tag}：{command[:120]}\n"
        f'查看输出: check_background(task_id="{task_id}") | '
        f'停止: stop_background(task_id="{task_id}")'
    )


def _launch_background(command, label: str, keep_alive: bool,
                       shell: bool = False) -> tuple[str, subprocess.Popen]:
    """后台启动进程并登记（run_background 与子任务派发共用的引擎）。

    command：shell=True 时为命令字符串；否则为 argv 列表。
    """
    _BACKGROUND_LOG_DIR.mkdir(parents=True, exist_ok=True)
    task_id = f"bg-{next(_BACKGROUND_SEQ)}"
    log_path = _BACKGROUND_LOG_DIR / f"{task_id}.log"
    env = dict(os.environ, PYTHONUTF8="1")
    with open(log_path, "wb") as log_file:
        proc = subprocess.Popen(
            command,
            shell=shell,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            cwd=PROJECT_ROOT,  # 固定工作目录：相对路径都在项目内
            env=env,
            start_new_session=os.name != "nt",  # 与 run_command 同一套树杀约定
            # Windows：新进程组免疫 Ctrl+C——用户中断主会话不能带走
            # 后台子任务（实测 0xC000013A 全军覆没的教训）
            creationflags=(
                subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
            ),
        )
    _BACKGROUND_TASKS[task_id] = {
        "proc": proc, "log": log_path, "pos": 0,
        "command": label or (command if isinstance(command, str) else " ".join(command)),
        "label": label, "session": _CURRENT_SESSION, "keep_alive": keep_alive,
    }
    return task_id, proc


def start_background_argv(argv: list, label: str = "", keep_alive: bool = False) -> str:
    """以 argv 列表后台启动一个进程（免 shell、免确认）。

    调用方是运行时自身（如子任务派发）而非模型，因此不经过危险命令
    清单与用户确认。输出写日志文件，归属当前会话（退出时一并清理）。
    """
    _audit(f"[start_background_argv] {label or ' '.join(argv)}")
    task_id, proc = _launch_background([str(a) for a in argv], label, keep_alive)
    return (
        f"后台任务 {task_id} 已启动（PID {proc.pid}）\n"
        f'查看输出: check_background(task_id="{task_id}")'
    )


def background_find(fragment: str) -> dict | None:
    """按标签或命令片段查找仍存活的后台任务条目（无则 None）。"""
    for entry in _BACKGROUND_TASKS.values():
        if fragment in (entry.get("label") or "") or fragment in (entry.get("command") or ""):
            if entry["proc"].poll() is None:
                return entry
    return None


def check_background(task_id: str) -> str:
    """查看后台任务的运行状态与增量输出（自上次查看以来的新增部分）。"""
    entry = _BACKGROUND_TASKS.get(task_id)
    if entry is None:
        return f"未找到后台任务: {task_id}（控制句柄仅在本会话内有效）。"
    if (error := _ownership_error(task_id, entry)):
        return error
    proc = entry["proc"]
    running = proc.poll() is None
    new_text = ""
    if entry["log"].exists():
        with open(entry["log"], "rb") as f:
            f.seek(entry["pos"])
            data = f.read()
        entry["pos"] += len(data)
        new_text = data.decode("utf-8", errors="replace")
    status = "运行中" if running else f"已退出（exit_code={proc.returncode}）"
    body = new_text.strip() or "（无新输出）"
    return f"[{task_id}] {status}\n{body[-2000:]}"


def _ownership_error(task_id: str, entry: dict) -> str | None:
    """跨会话管理防护：任务只归启动它的会话管。"""
    owner = entry.get("session")
    if owner and owner != _CURRENT_SESSION:
        return f"后台任务 {task_id} 由会话 {owner} 启动，请在那个会话中管理。"
    return None


def stop_session_backgrounds() -> int:
    """会话退出时停止当前会话启动的所有后台任务（keep_alive 除外）。

    返回停止的数量。进程崩溃时这些子进程会成为孤儿——日志仍在
    .wovra-background/ 下，可手动查看与清理。"""
    stopped = 0
    for tid, entry in list(_BACKGROUND_TASKS.items()):
        if entry.get("session") != _CURRENT_SESSION or entry.get("keep_alive"):
            continue
        proc = entry["proc"]
        if proc.poll() is None:
            _kill_process_tree(proc.pid)
        _BACKGROUND_TASKS.pop(tid, None)
        stopped += 1
    return stopped


def stop_background(task_id: str) -> str:
    """停止后台任务（整树强杀），返回退出状态。"""
    entry = _BACKGROUND_TASKS.get(task_id)
    if entry is None:
        return f"未找到后台任务: {task_id}（控制句柄仅在本会话内有效）。"
    if (error := _ownership_error(task_id, entry)):
        return error
    proc = entry["proc"]
    if proc.poll() is None:
        _kill_process_tree(proc.pid)
    try:
        code = proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        return f"后台任务 {task_id} 已发送终止信号但未退出，请稍后用 check_background 确认。"
    return f"后台任务 {task_id} 已停止（exit_code={code}）。"
