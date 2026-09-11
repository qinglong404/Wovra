"""演示「大输出不进上下文但可定位、可全取、不丢信息」（2026-09-12，worklog §27）。

零 LLM 只读演示：跑一次即出三段证据（超限提示 / pattern 定位 / spill 可搜）。
保留不删（用户口径：测试脚本不要删）。
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

os.environ["WOVRA_OUTPUT_LIMIT"] = "500"   # 便于触发超限
os.environ["WOVRA_PREVIEW_CHARS"] = "120"

from wovra.tools import files, limits  # noqa: E402

big = "\n".join(f"line {i} junk" for i in range(1, 2001))
big = big.replace("line 900 junk", "line 900 ERROR boom")
print(f"[原文] {len(big):,} 字符 / 2000 行，其中第 900 行含 ERROR")

out = limits.clip(big, "demo")
print(f"\n[1] 超限返回：{len(out)} 字符（原文 {len(big):,}）")
print(out[:200].replace("\n", "⏎"))
print("...")
print(out[out.rfind("…"):][:220])

spill = sorted((Path(files.safety.PROJECT_ROOT) / "output" / "spill").glob("*demo*.txt"))[-1]
rel = spill.relative_to(files.safety.PROJECT_ROOT).as_posix()

hit = files.read_file(rel, pattern="ERROR")
print(f"\n[2] read_file(pattern) 定位：{hit[:200].replace(chr(10), '⏎')}")

found = files.search_files("ERROR", directory="output/spill")
print(f"\n[3] search_files 搜 spill 目录：{found[:160]}")

full_env = os.environ.pop("WOVRA_OUTPUT_LIMIT", None)  # 全量权限：放开上限再读
os.environ.pop("WOVRA_PREVIEW_CHARS", None)
full = files.read_file(rel, num_lines=2000)
print(f"\n[4] 全量读取（放开上限后）：{len(full)} 字符，首尾都在 = "
      f"{'line 1 junk' in full and 'line 2000 junk' in full}")
if full_env is not None:
    os.environ["WOVRA_OUTPUT_LIMIT"] = full_env
