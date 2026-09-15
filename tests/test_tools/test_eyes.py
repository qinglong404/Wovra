"""工具层测试：眼睛（screenshot / view_image）与多模态图片注入口径。

本模块锁住三件在 2026-09-13 实测确定的事实（「眼睛」的成立基础）：

1. **端点通道**：user 消息的 parts 数组能带图（模型答对颜色）；tool 角色的
   数组 content 被端点 400 拒（`param: messages.2.content`）——故图片走
   「工具结果留引用 + 装配期以 user 注入」。探针 `scripts/probe_vision_channel.py`。
2. **图片不进存档**：工具结果只留一行 `【图片】path=...`，task.json 不被
   base64 撑爆（真实会话已 16MB 级）。
3. **估算口径**：图片按固定 token 折算，绝不按 base64 文本长度算——否则
   一张 1MB 的图会被估成上百万 tok，把水位与紧急折叠一起带偏。

不联网、不开浏览器的用例为主（快且稳）；真截图用例在找不到浏览器时跳过。
"""

import base64
import struct
import zlib

import pytest

from wovra.tools import eyes
from wovra.tools import screenshot, view_image

from ._helpers import *  # noqa: F401,F403


def _png_solid(w: int, h: int, rgb: tuple) -> bytes:
    """纯 stdlib 造一张纯色 PNG（与探针同一手法，不引依赖）。"""
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def _png_noise(w: int, h: int, seed: int = 7) -> bytes:
    """造一张**压不动**的噪声 PNG（base64 长度可观，用于口径断言）。"""
    state = seed
    rows = []
    for _ in range(h):
        line = bytearray()
        for _ in range(w):
            state = (state * 1103515245 + 12345) & 0x7FFFFFFF
            line += bytes(((state >> 16) & 0xFF, (state >> 8) & 0xFF, state & 0xFF))
        rows.append(b"\x00" + bytes(line))
    raw = b"".join(rows)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw, 0)) + chunk(b"IEND", b""))


@pytest.fixture
def eye_root(tmp_path, monkeypatch):
    """把 PROJECT_ROOT 指到临时目录（截图/看图都在界内操作）。"""
    from wovra import tools as tools_module

    root = tmp_path / "ws"
    root.mkdir()
    monkeypatch.setattr(tools_module.safety, "PROJECT_ROOT", root)
    monkeypatch.setattr(tools_module.safety, "_audit", lambda detail: None)
    monkeypatch.setattr(tools_module.eyes.safety, "PROJECT_ROOT", root)
    return root


# ---- 看图：入参校验与引用标记 -------------------------------------------------

def test_view_image_missing_file_is_actionable(eye_root):
    out = view_image("nope.png")
    assert "文件不存在" in out and "screenshot" in out


def test_view_image_rejects_non_image(eye_root):
    (eye_root / "a.txt").write_text("x", encoding="utf-8")
    out = view_image("a.txt")
    assert "不是支持的图片格式" in out and "read_file" in out


def test_view_image_rejects_directory(eye_root):
    (eye_root / "d").mkdir()
    out = view_image("d")
    assert "是目录" in out


def test_view_image_rejects_outside_workspace(eye_root):
    out = view_image("../outside.png")
    assert "越界" in out


def test_view_image_emits_marker_and_not_base64(eye_root):
    """结果只留一行引用——base64 绝不进工具结果（task.json 的命）。"""
    raw = _png_solid(8, 8, (10, 20, 30))
    (eye_root / "pic.png").write_bytes(raw)
    out = view_image("pic.png", note="看配色")
    assert "【图片】path=pic.png" in out
    assert "看配色" in out
    assert base64.b64encode(raw).decode()[:40] not in out
    assert "下一次" in out
    # §3.2 的 P0 修复：必须**显著**声明"尚未进入视野"，否则模型会把
    # "已调用"当成"已看到"（复盘里真发生了多轮无依据确认）
    assert "尚未" in out and "进入你的视野" in out
    assert "不要声称已查看" in out


def test_view_image_too_large_is_refused(eye_root, monkeypatch):
    monkeypatch.setattr(eyes, "_MAX_IMAGE_BYTES", 16)
    (eye_root / "big.png").write_bytes(_png_solid(8, 8, (1, 2, 3)))
    out = view_image("big.png")
    assert "超过" in out and "调小截图尺寸" in out


# ---- 图片加载（装配期用） -----------------------------------------------------

def test_load_image_part_builds_data_uri(eye_root):
    (eye_root / "pic.png").write_bytes(_png_solid(4, 4, (200, 0, 0)))
    part = eyes.load_image_part("pic.png")
    assert part["type"] == "image_url"
    assert part["image_url"]["url"].startswith("data:image/png;base64,")


