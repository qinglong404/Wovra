"""眼睛（工具层）：screenshot 截图落盘 + view_image 看图回读。

为什么有它（2026-09-13 用户拍板「将眼睛和 diff 都做了吧」）：

Wovra 此前能"看 DOM、看不见像素"——界面类交付只能靠人眼后置验收，
历次会话为此现写 ad-hoc 截图脚本（`.shots/*.png` 44 张、`output/_png_check.py`），
每验一次重写一次，正好违反 AGENTS.md §0「先跑仪器再写脚本」。
机制缺口在本仓库被记过三次（`docs/session-log-20260907.md` §五-5、
`docs/organization-runtime-v1.md:251`、`docs/frontend-visualization-plan.md:21`）。

**两条实测事实决定了本模块的形态（`scripts/probe_vision_channel.py` 当日实跑）**：

1. 本端点（commandcode / deepseek-v4.1-flash）**user 通道能看图**——
   两张纯色图颜色全答对（文件名与提示词都不含颜色词，排除了靠猜）；
2. **tool 通道传图被端点 400 拒**（`param: messages.2.content`）——
   所以多模态图片**不能内联在工具结果字符串里**。

于是设计成：`view_image` 只返回一行**引用标记**（纯文本，可落 task.json），
图片本体由**装配期**（`agent/assembly.py::_eye_image_parts`）展开成
user 消息的 image parts 追加在尾部。好处：task.json 不被 base64 撑爆
（真实会话已 16MB 级）、引用是纯文本可检索、追加在尾部不破坏前缀缓存。

零新依赖：截图用本机 chrome/msedge 的 `--headless --screenshot`（实测可用），
像素统计用纯 stdlib 解 PNG（zlib + 反滤波），不引 Pillow。
"""

import base64
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import zlib
from pathlib import Path

from . import safety

# view_image 结果里的引用标记：装配期按它找图并展开成 image parts。
# 格式固定（`【图片】path=<工作区内相对路径>`），正则与装配端共用一处定义。
EYE_MARKER_RE = re.compile(r"【图片】path=(\S+)")

# 单张图上限（base64 后 ×1.37 进请求体；超过就不该塞进上下文）
_MAX_IMAGE_BYTES = 12_000_000

# 估算口径：一张图折算多少 prompt token（**只用于水位/成本估算**，不是实测）。
# 取 1500 —— 业界多模态图片的常见量级（OpenAI 低细节 85 tok、高细节按 512px
# 块约 1000–2000；本端点未给出精确公式）。为什么必须有这个常量：装配把图片
# 作为 content parts 追加进消息，若沿用"逐字符估 token"的文本口径，一张
# 1MB 的 PNG 会被算成上百万 tok（base64 文本长度）→ 水位误判触发紧急折叠。
# 宁可给一个量级正确的近似值，也不要让文本口径去算二进制图片。
TOKENS_PER_IMAGE = 1500

# 一次注入上下文的最大图片张数（多了就是拿钱换噪声）
MAX_INJECTED_IMAGES = 4

_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}

_MIME = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp",
}


def shots_dir() -> Path:
    """截图落盘目录（`.shots/`，已 gitignore，serve 的噪声目录清单也认它）。"""
    path = Path(safety.workspace_root()) / ".shots"
    path.mkdir(parents=True, exist_ok=True)
    return path


# ---- 浏览器探测 ---------------------------------------------------------------
# 不引入 playwright（要下 ~300MB 浏览器，与单进程 CLI 形态不符，
# 见 docs/web-tooling-research-20260913.md）；本机已有 chrome/edge 可直接用。

def _browser_candidates() -> list[str]:
    """候选浏览器可执行文件：WOVRA_BROWSER > 常见安装路径 > PATH。"""
    out: list[str] = []
    env = os.environ.get("WOVRA_BROWSER", "").strip()
    if env:
        out.append(env)
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    local = os.environ.get("LOCALAPPDATA", "")
    rel = [
        (pf, r"Google\Chrome\Application\chrome.exe"),
        (pf86, r"Google\Chrome\Application\chrome.exe"),
        (local, r"Google\Chrome\Application\chrome.exe"),
        (pf, r"Microsoft\Edge\Application\msedge.exe"),
        (pf86, r"Microsoft\Edge\Application\msedge.exe"),
    ]
    for base, tail in rel:
        if base:
            out.append(str(Path(base) / tail))
    for name in ("chrome", "chrome.exe", "msedge", "msedge.exe",
                 "chromium", "chromium-browser", "google-chrome"):
        found = shutil.which(name)
        if found:
            out.append(found)
    return out


