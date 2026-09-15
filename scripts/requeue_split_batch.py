"""把"已整理但产物被拒/过期"的批次回入水位（卡死会话的恢复工具）。

## 它解决什么

分裂产物被拒（或过期回入水位前的老数据）时，那批轮的 `org_state` 停在 `done`：
`_unorganized_rounds()` 只收 `""`/`pending`/`failed`，于是它们**再也不会被整理**，
会话就永久没有可用域树（实测 20260914-181519-0d3875：R1-R6 done、产品被拒、
注册表只有 Main）。本工具把这些轮改回 `org_state=failed` —— 下一次水位维护会把
它们与新轮一起重新整理+分裂，产物就能覆盖全部材料、正常落地。

判据（只挑真正需要重做的）：
* `org_state == "done"`（整理过）；
* `domains` 为空（没有生效的域树）；
* `split_state` ∈ {"", "stale", "rejected"}（未落地/过期/被拒）。

## 用法

    uv run --no-sync python scripts/requeue_split_batch.py <会话id>            # 预演（只打印）
    uv run --no-sync python scripts/requeue_split_batch.py <会话id> --apply    # 落盘（先备份 task.json）

`--apply` 会先把 task.json 备份成 `task.json.bak-<时间戳>`，可随时回滚。
"""
from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from wovra import task as task_module  # noqa: E402


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    apply = "--apply" in sys.argv
    if len(args) != 1:
        print(__doc__)
        return 2
    sid = args[0]
    path = task_module.TASKS_ROOT / sid / "task.json"
    if not path.exists():
        print(f"找不到 {path}")
        return 1
    data = json.loads(path.read_text(encoding="utf-8"))
    rounds = data.get("rounds") or []
    pick = [
        r for r in rounds
        if str(r.get("org_state") or "") == "done"
        and not (r.get("domains") or [])
        and str(r.get("split_state") or "") in ("", "stale", "rejected")
    ]
    if not pick:
        print(f"{sid}：没有需要回入水位的轮（已整理且无域树的轮为 0）")
        return 0
    print(f"{sid}：将回入水位的轮 = {[r.get('seq') for r in pick]}")
    for r in pick:
        print(f"  R{r.get('seq')}: org_state=done → failed，split_state="
              f"{r.get('split_state') or '(空)'} → stale")
    if not apply:
        print("\n（预演。确认无误后加 --apply 落盘；落盘前会自动备份 task.json）")
        return 0
    backup = path.with_suffix(f".json.bak-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(path, backup)
    for r in pick:
        r["org_state"] = "failed"
        r["split_state"] = "stale"
    data.setdefault("history", []).append({
        "time": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "kind": "maintenance",
        "detail": ("requeue_split_batch：把已整理但无域树的轮回入水位"
                   f"（{[r.get('seq') for r in pick]}）——下批维护重做整理+分裂"),
    })
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n已落盘。备份：{backup}")
    print("下一次水位维护（或轮闭合触达水位）会把它们与新轮一起重做。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