def test_load_image_part_returns_none_on_junk(eye_root):
    assert eyes.load_image_part("missing.png") is None
    (eye_root / "e.png").write_bytes(b"")
    assert eyes.load_image_part("e.png") is None
    (eye_root / "x.txt").write_text("t", encoding="utf-8")
    assert eyes.load_image_part("x.txt") is None      # 后缀不是图片


def test_eye_marker_regex_ignores_marker_shaped_source_text(eye_root):
    """标记正则不得把**源码/JSON 里形如标记的文本**当成图片引用。

    2026-09-15 实测的噪声：读过 `eyes.py`（里面有 `f"【图片】path={rel}{hint}"`）
    与本测试文件后，轮的提示里堆出一片"未注入"假报告——那些"路径"是
    `{rel}{hint}\\n"` 这种代码片段，拿去查文件必然"不存在"。
    """
    code = ('    return (f"【图片】path={rel}{hint}\\n"\n'
            '            f"（{size:,} 字节，延迟投递。）")')
    blob = 'text = "【图片】path=a.png\\n【图片】path=b.png"'
    assert eyes.EYE_MARKER_RE.findall(code) == []
    assert eyes.EYE_MARKER_RE.findall(blob) == []
    assert eyes.eye_parts_from_marker(code) == []
    # 真标记（行首 + 图片后缀）照旧能抽出来
    (eye_root / "real.png").write_bytes(_png_solid(4, 4, (9, 9, 9)))
    assert eyes.EYE_MARKER_RE.findall("【图片】path=real.png　关注点：配色") == ["real.png"]
    assert len(eyes.eye_parts_from_marker("【图片】path=real.png\n")) == 1


def test_eye_parts_from_marker_finds_multiple(eye_root):
    (eye_root / "a.png").write_bytes(_png_solid(4, 4, (1, 1, 1)))
    (eye_root / "b.png").write_bytes(_png_solid(4, 4, (2, 2, 2)))
    text = "【图片】path=a.png\n【图片】path=b.png\n【图片】path=gone.png"
    parts = eyes.eye_parts_from_marker(text)
    assert len(parts) == 2                              # 坏引用静默跳过


# ---- 口径：图片不按 base64 文本算 token ---------------------------------------

def test_image_tokens_use_fixed_caliber(eye_root):
    """一张大图不得被算成上百万 tok（否则水位误判触发紧急折叠）。"""
    from wovra.agent import Agent
    from wovra.tokens import breakdown

    (eye_root / "big.png").write_bytes(_png_noise(160, 160))
    part = eyes.load_image_part("big.png")
    data_len = len(part["image_url"]["url"])
    assert data_len > 50_000                            # 确实有可观的 base64

    msgs = [{"role": "user", "content": [{"type": "text", "text": "看图"},
                                         part]}]
    est = Agent._estimate_messages(msgs)
    assert est < eyes.TOKENS_PER_IMAGE * 4              # 只按固定口径 + 少量文字
    assert est >= eyes.TOKENS_PER_IMAGE                 # 但确实算了一张图

    table = breakdown("s", "c", [{"type": "function", "function": {
        "name": "x", "parameters": {}}}], msgs)
    assert table["user"] < eyes.TOKENS_PER_IMAGE * 4    # 成本账同口径


def test_content_to_text_hides_base64(eye_root):
    (eye_root / "p.png").write_bytes(_png_solid(8, 8, (3, 4, 5)))
    part = eyes.load_image_part("p.png")
    text = eyes.content_to_text([{"type": "text", "text": "说明"}, part])
    assert "说明" in text and "[图片" in text
    assert "base64," not in text                        # 一个字符都不泄漏


# ---- 截图：CSS 与入参（不开浏览器的部分） -------------------------------------

def test_screenshot_rejects_bad_target(eye_root):
    assert "不接受 file://" in screenshot("file:///C:/x.html")
    assert "仅支持 http/https" in screenshot("ftp://x/y")
    assert "target 为空" in screenshot("   ")
    assert "目录" in screenshot(".")


def test_screenshot_reports_missing_browser(eye_root, monkeypatch):
    monkeypatch.setattr(eyes, "find_browser", lambda: None)
    (eye_root / "a.html").write_text("<html></html>", encoding="utf-8")
    out = screenshot("a.html")
    assert "找不到 Chrome/Edge" in out and "WOVRA_BROWSER" in out


