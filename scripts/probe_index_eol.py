"""取证：索引（staged）与 HEAD 的行尾状态——判断分块提交是否污染了索引。

背景（2026-09-13 实测）：`scripts/git_stage_hunks.py` 第一轮用 **text 模式**喂
patch 给 `git apply --cached`，Windows 上 subprocess 的 text 模式会把 `\\n`
改写成 `\\r\\n` → 索引里那几行变成 CRLF，而仓库（HEAD 与工作区经 clean 过滤后）
是 LF。后果：`git diff` 显示"整个文件都变了"，且提交会把混合行尾写进版本库。

判据：
* `HEAD CRLF = 0` = 仓库策略是 LF（本仓库如此）；
* `index CRLF > 0` 而该文件本轮只改了少数几行 = **索引被污染**，须重新暂存。

只读，不改任何东西。
"""
import subprocess
import sys

FILES = [
    "src/wovra/tools/__init__.py",
    "src/wovra/cli/prompt.py",
    "src/wovra/agent/support.py",
    "src/wovra/serve.py",
    "src/wovra/agent/assembly.py",
    "src/wovra/tokens.py",
    "src/wovra/tools/files.py",
    "tests/test_tools/_helpers.py",
    "tests/test_tools/test_files.py",
    "webui/index.html",
    "docs/worklog-20260911.md",
]


def crlf(blob: bytes) -> int:
    return blob.count(b"\r\n")


def show(rev: str, path: str) -> bytes:
    res = subprocess.run(["git", "show", f"{rev}:{path}"], capture_output=True)
    return res.stdout if res.returncode == 0 else b""


def main() -> int:
    print(f"{'文件':<40}{'HEAD':>6}{'索引':>6}{'工作区':>8}   判定")
    polluted = []
    for path in FILES:
        head = crlf(show("HEAD", path))
        idx = crlf(show("", path))          # 冒号前为空 = 索引版本
        try:
            with open(path, "rb") as fh:
                work = crlf(fh.read())
        except OSError:
            work = -1
        staged = subprocess.run(["git", "diff", "--cached", "--numstat", "--", path],
                                capture_output=True, text=True).stdout.strip()
        judge = "干净"
        if idx > 0 and head == 0:
            judge = "★索引被污染（CRLF）"
            polluted.append(path)
        print(f"{path:<40}{head:>6}{idx:>6}{work:>8}   {judge}   [{staged or '未暂存'}]")
    print()
    if polluted:
        print("需重新暂存（先 git restore --staged，再用字节模式重新 apply）：")
        for p in polluted:
            print("   ", p)
    else:
        print("索引行尾干净（全 LF），无需处理。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
