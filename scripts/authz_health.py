"""授权清单体检（常驻仪器，零 LLM，只读）：越界边界现在实际是什么状态。

为什么要有它：`.wovra/authorized-paths.json` 只要躺着一条过宽条目
（盘根 / 工作区祖先目录），守卫在真实通道上就等于不存在——而"越界被放行"
的现场看起来和"守卫坏了"一模一样，查错方向会跑偏（worklog §50.6.1、§69.2）。
本仪器把清单逐条判一遍，直接给出"哪条过宽、过宽条目现在是否仍生效"。

用法：uv run --no-sync python scripts/authz_health.py
输出只打结论（≤20 行）。
"""
import json
from pathlib import Path

from wovra.tools import safety

store = safety.PROJECT_ROOT / ".wovra" / "authorized-paths.json"
print(f"清单：{store}")
try:
    entries = json.loads(store.read_text(encoding="utf-8"))
except FileNotFoundError:
    entries = []
    print("  （文件不存在 = 零授权，边界完整）")
except ValueError:
    entries = []
    print("  （文件不是合法 JSON → 加载期按空清单处理）")

if entries:
    print(f"  共 {len(entries)} 条：")
    for raw in entries:
        try:
            p = Path(str(raw)).resolve()
        except (OSError, ValueError):
            print(f"    {raw}  → 解析失败（不生效）")
            continue
        broad = safety._is_too_broad(p)
        print(f"    {raw}  → {'过宽·不生效' if broad else '生效中'}")

print("生效面抽查（过宽条目一律不作数）：")
for probe in (
    str(safety.PROJECT_ROOT / "src" / "wovra" / "task.py"),
    "C:/Windows/win.ini",
    "D:/tc/x.txt",
    "/etc/passwd",
):
    print(f"  is_authorized({probe!r}) = {safety.is_authorized(probe)}")
print(f"工作区：{safety.PROJECT_ROOT}")
