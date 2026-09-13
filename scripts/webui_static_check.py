"""前端静态体检（常驻仪器，零依赖）。

为什么要有：同一类 bug 已经踩过一次——`webui/index.html` 里出现过**两个
`applyChunk`**（后来声明的赢），我改了前面那个、以为改好了，实际跑的是旧的那份。
JS 对这类错误**不报错**，所以只能机械扫。

查四件事（都只看 `webui/index.html` 的 `<script>` 块与标记本身）：

1. **顶层重复声明**：`function NAME` / 顶层 `const|let|var NAME` 出现两次以上——
   后者静默覆盖前者（就是 applyChunk 那次）。
2. **调了不存在的函数**：`NAME(` 调用点里，`NAME` 既没在本页声明、也不在浏览器/
   依赖白名单里（典型的"改名了但调用点没跟着改"）。
3. **引用了不存在的 id**：`getElementById('x')` / `$('#x')` / `querySelector('#x')`，
   而页面标记里没有 `id="x"`（元素被删了但调用点还在）。
4. **引用已删除的类**：`querySelector('.x')` 里的类既不在 CSS 也不在标记里，
   且不在"动态生成"的例外表里。

用法：`uv run --no-sync python scripts/webui_static_check.py`（有问题时退出码 1）
"""
import pathlib
import re
import sys

PAGE = pathlib.Path("webui/index.html")

# 浏览器/运行时/页面依赖提供的名字（不在本页声明，但调用合法）
GLOBALS = {
    # 语言与浏览器
    "if", "for", "while", "switch", "catch", "return", "function", "typeof",
    "Object", "Array", "String", "Number", "Boolean", "Math", "JSON", "Date",
    "Set", "Map", "Promise", "RegExp", "Error", "Symbol", "BigInt", "Intl",
    "parseInt", "parseFloat", "isNaN", "isFinite", "encodeURIComponent",
    "decodeURIComponent", "encodeURI", "decodeURI", "structuredClone",
    "setTimeout", "clearTimeout", "setInterval", "clearInterval",
    "requestAnimationFrame", "cancelAnimationFrame", "queueMicrotask",
    "fetch", "alert", "confirm", "prompt", "console", "performance",
    "document", "window", "navigator", "location", "history", "localStorage",
    "sessionStorage", "URL", "URLSearchParams", "Blob", "File", "FileReader",
    "TextDecoder", "TextEncoder", "AbortController", "CustomEvent", "Event",
    "Element", "Node", "HTMLElement", "CSS", "getComputedStyle", "matchMedia",
    "Image", "Audio", "ResizeObserver", "IntersectionObserver", "MutationObserver",
    "WebSocket", "EventSource", "FormData", "Headers", "Request", "Response",
    "WeakMap", "WeakSet", "Proxy", "Reflect", "Function", "eval",
    # 语法噪声（正则误匹配时的兜底）
    "new", "await", "yield", "delete", "void", "in", "of", "do", "else", "case",
    "try", "finally", "throw", "class", "extends", "super", "this", "true",
    "false", "null", "undefined", "async",
}

# 动态生成的类（由 JS 拼出来/框架加上的），不要求出现在 CSS/标记里
DYNAMIC_CLASSES = {
    "open", "on", "md", "mono", "acc", "bad", "interim", "user", "live-prov",
    "agent", "you", "me", "msg", "bub", "hljs", "hidden", "selected", "active",
    "empty", "loading", "err", "warn", "ok", "st-running", "st-run", "st-ok",
    "st-err", "st-wait", "think-box", "think-head", "think-body", "tcard",
    "t-head", "t-icon", "t-name", "t-sum", "t-when", "t-st", "t-jump",
    "t-args", "t-result", "live-done", "live-tail", "livebox", "live-line",
    "live-dot", "rl-dot", "rl-st", "rl-time", "runline", "runbar", "sep",
    "sysline", "mtime", "gbox", "ccol", "crow", "avatar", "cname", "tabs",
    "tab", "chip", "kpi", "lcard", "tcard", "tdir", "hist", "step-list",
    "acc-list", "more-btn", "follow-chip", "pend-bar", "stop-btn",
}


def page_scripts(text: str) -> str:
    return "\n;\n".join(re.findall(r"<script[^>]*>(.*?)</script>", text, re.S))


def strip_literals(js: str) -> str:
    """去掉字符串与注释——否则 CSS 文本/文案里的 `var(--x)`、`linear-gradient(`
    会被当成函数调用（第一版扫出 4 个假阳性）。模板串整体去掉（里面的 `${}` 也就
    不扫了：宁可漏，不要假——假阳性会让人不再相信仪器）。"""
    js = re.sub(r"/\*.*?\*/", " ", js, flags=re.S)
    js = re.sub(r"//[^\n]*", " ", js)
    js = re.sub(r"`(?:\\.|[^`\\])*`", "``", js, flags=re.S)
    js = re.sub(r"'(?:\\.|[^'\\\n])*'", "''", js)
    js = re.sub(r'"(?:\\.|[^"\\\n])*"', '""', js)
    return js


def markup(text: str) -> str:
    """去掉 <script> 块之后的标记（找 id/class 用）。"""
    return re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.S)


