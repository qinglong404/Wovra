"""提交边界仪器：只把自己那部分 hunk 写进索引（并行会话共改同一批文件时）。

**为什么需要**（2026-09-13 实测，worklog §89.4 / §91.7 已两次记过）：多个会话
会同时改同一批文件——本节（眼睛）在改 `tools/eyes.py`、装配注入、`tokens.py`，
而另一会话同时在改 `core.py` 的事件级直播（`on_event`）、`web.py`、`webui/`。
这时 `git add <file>` 会把对方还没做完的半成品一起提交（他的测试可能还没绿），
可如果不提交，本节的功能（工具注册）又交不出去。

**做法**：按关键词筛选 `git diff` 的 hunk，只把自己那部分 `git apply --cached`
进索引——**工作区一个字节都不动**，对方接着改不受影响。

用法（默认 dry-run，只打印每个文件将写进索引的 hunk）：

    uv run --no-sync python scripts/git_stage_hunks.py --dry
    uv run --no-sync python scripts/git_stage_hunks.py --apply

规则表写在 `SPECS` 里：`文件 → (必含关键词, 排除关键词)`，逗号分隔，空 = 不限。
一个 hunk 只要**新增行**命中任一 include 且不含任何 exclude 就归我。
"""

import argparse
import re
import subprocess
import sys

# 文件 → (include 关键词, exclude 关键词)；空 tuple = 该文件全部 hunk 都归我
SPECS: dict[str, tuple[str, str]] = {
    # 只有本节的改动：整文件都归我
    "src/wovra/tools/files.py": ("", ""),
    "src/wovra/tokens.py": ("", ""),
    "src/wovra/agent/assembly.py": ("", ""),
    "tests/test_tools/_helpers.py": ("", ""),
    "tests/test_tools/test_files.py": ("", ""),
    # 与并行会话共改：按关键词挑
    "src/wovra/tools/__init__.py": ("eyes", "github"),
    "src/wovra/cli/prompt.py": ("screenshot,view_image,眼睛", "github"),
    "src/wovra/agent/support.py": ("view_image", "github"),
    "src/wovra/serve.py": ("_content_text,eyes", "on_event,直播"),
    "webui/index.html": ("screenshot,view_image,📷", ""),
    "docs/worklog-20260911.md": ("93.,眼睛,eye", "88.,GitHub 检索"),
}


def _split_patch(patch: str) -> list[tuple[str, str, list[str]]]:
    """把整份 diff 拆成 [(文件头, 文件路径, [hunk 文本…])]。"""
    files: list[tuple[str, str, list[str]]] = []
    header, hunks, path = "", [], ""
    for line in patch.splitlines(keepends=True):
        if line.startswith("diff --git "):
            if path:
                files.append((header, path, hunks))
            header, hunks = line, []
            m = re.search(r" b/(.+)$", line.rstrip("\n"))
            path = m.group(1) if m else ""
        elif line.startswith("@@"):
            hunks.append(line)
        elif hunks:
            hunks[-1] += line
        else:
            header += line
    if path:
        files.append((header, path, hunks))
    return files


def _matches(hunk: str, include: str, exclude: str) -> bool:
    added = [ln for ln in hunk.splitlines() if ln.startswith("+") and not ln.startswith("+++")]
    body = "\n".join(added)
    if exclude and any(k and k in body for k in exclude.split(",")):
        return False
    if not include:
        return True
    return any(k and k in body for k in include.split(","))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真的写进索引（默认只预览）")
    args = ap.parse_args()

    total_staged = 0
    for spec_path, (include, exclude) in SPECS.items():
        # `-U0`：零上下文——hunk 按**最小边界**切分。用默认的 -U3 时，相邻
        # 3 行内的两组改动会被黏进同一个 hunk，于是"只提交自己那份"就做不到
        # （2026-09-13 实测：并行会话的 `github_search` 与本节的眼睛注册挤在
        # 同一个 hunk 里）。零上下文补丁要用 `--unidiff-zero` 才吃。
        raw = subprocess.run(["git", "diff", "-U0", "--", spec_path],
                             capture_output=True, text=True, encoding="utf-8",
                             errors="replace").stdout
        if not raw.strip():
            continue
        keep_parts, kept, dropped = [], 0, 0
        for header, path, hunks in _split_patch(raw):
            mine = [h for h in hunks if _matches(h, include, exclude)]
            kept += len(mine)
            dropped += len(hunks) - len(mine)
            if mine:
                keep_parts.append(header + "".join(mine))
        if not kept:
            print(f"  跳过 {spec_path}（没有命中关键词的 hunk）")
            continue
        patch = "".join(keep_parts)
        if args.apply:
            # 同一文件的多个 hunk 必须**一次性**提交进索引：它们的行号都相对
            # HEAD 版本，分两次 apply 时第二次的行号已经被第一次改动移位了。
            #
            # `input=` 必须喂**字节**（text=False）：Windows 上文本模式会把
            # `\n` 改写成 `\r\n`，而 `-U0` 补丁没有上下文行可锚定，行内容一变
            # 就 "patch does not apply"（2026-09-13 实测：三个文件因此 apply 失败，
            # 报错还被 [:200] 截断成了"只有 trailing whitespace 警告"的假象）。
            res = subprocess.run(
                ["git", "apply", "--cached", "--recount", "--unidiff-zero",
                 "--whitespace=nowarn", "-"],
                input=patch.encode("utf-8"), capture_output=True)
            if res.returncode != 0:
                err = res.stderr.decode("utf-8", errors="replace").strip()
                print(f"  ✗ {spec_path}: {err[:400]}")
                continue
        print(f"  ✓ {spec_path}：收录 {kept} 个 hunk、跳过 {dropped} 个"
              f"（{'已进索引' if args.apply else '预览'}）")
        total_staged += kept
    print(f"合计 {total_staged} 个 hunk"
          f"{'已写进索引' if args.apply else '（dry-run，加 --apply 生效）'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
