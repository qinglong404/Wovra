"""附件原生解析测试（2026-09-16，GAIA FINDINGS §3）。

夹具全部**现场合成**（zipfile/zlib 拼出真的 docx/xlsx/pptx/pdf），不用任何
第三方库也不需要二进制样本——解析器要解的正是这种 ZIP+XML 与 PDF 流结构。
"""

import zlib
import zipfile

from wovra.tools import documents as documents_module


def _zip(path, members: dict[str, str | bytes]):
    with zipfile.ZipFile(path, "w") as archive:
        for name, body in members.items():
            archive.writestr(name, body)
    return path


def _read(tmp_path, path, **kwargs):
    """经 read_file 走一遍（这才是模型实际用的入口）。"""
    from wovra import tools as tools_module

    relative = path.relative_to(tmp_path)
    return tools_module.files.read_file(str(relative), **kwargs)


def _root(tmp_path, monkeypatch):
    from wovra import tools as tools_module

    monkeypatch.setattr(tools_module.safety, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(tools_module.safety, "_audit", lambda detail: None)


def test_read_file_parses_docx(tmp_path, monkeypatch):
    """docx：word/document.xml 里的段落逐段取出（表格单元格里的段落也成段）。"""
    _root(tmp_path, monkeypatch)
    body = (
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body>"
        '<w:p><w:r><w:t>季度报告</w:t></w:r></w:p>'
        '<w:p><w:r><w:t>营收 </w:t></w:r><w:r><w:t>120 万</w:t></w:r></w:p>'
        "<w:tbl><w:tr><w:tc><w:p><w:r><w:t>表格里的字</w:t></w:r></w:p></w:tc></w:tr></w:tbl>"
        "</w:body></w:document>")
    path = _zip(tmp_path / "report.docx", {"word/document.xml": body})
    out = _read(tmp_path, path)
    assert "Word 文档" in out
    assert "季度报告" in out and "营收 120 万" in out and "表格里的字" in out


def test_read_file_parses_xlsx_with_shared_strings(tmp_path, monkeypatch):
    """xlsx：sharedStrings 索引 + 数字单元格，按工作表分块输出。"""
    _root(tmp_path, monkeypatch)
    shared = ('<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
              "<si><t>名称</t></si><si><t>数量</t></si><si><t>苹果</t></si></sst>")
    workbook = ('<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                '<sheets><sheet name="库存" sheetId="1"/></sheets></workbook>')
    sheet = ('<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
             '<sheetData>'
             '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c></row>'
             '<row r="2"><c r="A2" t="s"><v>2</v></c><c r="B2"><v>7</v></c></row>'
             "</sheetData></worksheet>")
    path = _zip(tmp_path / "book.xlsx", {
        "xl/sharedStrings.xml": shared,
        "xl/workbook.xml": workbook,
        "xl/worksheets/sheet1.xml": sheet,
    })
    out = _read(tmp_path, path)
    assert "Excel 工作簿" in out and "工作表：库存" in out
    assert "名称\t数量" in out and "苹果\t7" in out


def test_read_file_parses_pptx_by_slide_order(tmp_path, monkeypatch):
    """pptx：按 slide 数字顺序分页（slide2 不能排在 slide10 之后）。"""
    _root(tmp_path, monkeypatch)

    def slide(text):
        return ('<p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
                'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">'
                f'<a:p><a:r><a:t>{text}</a:t></a:r></a:p></p:sld>')

    path = _zip(tmp_path / "deck.pptx", {
        "ppt/slides/slide10.xml": slide("第十页"),
        "ppt/slides/slide2.xml": slide("第二页"),
    })
    out = _read(tmp_path, path)
    assert "PowerPoint 演示文稿" in out
    assert out.index("第 1 页") < out.index("第 2 页")
    assert "第二页" in out and "第十页" in out


def test_read_file_parses_pdf_text_layer(tmp_path, monkeypatch):
    """PDF：只解 FlateDecode 文本流；扫描件/自定义编码解出噪声时**拒答**。"""
    _root(tmp_path, monkeypatch)
    content = (b"BT /F1 12 Tf (Hello from the PDF text layer) Tj "
               b"(second line of text) Tj ET")
    stream = zlib.compress(content)
    pdf = (b"%PDF-1.4\n1 0 obj\n<< /Length " + str(len(stream)).encode()
           + b" /Filter /FlateDecode >>\nstream\n" + stream + b"\nendstream\nendobj\n%%EOF\n")
    path = tmp_path / "doc.pdf"
    path.write_bytes(pdf)
    out = _read(tmp_path, path)
    assert "PDF（文本层）" in out
    assert "Hello from the PDF text layer" in out and "second line of text" in out

    # 解出来是噪声（可打印字符占比过低）→ 当解不出，给下一步提示而不是乱码
    garbage = zlib.compress(b"BT " + bytes(range(1, 40)) * 40 + b" Tj ET")
    bad = tmp_path / "scan.pdf"
    bad.write_bytes(b"%PDF-1.4\n1 0 obj\n<< /Filter /FlateDecode >>\nstream\n"
                    + garbage + b"\nendstream\nendobj\n")
    out = _read(tmp_path, bad)
    assert "无法按文本读取" in out and "pdftotext" in out
    assert "PDF（文本层）" not in out


def test_read_file_csv_keeps_line_tools(tmp_path, monkeypatch):
    """UTF-8 的 csv 走原有文本路径（不该被改道）；start_line/pattern 照旧可用。"""
    _root(tmp_path, monkeypatch)
    path = tmp_path / "data.csv"
    path.write_text("name,score\n甲,1\n乙,2\n丙,3\n", encoding="utf-8")
    out = _read(tmp_path, path)
    assert "乙,2" in out and "CSV" not in out
    only = _read(tmp_path, path, pattern="乙")
    assert "乙,2" in only and "甲,1" not in only
    first_two = _read(tmp_path, path, num_lines=2)
    assert "以下为第 1-2 行" in first_two and "丙,3" not in first_two


def test_read_file_gbk_csv_decodes_and_names_the_encoding(tmp_path, monkeypatch):
    """中文附件常见 GBK：不允许变成乱码，也不允许说"读不出"；标签里写清编码。"""
    _root(tmp_path, monkeypatch)
    path = tmp_path / "gbk.csv"
    path.write_bytes("名称,数量\n螺丝,30\n".encode("gbk"))
    out = _read(tmp_path, path)
    assert "螺丝" in out and "CSV 表格，gbk 编码，2 列" in out


def test_read_file_unsupported_binary_gets_actionable_hint(tmp_path, monkeypatch):
    """纯二进制：仍算读失败（lifecycle 靠这个文案），但要说清下一步怎么办。"""
    _root(tmp_path, monkeypatch)
    image = tmp_path / "shot.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n" + bytes(range(1, 200)))
    out = _read(tmp_path, image)
    assert "无法按文本读取" in out and "view_image" in out

    # 非 UTF-8 的字节（0xFF 在这个位置上不合法）才走不到文本路径
    blob = tmp_path / "raw.bin"
    blob.write_bytes(bytes([0xFF, 0xFE, 0x00] + list(range(1, 60))))
    assert "二进制或未知格式" in _read(tmp_path, blob)


def test_extract_returns_none_for_unparsable_office_file(tmp_path):
    """坏 zip / 缺成员的"docx"要当解不出，不许抛异常穿透到工具层。"""
    broken = tmp_path / "broken.docx"
    broken.write_bytes(b"not a zip at all")
    assert documents_module.extract(broken) is None

    empty = _zip(tmp_path / "empty.docx", {"docProps/app.xml": "<a/>"})
    assert documents_module.extract(empty) is None
    assert documents_module.extract(tmp_path / "missing.pdf") is None