def check_duplicate_declarations(js: str) -> list[str]:
    """顶层重复声明：JS 静默覆盖，最坑。"""
    bad: list[str] = []
    # 行首（缩进 0）的 function / const / let / var 声明
    for kind, pat in (("function", r"^function\s+([A-Za-z_$][\w$]*)\s*\("),
                      ("var", r"^(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=")):
        seen: dict[str, int] = {}
        for line in js.splitlines():
            m = re.match(pat, line)
            if m:
                seen[m.group(1)] = seen.get(m.group(1), 0) + 1
        for name, n in seen.items():
            if n > 1:
                bad.append(f"顶层 {kind} `{name}` 声明了 {n} 次（后一个静默覆盖前一个）")
    return bad


def check_calls_to_undefined(js: str) -> list[str]:
    """调了不存在的函数（改名后调用点没跟着改）。"""
    declared = set(re.findall(r"function\s+([A-Za-z_$][\w$]*)\s*\(", js))
    declared |= set(re.findall(r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=", js))
    declared |= set(re.findall(r"window\.([A-Za-z_$][\w$]*)\s*=", js))
    declared |= set(re.findall(r"^\s*([A-Za-z_$][\w$]*)\s*[:=]\s*(?:async\s*)?\(", js, re.M))
    called = set(re.findall(r"(?<![\w$.])([A-Za-z_$][\w$]*)\s*\(", js))
    bad = []
    for name in sorted(called):
        if name in declared or name in GLOBALS:
            continue
        # 属性写法 `obj.name(` 已被负向断言排除；剩下的可疑
        bad.append(f"调用 `{name}(` 但本页没有声明、也不在浏览器白名单里")
    return bad


def check_ids(js: str, page: str) -> list[str]:
    """引用了页面里不存在的 id。

    两处收紧（第一版有 9 个假阳性）：**选择器里只取 id 那个 token**
    （`#content .rsep` 是"content 里的 rsep"，不是 id 叫 `content .rsep`）；
    **id 也从整个页面文本里收**（面板是 `innerHTML='<div id="x">'` 动态拼出来的，
    只在标记里找会误判）。
    """
    have = set(re.findall(r'\bid="([^"]+)"', page))
    have |= set(re.findall(r"\bid='([^']+)'", page))
    have |= set(re.findall(r"\.id\s*=\s*['\"]([^'\"]+)['\"]", js))
    refs: set[str] = set()
    for pat in (r"getElementById\(\s*'([^']+)'", r'getElementById\(\s*"([^"]+)"',
                r"\$\(\s*'([^']+)'", r'\$\(\s*"([^"]+)"',
                r"querySelector(?:All)?\(\s*'([^']+)'",
                r'querySelector(?:All)?\(\s*"([^"]+)"'):
        for sel in re.findall(pat, js):
            # 从选择器里挑出所有 `#token`，每个 token 都要存在
            refs |= set(re.findall(r"#([A-Za-z_][\w-]*)", sel))
    return [f"引用了不存在的 id `#{i}`（元素被删了但调用点还在）"
            for i in sorted(refs - have)]


def check_classes(js: str, mark: str) -> list[str]:
    """querySelector 里的类既不在 CSS、也不在标记、也不在动态例外表里。"""
    have = set(re.findall(r'class="([^"]*)"', mark))
    have = {c for group in have for c in group.split()}
    have |= set(re.findall(r"\.([A-Za-z][\w-]*)\s*[,{]", mark))     # CSS 选择器
    have |= set(re.findall(r'className\s*=\s*[\'"]([^\'"]+)[\'"]', js))
    have |= set(re.findall(r"classList\.(?:add|remove|toggle)\(\s*'([^']+)'", js))
    have |= DYNAMIC_CLASSES
    refs: set[str] = set()
    for pat in (r"querySelector(?:All)?\(\s*'\.([A-Za-z][\w-]*)'",
                r'querySelector(?:All)?\(\s*"\.([A-Za-z][\w-]*)"',
                r"closest\(\s*'\.([A-Za-z][\w-]*)'",
                r"classList\.contains\(\s*'([^']+)'"):
        refs |= set(re.findall(pat, js))
    return [f"查了类 `.{c}`，但它既不在 CSS/标记里、也不在动态例外表里"
            for c in sorted(refs - {x for g in have for x in g.split()})]


def check_interval_pairing(js: str) -> list[str]:
    """`setInterval` 必须有配对的 `clearInterval`（否则页面越用越卡）。

    这是"流畅"的一条硬指标：一个没停的 300ms 轮询会让界面在**空闲时也一直动**
    （用户看到的是"卡"和掉帧，不是省电问题）。
    """
    arms = len(re.findall(r"\bsetInterval\s*\(", js))
    clears = len(re.findall(r"\bclearInterval\s*\(", js))
    if arms and not clears:
        return [f"有 {arms} 处 setInterval 但一处 clearInterval 都没有（定时器停不下来）"]
    return []


def main() -> int:
    text = PAGE.read_text(encoding="utf-8")
    raw_js = page_scripts(text)
    js = strip_literals(raw_js)          # 去掉字符串/注释后再做"调用点"分析
    mark = markup(text)
    checks = [
        ("顶层重复声明", check_duplicate_declarations(js)),
        ("调了不存在的函数", check_calls_to_undefined(js)),
        ("引用了不存在的 id", check_ids(raw_js, text)),
        ("疑似失效的类选择器", check_classes(js, mark)),
        ("定时器配对", check_interval_pairing(js)),
    ]
    total = 0
    for title, items in checks:
        if items:
            print(f"\n【{title}】{len(items)} 项")
            for it in items:
                print("  ✗ " + it)
            total += len(items)
        else:
            print(f"【{title}】干净")
    print(f"\n静态体检：{'通过' if not total else f'{total} 项待查'}")
    return 0 if not total else 1


if __name__ == "__main__":
    raise SystemExit(main())
