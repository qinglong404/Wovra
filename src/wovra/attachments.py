"""用户附件：粘贴/上传的内容落盘 + 轮内注入（不是一次工具调用）。

用户口径（2026-09-17）：前端粘贴的大段内容与文件、图片，都按"读文件级别"
注入——模型直接看到内容，**不必再调一次 read_file**；历史轮只留一行引用。

三条形态约束（与 `tools/eyes.py` 的图片通道同源）：

* **原文不进 task.json**：会话存档已 MB 级，粘贴一份几万行日志塞进去等于
  把存档撑爆。落盘到工作区 `attachments/`，存档里只留一行路径引用。
* **注入走装配期**：`agent/assembly.py::_attachment_message` 扫当前轮的引用
  标记并按需展开——轮内注入、零工具往返。
* **放不进去就说放不进去**：超限/读不到/格式不支持都要在注入的说明里写出
  来，否则模型会以为"用户给我的东西我看过了"。

引用标记形如 `【附件】path=attachments/pasted-20260917-170102.py`（行首），
用户消息里就带这一行；历史轮次保留它，模型需要细节时自己 read_file。
"""

import base64
import re
import time
from pathlib import Path

from .tools import documents, safety

# 附件落盘目录（工作区内，相对路径）。名字带 wovra- 前缀：一是不与用户项目里常见的
# `attachments/` 撞名（一旦撞上，忽略它就会把用户的正常文件挡在版本库外），
# 二是仓库自己可以只忽略这一个名字。不用隐藏名：glob/search 应当能找到用户贴进来的东西。
ATTACH_DIR = "wovra-attachments"

# 引用标记：行首 + 路径字符集不含空白/引号/括号 + 不含换行。
# 与 `eyes.EYE_MARKER_RE` 同样的收紧理由：放宽会把源码/JSON 里的样板文本
# 也当成引用（眼睛那边实测过这类假阳性）。
ATTACH_MARKER_RE = re.compile(
    r"""(?m)^【附件】path=([^\s"'{}()<>|;*?]+)""")

# 单文件与整轮的注入上限（字符）。超限不静默：正文里写明被截断到哪。
MAX_INJECT_CHARS = 200_000
MAX_TOTAL_CHARS = 500_000

# 文本文件嗅探的编码顺序（中文粘贴里 GBK 不罕见）
_ENCODINGS = ("utf-8-sig", "utf-8", "gbk", "big5", "latin-1")

_IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff")

_BAD_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def is_image(rel: str) -> bool:
    return Path(str(rel or "")).suffix.lower() in _IMAGE_SUFFIXES


def safe_name(name: str) -> str:
    """用户给的文件名 → 可安全落盘的名字（去目录、去危险字符、保后缀）。"""
    base = Path(str(name or "")).name or "pasted.txt"
    cleaned = _BAD_CHARS.sub("_", base).strip(" .") or "pasted.txt"
    if len(cleaned) > 120:                      # 后缀优先保住
        suffix = Path(cleaned).suffix
        cleaned = cleaned[: 120 - len(suffix)] + suffix
    return cleaned


def unique_name(name: str, base: Path) -> str:
    """同目录内不覆盖已有文件：重名时补 -2、-3（附件是用户材料，不能悄悄替掉）。"""
    stem, suffix = Path(name).stem, Path(name).suffix
    candidate, n = name, 1
    while (base / candidate).exists():
        n += 1
        candidate = f"{stem}-{n}{suffix}"
    return candidate


def save(data: bytes, name: str) -> str:
    """把附件写进工作区 `attachments/`，返回相对路径（如 attachments/x.py）。"""
    workspace = Path(safety.workspace_root())
    folder = workspace / ATTACH_DIR
    folder.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    final = unique_name(f"{stamp}-{safe_name(name)}", folder)
    (folder / final).write_bytes(data)
    return f"{ATTACH_DIR}/{final}"


def save_text(text: str, name: str) -> str:
    """粘贴文本的落盘口（UTF-8，前端给的名字只作提示）。"""
    return save(text.encode("utf-8"), name)


def marker(rel: str) -> str:
    """给用户消息用的引用行。"""
    return f"【附件】path={rel}"


def save_data_url(data_url: str, name: str) -> str:
    """浏览器 `FileReader` 的 data URL → 落盘（粘贴图片走这条路）。"""
    head, _, payload = str(data_url).partition(",")
    if not payload:
        raise ValueError("data URL 里没有内容")
    if ";base64" in head:
        return save(base64.b64decode(payload), name)
    return save_text(payload, name)


def resolve(rel: str) -> Path | None:
    """引用路径 → 实际文件；不存在或越界返回 None。"""
    text = str(rel or "").strip()
    if not text or ".." in Path(text).parts:
        return None
    candidate = Path(text)
    if not candidate.is_absolute():
        candidate = Path(safety.workspace_root()) / candidate
    try:
        return candidate if candidate.is_file() else None
    except OSError:
        return None


def read_text(rel: str) -> tuple[str, str] | None:
    """附件 → (来源标签, 文本)；读不出返回 None。

    先走 `documents.extract`（docx/xlsx/pptx/pdf/csv 那些非纯文本格式），
    再按编码嗅探纯文本。乱码不返回——那比"读不出"更有害。
    """
    target = resolve(rel)
    if target is None:
        return None
    parsed = documents.extract(target)
    if parsed is not None:
        return parsed
    try:
        raw = target.read_bytes()
    except OSError:
        return None
    if b"\x00" in raw[:4096]:                   # 有 NUL 的多半是二进制
        return None
    for encoding in _ENCODINGS:
        try:
            return "", raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return None


def _header(rel: str, label: str, text: str) -> str:
    lines = text.count("\n") + 1
    kind = f"{label}，" if label else ""
    return f"──── {rel}（{kind}{lines:,} 行、{len(text):,} 字符）────"


def load_parts(text: str) -> tuple[list[dict], list[str]]:
    """装配期：扫引用 → (content parts, 未注入说明)。

    文本附件拼成一个 text part（调用方还会再补一段说明文字），图片各自一个
    image_url part。同一路径在一轮里重复引用只算一次。
    """
    from .tools import eyes as eyes_module

    blocks: list[str] = []
    images: list[dict] = []
    skipped: list[str] = []
    seen: set[str] = set()
    total = 0
    for match in ATTACH_MARKER_RE.finditer(text or ""):
        rel = match.group(1)
        if rel in seen:
            continue
        seen.add(rel)
        if is_image(rel):
            part = eyes_module.load_image_part(rel)
            if part is not None and len(images) < eyes_module.MAX_INJECTED_IMAGES:
                images.append(part)
            elif part is None:
                skipped.append(f"{rel}（{eyes_module.image_reject_reason(rel) or '原因未知'}）")
            continue
        loaded = read_text(rel)
        if loaded is None:
            skipped.append(f"{rel}（读不出：非文本/编码不支持/文件已不在工作区）")
            continue
        label, body = loaded
        if total >= MAX_TOTAL_CHARS:
            skipped.append(f"{rel}（本轮注入总量已达上限 {MAX_TOTAL_CHARS:,} 字符）")
            continue
        room = min(MAX_INJECT_CHARS, MAX_TOTAL_CHARS - total)
        if len(body) > room:
            body = body[:room] + f"\n…（附件超长，已截断到 {room:,} 字符；完整内容用 read_file 读该路径）"
        total += len(body)
        blocks.append(f"{_header(rel, label, body)}\n{body}")
    parts: list[dict] = []
    if blocks:
        parts.append({"type": "text", "text": "\n\n".join(blocks)})
    return parts + images, skipped
