"""路径匹配：把"字符串全等"换成三档鲁棒匹配（2026-09-14 用户口径）。

背景：分裂产物里的路径由模型抄写，形态天然会漂——`agent-test/a.md` 与
`a.md`（会话工作区就是 agent-test）、`/home/u/ws/a.md` 与 `a.md`、
`./a.md` 与 `a.md`、Windows 反斜杠……此前一律字符串全等，抄法一变就判
"漏项"，整批中止（用户实测："确实太脆了"）。

三档，按可靠性从高到低，**只在唯一命中时才认**：

1. **精确**：归一后全等（去空白、`\\`→`/`、折叠 `//`、去 `./` 前缀与尾斜杠）。
2. **路径边界后缀**：一方是另一方的尾巴，且尾巴从分隔符开始
   （`agent-test/a.md` ↔ `a.md`、`/home/u/ws/a.md` ↔ `a.md`）。
3. **唯一同名**：两侧 basename 相同，且候选里同名的**目录只有一种**
   （`a/index.js` 与 `b/index.js` 同时出现 → 歧义，不认——宁可报错也不串文件）。

目录前缀（旧 `file_domains` 形态，如 `src/wovra/tools`，无扩展名）**不参与
模糊**：它按"前缀归属"处理（`under()`），防同名目录误伤。
"""

from __future__ import annotations

__all__ = ["norm", "basename", "dirname", "matches", "under", "entry_matches"]


def norm(path: str) -> str:
    """路径归一：反斜杠统一、折叠斜杠、**消解 `.` 与 `..`**、去尾部斜杠、去空白。

    `..` 只在有前驱段可消时消解（`sub/../a.py` → `a.py`；`../a.py` 保持原样
    ——它是工作区外的兄弟路径）。**保留前导 `/`**——绝对路径（`/etc/hostname`）
    是合法形态，模糊匹配的后缀档会处理"带不带前导斜杠"的差异。

    为什么必须消解（2026-09-14 实测）：账本里同一个文件会以多种写法出现
    （`t1_hello.txt`、`./t1_hello.txt`、`sub/../t1_hello.txt`——agent 当时就是
    这么访问它的），不归一会变成"三个不同文件"且互相歧义。
    """
    s = str(path or "").strip().replace("\\", "/")
    # **剥掉引用符**（2026-09-15）：模型常把路径写成 `cpp/src/a.py`、'src/a.py'、
    # "a.py"——反引号/引号不是路径的一部分；混进匹配就永远对不上（实测会话
    # 20260915-131130-010611 分裂两次 27/29 个文件全"没有域认领"）。
    s = s.strip("`'\"")
    while "//" in s:
        s = s.replace("//", "/")          # 先折叠（".//a" → "./a"）
    while s.startswith("./"):
        s = s[2:]
    s = s.rstrip("/")
    if not s:
        return ""
    absolute = s.startswith("/")
    out: list[str] = []
    for seg in s.split("/"):
        if seg in ("", "."):
            continue
        if seg == ".." and out and out[-1] != "..":
            out.pop()                     # 有前驱段才消解；前缀 .. 保留
        else:
            out.append(seg)
    joined = "/".join(out)
    if absolute and joined:
        return "/" + joined
    return joined or ("/" if absolute else "")


def basename(path: str) -> str:
    return norm(path).rsplit("/", 1)[-1]


def dirname(path: str) -> str:
    n = norm(path)
    return n.rsplit("/", 1)[0] if "/" in n else ""


def _file_like(path: str) -> bool:
    """像"文件"的条目才参与模糊（末段带扩展名）；目录前缀不参与。"""
    return "." in basename(path)


def _tail_hit(query: str, candidate: str) -> bool:
    """一方是另一方的尾巴，且尾巴从路径分隔符开始。"""
    q, c = norm(query), norm(candidate)
    if not q or not c or q == c:
        return False
    return q.endswith("/" + c.lstrip("/")) or c.endswith("/" + q.lstrip("/"))


def matches(query: str, candidates) -> bool:
    """query（Runtime 侧的真路径）是否被 candidates（模型抄的路径们）命中。

    三档：精确 → 路径边界后缀 → 唯一同名（候选里同名文件的目录只有一种）。
    """
    q = norm(query)
    if not q:
        return False
    cs = [norm(c) for c in (candidates or []) if str(c or "").strip()]
    if not cs:
        return False
    if q in cs:
        return True
    if not _file_like(q):
        return False                       # 目录不做模糊（交给 entry_matches）
    # 路径边界后缀：多个**不同目录**的尾巴同时命中 → 歧义，不认
    # （`a.py` 对 ["x/a.py","y/a.py"] 不许算命中）
    tails = {dirname(c) for c in cs if _file_like(c) and _tail_hit(q, c)}
    if len(tails) == 1:
        return True
    if len(tails) > 1:
        return False
    # 唯一同名兜底（同上：同名目录只有一种才算）
    bq = basename(q)
    dirs = {dirname(c) for c in cs if basename(c) == bq}
    return len(dirs) == 1


def under(path: str, prefix: str) -> bool:
    """path 是否在目录 prefix 下（含相等）。

    容忍两种写法之一带更长前缀：`agent-test/a.py` 与
    `/home/u/ws/agent-test` 也算在它下面（会话工作区相对形态 vs 绝对形态）。
    """
    p, pre = norm(path), norm(prefix)
    if not p or not pre:
        return False
    if p == pre or p.startswith(pre + "/"):
        return True
    d = dirname(p)
    return bool(d) and pre.endswith("/" + d)


def entry_matches(path: str, entry: str) -> bool:
    """单个"条目"（可能是文件，也可能是旧形态的目录前缀）是否认领 path。"""
    if matches(path, [entry]):
        return True
    if _file_like(entry):
        return False
    return under(path, entry)
