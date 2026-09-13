"""Probe：当前端点（commandcode / deepseek-v4.1-flash）的多模态通道能力。

「眼睛」的设计完全取决于这两个事实，必须先证、不许猜：

  A) user 消息里的 image_url（data URI）能否被接受，模型是否真"看见"像素
     —— 生成两张纯色图（纯 stdlib zlib 造 PNG，零依赖），问颜色。
  B) **tool 角色的消息**能否以 content 数组（text + image_url）承载图片
     —— 这是 Wovra 的关键：view_image 若走不通 tool 通道，就得改设计
        （结果文本回引用 + 装配期注入图片，或退化成 user 消息）。

结论口径：颜色答对 = 模型真看到像素（不是靠文件名/提示词猜的——文件名与
提示词里都不含颜色词）。跑法：uv run --no-sync python scripts/probe_vision_channel.py
"""
import base64
import struct
import sys
import zlib

from wovra.llm import LLM


def png_solid(w: int, h: int, rgb: tuple) -> bytes:
    """造一张纯色 PNG（纯 stdlib；不引依赖）。"""
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def data_uri(raw: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(raw).decode()


def ask(llm: LLM, messages: list[dict], tag: str) -> str:
    try:
        resp = llm.chat(messages, stream=False)
    except Exception as error:  # noqa: BLE001——探针要的是事实
        return f"{tag} 请求被拒：{type(error).__name__}: {str(error)[:220]}"
    text = (resp.choices[0].message.content or "").strip().replace("\n", " ")
    return f"{tag} 返回：{text[:160]}"


def main() -> int:
    llm = LLM()
    print(f"端点 {llm.base_url}　模型 {llm.model}")
    red = data_uri(png_solid(96, 96, (220, 40, 40)))
    blue = data_uri(png_solid(96, 96, (40, 80, 220)))

    # A：user 通道（两张图，问顺序颜色）
    a = ask(llm, [{"role": "user", "content": [
        {"type": "text", "text": "这两张图分别是纯色。按顺序说出每种颜色，只答两个词。"},
        {"type": "image_url", "image_url": {"url": red}},
        {"type": "image_url", "image_url": {"url": blue}},
    ]}], "A(user 通道)")

    # B：tool 通道（tool 角色 + 数组 content）
    b = ask(llm, [
        {"role": "user", "content": "调用 look 工具看图。"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "look", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": [
            {"type": "text", "text": "look 工具返回一张图。"},
            {"type": "image_url", "image_url": {"url": red}},
        ]},
        {"role": "user", "content": "那张图是什么颜色？只答一个词。"},
    ], "B(tool 通道)")

    print(a)
    print(b)
    print("判读：A/B 都答出「红」才算通道可用；B 报错则 view_image 须改走装配期注入。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