def find_browser() -> str | None:
    """第一个真实存在的浏览器路径；都没有返回 None（调用方给可行动提示）。"""
    for cand in _browser_candidates():
        try:
            if cand and Path(cand).is_file():
                return cand
        except OSError:
            continue
    return None


# ---- PNG 像素统计（纯 stdlib） -------------------------------------------------
# 作用：截图这一步就给出"画面是否全黑/全白/崩版"的机器判据，不必等模型看图。
# 这也是 AGENTS.md「界面必须实际渲染检查（含像素亮度/配色）」的最小实现。

def _png_samples(raw: bytes, max_samples: int = 3000):
    """解 PNG 并返回 (width, height, 采样像素列表, 隔行采样步长)。

    支持 8-bit 的灰度/RGB/调色板/带 alpha（非隔行）——刚好覆盖浏览器截图。
    遇到不支持的形态（隔行、1/2/4/16-bit）返回 None，调用方降级为"仅报尺寸"。
    """
    if not raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return None
    pos, idat = 8, bytearray()
    width = height = bitdepth = colortype = interlace = 0
    palette = b""
    while pos + 8 <= len(raw):
        (length,) = struct.unpack(">I", raw[pos:pos + 4])
        tag = raw[pos + 4:pos + 8]
        data = raw[pos + 8:pos + 8 + length]
        if tag == b"IHDR":
            width, height, bitdepth, colortype, _comp, _filt, interlace = struct.unpack(
                ">IIBBBBB", data)
        elif tag == b"PLTE":
            palette = data
        elif tag == b"IDAT":
            idat += data
        elif tag == b"IEND":
            break
        pos += 12 + length
    if not width or not height or interlace or bitdepth != 8:
        return None
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(colortype)
    if channels is None:
        return None
    try:
        raw_bytes = zlib.decompress(bytes(idat))
    except zlib.error:
        return None
    stride = width * channels
    prev = bytearray(stride)
    rows: list[bytes] = []
    off = 0
    for _ in range(height):
        if off + 1 + stride > len(raw_bytes):
            break
        ftype = raw_bytes[off]
        line = bytearray(raw_bytes[off + 1:off + 1 + stride])
        off += 1 + stride
        if ftype == 1:
            for i in range(channels, stride):
                line[i] = (line[i] + line[i - channels]) & 0xFF
        elif ftype == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif ftype == 3:
            for i in range(stride):
                left = line[i - channels] if i >= channels else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif ftype == 4:
            for i in range(stride):
                a = line[i - channels] if i >= channels else 0
                b = prev[i]
                c = prev[i - channels] if i >= channels else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pred = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[i] = (line[i] + pred) & 0xFF
        elif ftype != 0:
            return None
        rows.append(bytes(line))
        prev = line
    if not rows:
        return None
    step = max(1, len(rows) // 60)          # 行采样：最多看 60 行
    col_step = max(1, width // 60)          # 列采样：最多看 60 列
    samples = []

    def px(line: bytes, x: int):
        if colortype == 3:
            idx = line[x]
            if idx * 3 + 3 <= len(palette):
                return palette[idx * 3], palette[idx * 3 + 1], palette[idx * 3 + 2]
            return 0, 0, 0
        base = x * channels
        if colortype in (0, 4):
            v = line[base]
            return v, v, v
        if colortype == 2:
            return line[base], line[base + 1], line[base + 2]
        return line[base], line[base + 1], line[base + 2]  # RGBA

    for y in range(0, len(rows), step):
        line = rows[y]
        for x in range(0, width, col_step):
            if (x + 1) * channels <= len(line):
                samples.append(px(line, x))
            if len(samples) >= max_samples:
                break
        if len(samples) >= max_samples:
            break
    return width, height, samples, (step, col_step)


def _describe_pixels(raw: bytes) -> str:
    """把采样像素压成几行可判读的结论（亮度/配色/疑似空白）。"""
    parsed = _png_samples(raw)
    if not parsed:
        return "像素统计：本图形态不支持纯 stdlib 解码（隔行/高位深），请用 view_image 看图"
    width, height, samples, _step = parsed
    n = len(samples)
    if not n:
        return "像素统计：无有效采样"
    avg = [sum(s[i] for s in samples) / n for i in range(3)]
    lums = [0.299 * s[0] + 0.587 * s[1] + 0.114 * s[2] for s in samples]
    mean = sum(lums) / n
    var = sum((x - mean) ** 2 for x in lums) / n
    dark = sum(1 for x in lums if x < 64) / n * 100
    mid = sum(1 for x in lums if 64 <= x <= 192) / n * 100
    light = 100 - dark - mid
    buckets: dict[tuple, int] = {}
    for s in samples:
        key = (s[0] >> 5 << 5, s[1] >> 5 << 5, s[2] >> 5 << 5)
        buckets[key] = buckets.get(key, 0) + 1
    top = sorted(buckets.items(), key=lambda kv: -kv[1])[:3]
    tops = "、".join(
        f"#{r:02x}{g:02x}{b:02x} {c / n * 100:.0f}%" for (r, g, b), c in top
    )
    lines = [
        f"像素统计：{width}×{height}　平均亮度 {mean:.1f}/255"
        f"（暗 {dark:.0f}% / 中 {mid:.0f}% / 亮 {light:.0f}%），"
        f"均值 RGB({avg[0]:.0f},{avg[1]:.0f},{avg[2]:.0f})，主色 {tops}",
    ]
    warn = []
    if mean < 12:
        warn.append("画面几乎全黑——检查渲染失败/背景色丢失")
    elif mean > 243:
        warn.append("画面几乎全白——检查样式是否没加载（裸 HTML）")
    if var ** 0.5 < 3:
        warn.append("亮度标准差极小，疑似纯色/空白页——检查是否渲染了内容")
    if dark > 97 and mid < 2:
        warn.append("95% 以上像素落在暗档——深色主题属正常，纯黑则需确认")
    if warn:
        lines.append("⚠ " + "；".join(warn))
    return "\n".join(lines)


# ---- 截图 ---------------------------------------------------------------------

def _resolve_target(target: str) -> tuple[str, str] | tuple[None, str]:
    """把入参归一化成 (浏览器 URL, 说明)；不合法返回 (None, 错误说明)。

    两种形态：http(s) URL（含本地预览服务）与工作区内的本地文件。
    本地文件必须落在工作区内——截图是**读**操作，但把界外文件塞进
    上下文等于外泄，故与 read_file 同口径（`_safe_write_path`）。
    """
    text = (target or "").strip()
    if not text:
        return None, "target 为空：给 http(s) URL 或工作区内相对文件路径"
    low = text.lower()
    if low.startswith(("http://", "https://")):
        return text, "URL"
    if low.startswith("file://"):
        return None, "不接受 file:// —— 直接给工作区内的相对路径（如 webui/index.html）"
    if re.match(r"^[a-z][a-z0-9+.-]*://", low):
        return None, f"仅支持 http/https 或工作区内相对路径，收到: {text[:60]}"
    # 本地文件：相对路径必须在工作区内
    try:
        path: Path = safety._safe_write_path(text)
    except ValueError as error:
        return None, str(error)
    if path.is_dir():
        return None, f"{text} 是目录：请给具体文件（如 {text}/index.html）"
    if not path.exists():
        return None, (f"文件不存在: {text}。先用 glob_files('*.html') 确认路径拼写"
                      f"（相对工作区根目录，不要加盘符或 ..）")
    return path.as_uri(), "本地文件"


def _run_browser(browser: str, url: str, out: Path, width: int, height: int,
                 wait_ms: int, timeout: int) -> tuple[bool, str]:
    """跑一次无头截图；返回 (是否成功, 诊断文本)。"""
    profile = Path(tempfile.gettempdir()) / "wovra-eye-profile"
    args = [
        browser, "--headless=new", "--disable-gpu", "--hide-scrollbars",
        "--no-first-run", "--no-default-browser-check", "--disable-extensions",
        f"--user-data-dir={profile}",
        f"--window-size={width},{height}",
        "--force-device-scale-factor=1",
        f"--screenshot={out}",
    ]
    if wait_ms > 0:
        # 让页面 JS（图表、动画、异步渲染）跑完再截；虚拟时间预算比 sleep 稳
        args.append(f"--virtual-time-budget={wait_ms}")
    args.append(url)
    for attempt in ("--headless=new", "--headless"):
        args[1] = attempt
        try:
            proc = subprocess.run(args, capture_output=True, text=True,
                                  timeout=timeout, encoding="utf-8",
                                  errors="replace")
        except subprocess.TimeoutExpired:
            return False, f"截图超时（>{timeout}s）：页面可能一直不静止，可减少 wait_ms"
        except OSError as error:
            return False, f"启动浏览器失败: {error}"
        if out.is_file() and out.stat().st_size > 0:
            return True, ""
        err = " ".join((proc.stderr or "").split())[:300]
        if attempt == "--headless":
            return False, f"截图未产出文件（exit={proc.returncode}）: {err}"
    return False, "截图未产出文件"


def screenshot(target: str, width: int = 1280, height: int = 800,
               wait_ms: int = 0, out: str = "") -> str:
    """给本地网页文件或 URL 截图，落盘为 PNG，并给出像素统计。

    target 传工作区内的相对路径（如 webui/index.html）或 http(s) URL
    （含本地预览服务如 http://127.0.0.1:8000/）。窗口尺寸用 width/height
    控制——要看长页面就调大 height。页面有 JS 渲染时传 wait_ms（毫秒）
    等它画完。

    返回：截图路径 + 尺寸 + **像素统计**（平均亮度、明暗分布、主色）。
    统计就是为了让"全黑/全白/崩版"当场可见，不必等你调 view_image。
    看到图本身请接着调 view_image(path)。
    """
    resolved, note = _resolve_target(target)
    if resolved is None:
        return f"截图失败：{note}"
    try:
        width = max(64, min(int(width), 4000))
        height = max(64, min(int(height), 6000))
        wait_ms = max(0, min(int(wait_ms), 30000))
    except (TypeError, ValueError):
        return "截图失败：width/height/wait_ms 需为整数"
    if out:
        try:
            dest: Path = safety._safe_write_path(out)
        except ValueError as error:
            return f"截图失败：{error}"
        if dest.suffix.lower() != ".png":
            dest = dest.with_suffix(".png")
    else:
        stem = re.sub(r"[^0-9A-Za-z_.-]+", "-", target)[-40:].strip("-") or "shot"
        dest = shots_dir() / f"{time.strftime('%Y%m%d-%H%M%S')}-{stem}.png"
    dest.parent.mkdir(parents=True, exist_ok=True)

    browser = find_browser()
    if browser is None:
        return ("截图失败：本机找不到 Chrome/Edge。装一个浏览器，或用 "
                "WOVRA_BROWSER 环境变量指向可执行文件（如 C:\\...\\chrome.exe）")
    ok, diag = _run_browser(browser, resolved, dest, width, height, wait_ms, 90)
    if not ok:
        return f"截图失败：{diag}"
    size = dest.stat().st_size
    try:
        rel = dest.relative_to(Path(safety.workspace_root())).as_posix()
    except ValueError:
        rel = str(dest)
    safety._audit(f"[screenshot] {target} → {rel}（{size:,} 字节）")
    raw = dest.read_bytes()
    stats = _describe_pixels(raw) if size <= _MAX_IMAGE_BYTES else "（图过大，跳过统计）"
    return (f"已截图 {rel}（{size:,} 字节，窗口 {width}×{height}，{note}）\n"
            f"{stats}\n看这张图请调 view_image('{rel}')。")


# ---- 看图 ---------------------------------------------------------------------

def view_image(path: str, note: str = "") -> str:
    """把一张已落盘的图片送进你的视野（多模态）。

    path 传工作区内相对路径（screenshot 的返回值里就有）。调用后，图片
    会在**下一次请求**的上下文尾部出现，你就能真正看到像素——用于
    判断配色是否刺眼、布局是否错位、元素是否重叠这类文字量不出来的事。

    图片本体不进历史存档（只留一行引用），所以放心调用，不会撑爆任务文件。
    note 可写你想从图里确认什么（会一并显示在图片旁）。
    """
    try:
        target: Path = safety._safe_write_path(path)
    except ValueError as error:
        return f"看图失败：{error}"
    if target.is_dir():
        return f"看图失败：{path} 是目录，view_image 只接受图片文件"
    if not target.exists():
        return (f"看图失败：文件不存在: {path}。先用 screenshot 生成，"
                f"或用 glob_files('*.png') 找出实际路径")
    suffix = target.suffix.lower()
    if suffix not in _IMAGE_SUFFIXES:
        return (f"看图失败：{path} 不是支持的图片格式"
                f"（支持 {', '.join(sorted(_IMAGE_SUFFIXES))}）；"
                f"文本文件请用 read_file")
    size = target.stat().st_size
    if size > _MAX_IMAGE_BYTES:
        return (f"看图失败：{path} 有 {size:,} 字节，超过 {_MAX_IMAGE_BYTES:,} 上限"
                f"（再大就不该塞进上下文）。可调小截图尺寸（width/height）重截")
    if size == 0:
        return f"看图失败：{path} 是空文件"
    try:
        rel = target.relative_to(Path(safety.workspace_root())).as_posix()
    except ValueError:
        rel = path
    safety._audit(f"[view_image] {rel}（{size:,} 字节）")
    hint = f"　关注点：{note.strip()[:200]}" if note and note.strip() else ""
    return (f"【图片】path={rel}{hint}\n"
            f"（{size:,} 字节，将在下一次请求中作为图像送给你。）")


def load_image_part(path: str) -> dict | None:
    """把工作区内的图片读成 OpenAI 协议的 image_url part（装配期用）。

    读不到/超限/格式不支持都返回 None——装配绝不能因为一张坏图整体失败。
    """
    try:
        target = Path(safety.workspace_root()) / path
        if not target.is_file():
            return None
        raw = target.read_bytes()
    except OSError:
        return None
    if not raw or len(raw) > _MAX_IMAGE_BYTES:
        return None
    mime = _MIME.get(target.suffix.lower())
    if mime is None:
        return None
    b64 = base64.b64encode(raw).decode()
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}


def eye_parts_from_marker(text: str) -> list[dict]:
    """从一条工具结果文本里抽出图片引用并加载（装配期用，零 LLM）。"""
    parts: list[dict] = []
    for match in EYE_MARKER_RE.finditer(text or ""):
        part = load_image_part(match.group(1))
        if part is not None:
            parts.append(part)
    return parts


def describe_part(part: dict) -> str:
    """把图片 part 压成一行文字（展示/落盘用，不把 base64 灌进日志）。"""
    if not isinstance(part, dict):
        return str(part)[:120]
    if part.get("type") == "text":
        return str(part.get("text") or "")
    url = ((part.get("image_url") or {}).get("url") or "")
    if url.startswith("data:"):
        head = url.split(",", 1)[0]
        approx = int(len(url) * 3 / 4)          # base64 → 字节的近似
        return f"[图片 {head}，约 {approx:,} 字节]"
    return f"[图片 {url[:120]}]"


def part_text(part) -> str:
    """取一个 content part 的纯文本；图片 part 返回空串。

    图片的 token 另有固定口径（`TOKENS_PER_IMAGE`），**绝不能**按文本算：
    它的 base64 文本长度会让一张 1MB 的图被估成上百万 tok。
    """
    if isinstance(part, dict):
        if part.get("type") == "image_url":
            return ""
        return str(part.get("text") or "")
    return str(part or "")


def content_to_text(content) -> str:
    """把一条消息的 content 压成纯文本（图片只留一行说明，不带 base64）。

    content 有两种形态：普通字符串，或装配期注入的 parts 列表（看图的
    那条尾部消息）。展示、落盘、字符统计一律走这里——任何地方直接
    `str(content)` 都会把 base64 原文灌进去。
    """
    if isinstance(content, list):
        out: list[str] = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "image_url":
                out.append(describe_part(part))
                continue
            text = part_text(part)
            if text:
                out.append(text)
        return "\n".join(out)
    return str(content or "")


def _describe_here() -> str:  # pragma: no cover——仅供人工排障
    return f"browser={find_browser()} shots={shots_dir()}"
