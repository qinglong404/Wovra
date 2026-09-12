"""探针：证明"固定 tmp 名 + replace"在 Windows 上并发保存确实会炸，修复后不炸。

为什么保留（2026-09-12，worklog §42）：并发回归测试必须真的**能**失败，否则
它只是空过。本探针把两种写法放在同一台机器、同一并发度下对照——旧写法是修复
前 `Task.save` 的逐字同形，新写法直接调 `task._write_atomic`：

    uv run --no-sync python scripts/probe_save_race.py

判据：旧写法错误数 > 0（确认问题真实存在）且新写法 = 0（确认修复对症）。
"""

from __future__ import annotations

import sys
import tempfile
import threading
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from wovra import task as task_module  # noqa: E402

THREADS = 6
ROUNDS = 200


def _hammer(write, rounds: int = ROUNDS) -> list[str]:
    """并发跑 write()，返回错误清单（首个错误即停该线程，够说明问题了）。"""
    errors: list[str] = []

    def worker() -> None:
        for _ in range(rounds):
            try:
                write()
            except Exception as error:  # noqa: BLE001
                errors.append(f"{type(error).__name__}: {str(error)[:60]}")
                return

    threads = [threading.Thread(target=worker) for _ in range(THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return errors


def _old_style(directory: Path):
    """修复前的 Task.save 落盘写法（固定 tmp 名 + replace）。"""
    target = directory / "x.json"
    tmp = directory / "x.json.tmp"

    def write() -> None:
        tmp.write_text("z" * 5000, encoding="utf-8")
        tmp.replace(target)

    return write


def _new_style(directory: Path):
    """修复后的写法（唯一 tmp 名 + 串行 + 重试）。"""
    target = directory / "x.json"

    def write() -> None:
        with task_module._save_lock("probe"):
            task_module._write_atomic(target, "z" * 5000)

    return write


def main() -> int:
    old = _hammer(_old_style(Path(tempfile.mkdtemp())))
    new = _hammer(_new_style(Path(tempfile.mkdtemp())))
    print(f"旧写法（固定 tmp 名）并发 {THREADS}×{ROUNDS}：错误 {len(old)}")
    if old:
        print("  样例:", old[0])
    print(f"新写法（唯一 tmp + 串行 + 重试）：错误 {len(new)}")
    if new:
        print("  样例:", new[0])
    ok = bool(old) and not new
    print("结论：" + ("对症（旧必炸、新不炸）" if ok else "**存疑——判据未满足**"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
