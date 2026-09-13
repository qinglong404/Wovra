"""提交前最后一闸：索引里每个改动过的 .py 都必须能编译。

为什么需要：分块提交（`scripts/git_stage_hunks.py`）只把命中关键词的 hunk 写进
索引。若一个文件本该收 9 个 hunk 却只收了 8 个，工作区仍然一切正常（测试跑的是
工作区），**只有提交出去的树是坏的**——这类错误靠跑工作区测试永远发现不了。
故在提交前把索引版本抽出来逐个编译（`git show :path` → py_compile）。

只读，不改任何东西。
"""
import py_compile
import subprocess
import sys
import tempfile
from pathlib import Path


def main() -> int:
    names = subprocess.run(["git", "diff", "--cached", "--name-only"],
                           capture_output=True, text=True).stdout.split()
    py_files = [n for n in names if n.endswith(".py")]
    if not py_files:
        print("索引里没有 .py 改动")
        return 0
    bad = []
    with tempfile.TemporaryDirectory() as tmp:
        for name in py_files:
            blob = subprocess.run(["git", "show", ":" + name],
                                  capture_output=True).stdout
            target = Path(tmp) / Path(name).name
            target.write_bytes(blob)
            try:
                py_compile.compile(str(target), doraise=True, cfile=str(target) + "c")
            except py_compile.PyCompileError as error:
                bad.append(f"{name}: {str(error).splitlines()[-1][:160]}")
    if bad:
        print("索引版本编译失败（提交出去就是坏的）：")
        for line in bad:
            print("  ✗", line)
        return 1
    print(f"索引版本全部可编译（{len(py_files)} 个 .py）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
