"""worklog 提交工具：并行会话共存时的「按节暂存」与「节序整理」。

背景（2026-09-14）：`docs/worklog-20260911.md` 是**多会话共用**的文件——同一时刻
工作区里可能既有我这一节、也有别人未提交的节。直接 `git add` 会把别人的在途内容
一起裹进我的提交，而每节的回滚边界是「`git revert` 本节」，裹进去就破坏了这个边界。

用法一（暂存：把不归本次的节从**索引版本**里排除，工作区文件不动）：
    python scripts/maint_worklog_tool.py exclude "## §103 " "## §104 "
  → 构造「当前索引 + 工作区内容 − 指定节」的 blob，写进索引。
  只影响 index，工作区文件原样保留（别人的节不丢、不提交）。

用法二（节序：把某节整体移到文件末尾，保持节号可读升序）：
    python scripts/maint_worklog_tool.py reorder "## 105. "

留档：工程/取证脚本不删（用户口径 2026-09-11）。
"""

from __future__ import annotations

import re
import subprocess
import sys

REPO = "/home/lkf/bc/python/Wovra"
WORKLOG = "docs/worklog-20260911.md"

_HEADING = re.compile(r"^## ", re.M)


def _sections(text: str) -> list[tuple[int, str]]:
    """返回 [(起始偏移, 标题行)]，按出现顺序。"""
    return [(m.start(), text[m.start():text.find("\n", m.start())])
            for m in _HEADING.finditer(text)]


def _path() -> str:
    return f"{REPO}/{WORKLOG}"


def read_disk() -> str:
    with open(_path(), encoding="utf-8") as fh:
        return fh.read()


def read_index() -> str:
    out = subprocess.run(("git", "show", f":{WORKLOG}"), cwd=REPO,
                         capture_output=True, text=True)
    return out.stdout if out.returncode == 0 else ""


def write_index(text: str) -> None:
    blob = subprocess.run(("git", "hash-object", "-w", "--stdin"), cwd=REPO,
                          capture_output=True, text=True, input=text).stdout.strip()
    subprocess.run(("git", "update-index", "--cacheinfo",
                    f"100644,{blob},{WORKLOG}"), cwd=REPO, check=True)
    print(f"索引已更新 blob={blob} 行数={text.count(chr(10))}")


def exclude(prefixes: tuple[str, ...]) -> None:
    """把标题以任一 prefix 开头的节从索引版本里去掉（工作区不动）。"""
    text = read_disk()
    spans = _sections(text)
    drop = [i for i, (_, title) in enumerate(spans)
            if any(title.startswith(p) for p in prefixes)]
    if not drop:
        print("未匹配到要排除的节：", prefixes)
        return
    keep = text
    for i in reversed(drop):
        start = spans[i][0]
        end = spans[i + 1][0] if i + 1 < len(spans) else len(text)
        keep = keep[:start] + keep[end:]
    keep = re.sub(r"\n{3,}", "\n\n", keep).rstrip("\n") + "\n"
    write_index(keep)
    print("排除的节：", [spans[i][1] for i in drop])


def reorder(prefix: str) -> None:
    """把标题以 prefix 开头的节整体移到文件末尾（只改工作区文件）。"""
    text = read_disk()
    spans = _sections(text)
    idx = next((i for i, (_, t) in enumerate(spans) if t.startswith(prefix)), None)
    if idx is None:
        print("未找到节：", prefix)
        return
    start = spans[idx][0]
    end = spans[idx + 1][0] if idx + 1 < len(spans) else len(text)
    section = text[start:end].rstrip()
    if section.endswith("---"):
        section = section[:-3].rstrip()
    rest = (text[:start] + text[end:])
    rest = re.sub(r"\n{3,}", "\n\n", rest).rstrip("\n")
    new = rest + "\n\n" + section + "\n"
    with open(_path(), "w", encoding="utf-8") as fh:
        fh.write(new)
    print(f"节已移到末尾：{spans[idx][1]}  行数 {text.count(chr(10))} → {new.count(chr(10))}")


def append(prefixes: tuple[str, ...]) -> None:
    """把工作区里指定节**追加到索引版本末尾**（工作区不动）。

    用法：先 `git add` 本次的文件（索引里只该有本次的改动），再 append 本次
    对应的 worklog 节——索引 = 当前索引 + 这些节，别人的在途节一律不进。
    """
    idx_text = read_index()
    if not idx_text:
        print("索引里读不到 worklog，先 git add 一次该文件")
        return
    disk = read_disk()
    spans = _sections(disk)
    picked = []
    for prefix in prefixes:
        hit = next(((i, t) for i, (_, t) in enumerate(spans)
                    if t.startswith(prefix)), None)
        if hit is None:
            print("未找到节：", prefix)
            return
        picked.append(hit[0])
    parts = []
    for i in sorted(picked):
        start = spans[i][0]
        end = spans[i + 1][0] if i + 1 < len(spans) else len(disk)
        sec = disk[start:end].rstrip()
        if sec.endswith("---"):
            sec = sec[:-3].rstrip()
        parts.append(sec)
    out = idx_text.rstrip("\n") + "\n\n" + "\n\n".join(parts) + "\n"
    write_index(out)
    print("追加的节：", [spans[i][1] for i in sorted(picked)])


def main(argv: list[str]) -> None:
    if not argv:
        print(__doc__)
        return
    if argv[0] == "exclude":
        exclude(tuple(argv[1:]))
    elif argv[0] == "append":
        append(tuple(argv[1:]))
    elif argv[0] == "reorder":
        reorder(argv[1])
    else:
        print("未知动作：", argv[0])


if __name__ == "__main__":
    main(sys.argv[1:])
