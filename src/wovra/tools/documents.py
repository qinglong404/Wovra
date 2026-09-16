"""附件原生解析（2026-09-16）：把常见文档格式解成文本，零新依赖。

动机（GAIA 实测，output/gaia/FINDINGS.md §3）：Wovra 原先只有 read_file
（UTF-8 文本），带附件的题 agent 只能自己 run_command 写脚本调库——实测一道
音频题的工作区里堆出 `.venv-asr`（392M）与 `.hf-cache`（1.9G，HuggingFace
模型缓存），4.9G 产物里 4.3G 是两题的自造轮子。这里把**零依赖能解出来的**
那些格式收进工具层，agent 不必再装一遍世界。

零依赖的实现基础：Office 三件套本质是 ZIP + XML（stdlib `zipfile` +
`xml.etree`），PDF 的文本层是 zlib 流里的字符串（stdlib `zlib` + 正则）。

两条纪律：
* **解析不出就返回 None**（调用方回退到原有提示），绝不返回乱码——乱码比
  "读不出"更有害，模型会基于垃圾字符编出看似有据的答案。
* 输出有硬上限（行数/页数/字符），超了在正文里显式写明截断，不静默丢。
"""

import csv
import io
import re
import xml.etree.ElementTree as ET
import zipfile
import zlib
from pathlib import Path

# 单次解析的规模上限（防一份巨型 xlsx 把内存和上下文一起打爆）
_MAX_ROWS_PER_SHEET = 5_000
_MAX_SHEETS = 20
_MAX_SLIDES = 200
_MAX_CHARS = 2_000_000

_IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff")
_MEDIA_SUFFIXES = (".mp3", ".wav", ".m4a", ".flac", ".ogg", ".mp4", ".mov", ".mkv", ".avi")


def extract(path: Path) -> tuple[str, str] | None:
    """文档 → (来源标签, 文本)；解不出返回 None。"""
    suffix = path.suffix.lower()
    try:
        if suffix in (".csv", ".tsv"):
            return _from_csv(path, "\t" if suffix == ".tsv" else ",")
        if suffix == ".docx":
            return _from_zip_xml(path, "word/document.xml", _docx_text, "Word 文档")
        if suffix == ".xlsx":
            return _from_xlsx(path)
        if suffix == ".pptx":
            return _from_pptx(path)
        if suffix == ".pdf":
            return _from_pdf(path)
        if suffix == ".zip":
            return _from_zip_listing(path)
    except (OSError, zipfile.BadZipFile, ET.ParseError, zlib.error, ValueError):
        return None
    return None


def binary_hint(path: Path) -> str:
    """读不出时给下一步（提示比"二进制文件"四个字有用）。"""
    suffix = path.suffix.lower()
    if suffix in _IMAGE_SUFFIXES:
        return "这是图片：用 view_image(path) 看图，不要用 read_file。"
    if suffix in _MEDIA_SUFFIXES:
        return ("这是音视频：Wovra 没有转写工具。若系统里已有 ffmpeg/whisper 之类，"
                "可用 run_command 调它们；否则请用户提供文字稿或截图。")
    if suffix in (".docx", ".xlsx", ".pptx", ".pdf"):
        return ("该文档解析未成功（可能是扫描件、加密或非标准编码）。"
                "可用 run_command 调系统工具（pdftotext 等）兜底，"
                "或请用户提供文本/截图。")
    return "二进制或未知格式。"


# ---- 通用工具 ---------------------------------------------------------------

def _decode(raw: bytes) -> str:
    """按常见编码依次尝试（中文附件里 GBK 很常见）。"""
    return _decode_with_name(raw)[0]


def _decode_with_name(raw: bytes) -> tuple[str, str]:
    """同 `_decode`，外加实际用上的编码名（要写进给模型的标签里）。"""
    for encoding in ("utf-8-sig", "utf-8", "gbk", "big5", "latin-1"):
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace"), "utf-8（有损）"


def _local(tag: str) -> str:
    """XML 标签的本地名（去掉 `{namespace}` 前缀）。"""
    return tag.rsplit("}", 1)[-1]


def _clip(text: str, note: str = "") -> str:
    if len(text) <= _MAX_CHARS:
        return text
    return text[:_MAX_CHARS] + f"\n…（{note or '内容超长'}，已截断到 {_MAX_CHARS} 字符）"


def _sort_key(name: str) -> tuple:
    """slide2.xml 排在 slide10.xml 前面（按数字，不按字典序）。"""
    return tuple(int(part) if part.isdigit() else part for part in re.split(r"(\d+)", name))