def test_screenshot_surfaces_browser_failure(eye_root, monkeypatch):
    monkeypatch.setattr(eyes, "find_browser", lambda: "C:/fake/chrome.exe")
    monkeypatch.setattr(eyes, "_run_browser",
                        lambda *a, **k: (False, "页面一直不静止"))
    (eye_root / "a.html").write_text("<html></html>", encoding="utf-8")
    assert "截图失败" in screenshot("a.html") and "不静止" in screenshot("a.html")


def _fake_shot(size=(100, 100), actual=None):
    """造一个假的 _run_browser：按请求的 W×H 写一张 PNG（可指定实际尺寸）。"""
    def _run(browser, url, out, width, height, wait_ms, timeout):
        w, h = actual or (width, height)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(_png_solid(w, h, (200, 30, 30)))
        return True, ""
    return _run


def test_screenshot_reports_clamped_request(eye_root, monkeypatch):
    """§3.1：请求尺寸被夹时必须明说「请求 vs 实际」，不能静默照单全收。

    复盘里连续两次 1440×9000/14000 都被砍到 6000 而调用方毫无察觉——
    这正是缺陷所在。现在：① 上限不再是那个凭空的 6000，而是服务端真能
    接受的 8192px/边（`IMAGE_HARD_MAX_SIDE`）；② 超限一律明写出来。
    """
    monkeypatch.setattr(eyes, "find_browser", lambda: "fake-chrome")
    monkeypatch.setattr(eyes, "_run_browser", _fake_shot())
    (eye_root / "a.html").write_text("<html></html>", encoding="utf-8")
    out = screenshot("a.html", width=1440, height=9000)
    assert "超出上限" in out and "1440×9000" in out
    assert "8192" in out                             # 明写上限，不再"静默砍"

    out = screenshot("a.html", width=1440, height=6000)
    assert "超出上限" not in out                      # 6000 以内正常出图
    assert "已截图" in out


def test_screenshot_ceiling_matches_server_hard_limit():
    """上限必须 ≤ 服务端硬上限：超了就是"截得出、看不见"的废图。

    实施中的真实教训（2026-09-15）：先按"浏览器能出图"把上限抬到 20000，
    本机 chrome 确实出得来——但服务端每边只收到 8192，超过的图既发不出去，
    还会把整轮之后**每一次**请求都 400（`load_image_part` 会拒注，可那张图
    对模型就等于不存在）。故上限卡在硬上限上。
    """
    assert max(eyes._MAX_SHOT_WIDTH, eyes._MAX_SHOT_HEIGHT) <= eyes.IMAGE_HARD_MAX_SIDE
    assert eyes._MAX_SHOT_HEIGHT > 6000            # 但确实比原来那个 6000 宽


def test_screenshot_reports_actual_vs_requested(eye_root, monkeypatch):
    """浏览器实际出图与请求不一致时也要报出来（尺寸以实际为准）。"""
    monkeypatch.setattr(eyes, "find_browser", lambda: "fake-chrome")
    monkeypatch.setattr(eyes, "_run_browser", _fake_shot(actual=(800, 400)))
    (eye_root / "a.html").write_text("<html></html>", encoding="utf-8")
    out = screenshot("a.html", width=1200, height=900)
    assert "实际出图" in out and "800×400" in out and "1200×900" in out


def test_pixel_stats_detect_blank_and_dark():
    """像素统计要能把「全黑/全白/纯色」判出来——这就是机器的那只眼。"""
    white = eyes._describe_pixels(_png_solid(32, 32, (255, 255, 255)))
    assert "几乎全白" in white
    black = eyes._describe_pixels(_png_solid(32, 32, (0, 0, 0)))
    assert "几乎全黑" in black
    red = eyes._describe_pixels(_png_solid(32, 32, (220, 40, 40)))
    assert "主色" in red and "#c02020" in red   # 主色按 32 级量化（220>>5<<5=192）
    assert "几乎全黑" not in red and "几乎全白" not in red


def test_pixel_stats_handles_non_png():
    assert "不支持" in eyes._describe_pixels(b"not a png")


# ---- 真截图（有浏览器才跑） ---------------------------------------------------

def test_screenshot_real_browser_when_available(eye_root):
    """真跑一次无头截图：产出 PNG + 尺寸 + 像素统计（红底应被判为红主色）。"""
    if eyes.find_browser() is None:
        pytest.skip("本机无 Chrome/Edge，跳过真截图用例")
    (eye_root / "page.html").write_text(
        '<html><body style="margin:0;background:#dc2828;width:100vw;height:100vh">'
        "</body></html>", encoding="utf-8")
    out = screenshot("page.html", width=200, height=120)
    assert "已截图" in out, out
    assert "像素统计" in out
    assert "200×120" in out
    assert "几乎全白" not in out
    shots = list((eye_root / ".shots").glob("*.png"))
    assert shots, "截图应当落盘到 .shots/"


