"""页面能不能被**浏览器**解析（不只是 JS 语法）：查 HTML 解析陷阱。

`node --check` 只验 JS 语法——它看不见"脚本块被 HTML 解析器提前结束"这类问题。
浏览器在 `<script>` 里的终止条件是**第一个 `</script`**（大小写不敏感的 `</script`），
另外 `<!--` 会让解析器进入 script-data-escaped 状态、行为很怪。
页面上出现空白/不渲染，先查这两个。

用法：`uv run --no-sync python scripts/webui_html_check.py`
"""
import pathlib
import re
import sys

PAGE = pathlib.Path("webui/index.html")


def main() -> int:
    t = PAGE.read_text(encoding="utf-8")
    bad = 0

    # ① 脚本块里的 `</script`：只有真正的收尾标签才允许
    opens = [m.start() for m in re.finditer(r"<script\b[^>]*>", t, re.I)]
    closes = [m.start() for m in re.finditer(r"</script\s*>", t, re.I)]
    if len(opens) != len(closes):
        print(f"  ✗ script 开闭不配对：开 {len(opens)} 个 / 闭 {len(closes)} 个")
        bad += 1
    # 逐块检查：块内不允许再出现 `</script`（除块尾那个）
    for i, o in enumerate(opens):
        end = closes[i] if i < len(closes) else len(t)
        body = t[o:end]
        inner = re.findall(r"</script", body, re.I)
        if len(inner) > 0:
            print(f"  ✗ 第 {i+1} 个脚本块内部又出现 `</script`（解析器会提前结束）")
            bad += 1

    # ② `<!--`（script-data-escaped 状态）
    for i, o in enumerate(opens):
        end = closes[i] if i < len(closes) else len(t)
        body = t[o:end]
        if "<!--" in body:
            print(f"  ⚠ 第 {i+1} 个脚本块里有 `<!--`（HTML 解析器会进 script-data-escaped）")
            bad += 1

    # ③ 明显的标签不闭合（只查最常见的 div/script/body/html）
    for tag in ("div", "script", "body", "html", "head", "style"):
        o = len(re.findall(rf"<{tag}\b", t, re.I))
        c = len(re.findall(rf"</{tag}\s*>", t, re.I))
        if o != c:
            print(f"  ✗ <{tag}> 开闭不配对：{o} / {c}")
            bad += 1

    # ④ 关键容器在不在（缺了会"什么都不渲染"）
    for i in ("content", "tabs", "composer", "send"):
        if f'id="{i}"' not in t:
            print(f"  ✗ 缺容器 #{i}（页面会是空的）")
            bad += 1

    print(f"\nHTML 体检：{'通过' if not bad else f'{bad} 项待查'}")
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