def _from_csv(path: Path, delimiter: str) -> tuple[str, str]:
    """utf-8 的 csv 本来就走文本路径；这里管的是**非 UTF-8** 的中文表格。

    标签里写出实际用的编码：附件是 GBK 时模型该知道这件事（后面自己按字节
    算长度、做字符串比较时口径会不一样）。
    """
    text, encoding = _decode_with_name(path.read_bytes())
    rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    width = max((len(row) for row in rows), default=0)
    # 行数交给 read_file 的表头去报（它按行切），这里只补它没有的两件事：
    # 分隔符类型、实际编码、列数
    kind = "TSV" if delimiter == "\t" else "CSV"
    return f"{kind} 表格，{encoding} 编码，{width} 列", _clip(text, "表格行数过多")


# ---- Office（ZIP + XML）-----------------------------------------------------

def _from_zip_xml(path: Path, member: str, convert, label: str) -> tuple[str, str] | None:
    with zipfile.ZipFile(path) as archive:
        if member not in archive.namelist():
            return None
        root = ET.fromstring(archive.read(member))
    text = convert(root)
    return (label, _clip(text)) if text.strip() else None


def _docx_text(root: ET.Element) -> str:
    """docx：每个 w:p 一段（表格单元格里的段落也会各自成段）。"""
    lines = []
    for element in root.iter():
        if _local(element.tag) != "p":
            continue
        line = "".join(node.text or "" for node in element.iter()
                       if _local(node.tag) == "t")
        if line.strip():
            lines.append(line)
    return "\n".join(lines)


def _from_xlsx(path: Path) -> tuple[str, str] | None:
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        shared: list[str] = []
        if "xl/sharedStrings.xml" in names:
            root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            shared = ["".join(node.text or "" for node in item.iter()
                              if _local(node.tag) == "t")
                      for item in root.iter() if _local(item.tag) == "si"]
        titles = _sheet_titles(archive, names)
        sheets = sorted((n for n in names if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n)),
                        key=_sort_key)
        if not sheets:
            return None
        blocks = []
        for index, name in enumerate(sheets[:_MAX_SHEETS]):
            rows = _sheet_rows(ET.fromstring(archive.read(name)), shared)
            if not rows:
                continue
            title = titles[index] if index < len(titles) else name
            body = "\n".join("\t".join(r) for r in rows)
            if len(rows) >= _MAX_ROWS_PER_SHEET:
                body += f"\n…（本表只取前 {_MAX_ROWS_PER_SHEET} 行）"
            blocks.append(f"### 工作表：{title}\n{body}")
        if not blocks:
            return None
        if len(sheets) > _MAX_SHEETS:
            blocks.append(f"…（共 {len(sheets)} 个工作表，只解析了前 {_MAX_SHEETS} 个）")
    return "Excel 工作簿", _clip("\n\n".join(blocks), "表格内容超长")


def _sheet_titles(archive: zipfile.ZipFile, names: list[str]) -> list[str]:
    if "xl/workbook.xml" not in names:
        return []
    root = ET.fromstring(archive.read("xl/workbook.xml"))
    return [element.get("name") or "" for element in root.iter()
            if _local(element.tag) == "sheet"]


def _sheet_rows(root: ET.Element, shared: list[str]) -> list[list[str]]:
    rows: list[list[str]] = []
    for row in root.iter():
        if _local(row.tag) != "row":
            continue
        cells: list[str] = []
        for cell in row.iter():
            if _local(cell.tag) != "c":
                continue
            kind = cell.get("t") or ""
            if kind == "inlineStr":
                value = "".join(node.text or "" for node in cell.iter()
                                if _local(node.tag) == "t")
            else:
                raw = next((node.text or "" for node in cell.iter()
                            if _local(node.tag) == "v"), "")
                if kind == "s":
                    try:
                        value = shared[int(raw)]
                    except (ValueError, IndexError):
                        value = raw
                else:
                    value = raw
            cells.append(value.replace("\t", " ").replace("\n", " "))
        if any(cell.strip() for cell in cells):
            rows.append(cells)
        if len(rows) >= _MAX_ROWS_PER_SHEET:
            break
    return rows