# ---- 图片尺寸两道线：服务端硬上限 8192 / 我们的预算上限 3000（2026-09-15 用户拍板）

def _jpeg_header(w: int, h: int) -> bytes:
    """只造到 SOF0 段为止的 JPEG 头——尺寸解析只读头部，不需要真图像。"""
    return (b"\xff\xd8" + b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00" + b"\x00" * 9
            + b"\xff\xc0" + struct.pack(">H", 17) + b"\x08"
            + struct.pack(">HH", h, w) + b"\x03" + b"\x00" * 9)


def test_image_size_reads_headers_of_common_formats():
    """尺寸解析只读头部（不解码像素），PNG/GIF/JPEG/WebP 都要认。"""
    assert eyes._image_size(_png_noise(40, 30)) == (40, 30)
    assert eyes._image_size(b"GIF89a" + struct.pack("<HH", 12, 34) + b"\x00" * 4) == (12, 34)
    assert eyes._image_size(_jpeg_header(640, 480)) == (640, 480)
    vp8x = (b"RIFF" + struct.pack("<I", 30) + b"WEBP" + b"VP8X" + b"\x00" * 8
            + (9).to_bytes(3, "little") + (5).to_bytes(3, "little"))
    assert eyes._image_size(vp8x) == (10, 6)
    assert eyes._image_size(b"not an image") is None


def test_image_at_budget_limit_is_injected_above_is_not(eye_root, monkeypatch):
    """3000px/边 = 放行；3001px = 拦（token 经济，用户口径"上限给 3000"）。"""
    monkeypatch.setattr(eyes, "IMAGE_MAX_SIDE", 3000)
    (eye_root / "ok.png").write_bytes(_png_solid(3000, 8, (10, 20, 30)))
    (eye_root / "big.png").write_bytes(_png_solid(3001, 8, (10, 20, 30)))

    assert eyes.load_image_part("ok.png") is not None
    assert eyes.load_image_part("big.png") is None
    reason = eyes.image_reject_reason("big.png")
    assert "3001×8" in reason and "3000" in reason
    assert "区域" in reason                       # 引导范围截取，而不是只说"不行"


def test_image_over_hard_limit_is_reported_as_undeliverable(eye_root, monkeypatch):
    """超过服务端硬上限（8192）要说"发不出去"——即使预算线被调高也不放行。"""
    monkeypatch.setattr(eyes, "IMAGE_MAX_SIDE", 100_000)
    (eye_root / "huge.png").write_bytes(_png_solid(8193, 8, (10, 20, 30)))
    (eye_root / "edge.png").write_bytes(_png_solid(8192, 8, (10, 20, 30)))

    assert eyes.load_image_part("edge.png") is not None
    assert eyes.load_image_part("huge.png") is None
    assert "硬上限" in eyes.image_reject_reason("huge.png")
    assert eyes.image_reject_reason("edge.png") == ""


def test_eye_image_message_annotates_skipped_image(eye_root, monkeypatch):
    """超上限的图**不注入**，但尾部消息必须写明"未注入"。

    2026-09-15 用户报障（会话 20260915-155432-b13727）：1440×9000 的图被注入后
    服务端 400，**整轮之后每次请求都失败**（续跑也没用）。现在装配层不再注入，
    且把"没注入"讲清楚——否则工具结果那句"下一次回复会作为图像出现"就成了谎言。
    """
    from wovra.agent import Agent
    from wovra.task import Task

    monkeypatch.setattr(eyes, "IMAGE_MAX_SIDE", 3000)
    (eye_root / "big.png").write_bytes(_png_solid(4000, 8, (1, 2, 3)))
    (eye_root / "small.png").write_bytes(_png_solid(40, 8, (1, 2, 3)))
    task = Task.create(goal="g")
    agent = Agent(llm=_StubLLM(), tools=[], task=task)
    agent.current_round = {"seq": 1, "events": [
        {"type": "tool_result",
         "message": {"role": "tool",
                     "content": "【图片】path=big.png\n【图片】path=small.png"}},
    ]}

    message = agent._eye_image_message()

    assert message is not None
    kinds = [p.get("type") for p in message["content"]]
    assert kinds.count("image_url") == 1                  # 只注入小的那张
    text = message["content"][0]["text"]
    assert "未注入" in text and "big.png" in text and "3000" in text
    assert "区域" in text                                  # 引导范围截取