def _from_pptx(path: Path) -> tuple[str, str] | None:
    with zipfile.ZipFile(path) as archive:
        slides = sorted((n for n in archive.namelist()
                         if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)), key=_sort_key)
        if not slides:
            return None
        blocks = []
        for index, name in enumerate(slides[:_MAX_SLIDES], start=1):
            root = ET.fromstring(archive.read(name))
            lines = []
            for element in root.iter():
                if _local(element.tag) != "p":
                    continue
                line = "".join(node.text or "" for node in element.iter()
                               if _local(node.tag) == "t")
                if line.strip():
                    lines.append(line)
            if lines:
                blocks.append(f"### 第 {index} 页\n" + "\n".join(lines))
        if not blocks:
            return None
        if len(slides) > _MAX_SLIDES:
            blocks.append(f"…（共 {len(slides)} 页，只解析了前 {_MAX_SLIDES} 页）")
    return "PowerPoint 演示文稿", _clip("\n\n".join(blocks), "幻灯片内容超长")


# ---- PDF（文本层）----------------------------------------------------------

_PDF_STRING_RE = re.compile(rb"\((?:\\.|[^\\()])*\)", re.S)
_PDF_STREAM_RE = re.compile(rb"stream\r?\n(.*?)\r?\nendstream", re.S)


def _from_pdf(path: Path) -> tuple[str, str] | None:
    """抽 PDF 的文本层。

    不做完整 PDF 解析（那要 xref/对象流/字体的全套），只做两件事：把
    FlateDecode 流解开，再把 `(…)` 字符串按 PDF 转义还原。对**文本型**
    PDF（LaTeX/Word 导出、报告、论文）足够；扫描件或用了自定义 CID 编码
    的字体会解成噪声——所以下面有一道质量闸，不合格就当解不出。
    """
    raw = path.read_bytes()
    chunks: list[str] = []
    for match in _PDF_STREAM_RE.finditer(raw):
        data = match.group(1)
        try:
            data = zlib.decompress(data)
        except zlib.error:
            continue                      # 未压缩或非 Flate：直接跳过（宁缺毋滥）
        if b"Tj" not in data and b"TJ" not in data:
            continue
        pieces = []
        for literal in _PDF_STRING_RE.finditer(data):
            piece = _pdf_unescape(literal.group(0)[1:-1])
            if piece.strip():
                pieces.append(piece)
        if pieces:
            chunks.append(" ".join(pieces))
    text = "\n".join(chunks)
    if not _looks_like_text(text):
        return None
    return "PDF（文本层）", _clip(text, "PDF 内容超长")


def _pdf_unescape(literal: bytes) -> str:
    out = bytearray()
    index = 0
    while index < len(literal):
        char = literal[index]
        if char == 0x5C and index + 1 < len(literal):          # 反斜杠转义
            nxt = literal[index + 1]
            simple = {0x6E: 10, 0x72: 13, 0x74: 9, 0x62: 8, 0x66: 12,
                      0x28: 40, 0x29: 41, 0x5C: 92}
            if nxt in simple:
                out.append(simple[nxt])
                index += 2
                continue
            octal = re.match(rb"[0-7]{1,3}", literal[index + 1:index + 4])
            if octal:
                out.append(int(octal.group(0), 8) & 0xFF)
                index += 1 + len(octal.group(0))
                continue
            index += 2
            continue
        out.append(char)
        index += 1
    return out.decode("latin-1")


def _looks_like_text(text: str) -> bool:
    """质量闸：可打印字符占比 + 至少有一点字母数字，否则当解不出来。

    自定义 CID 编码的 PDF 会解出成片的控制字符/私用区字符——那种"文本"
    交给模型比说"读不出"更坏（模型会照着乱码编答案）。
    """
    if len(text) < 20:
        return False
    printable = sum(1 for char in text if char.isprintable() or char in "\n\t")
    if printable / len(text) < 0.85:
        return False
    alnum = sum(1 for char in text if char.isalnum() or "\u4e00" <= char <= "\u9fff")
    return alnum >= 20


# ---- ZIP 清单（不是文档时至少能看见里面有什么）------------------------------

def _from_zip_listing(path: Path) -> tuple[str, str] | None:
    with zipfile.ZipFile(path) as archive:
        entries = [info for info in archive.infolist() if not info.is_dir()]
        if not entries:
            return None
        lines = [f"{info.filename}\t{info.file_size} 字节" for info in entries[:1000]]
        if len(entries) > 1000:
            lines.append(f"…（共 {len(entries)} 个条目，只列出前 1000 个）")
    return f"ZIP 压缩包：{len(entries)} 个条目", _clip("\n".join(lines))
