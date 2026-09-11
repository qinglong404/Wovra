"""把历史会话落盘的 v1 粗分块刷成 v3（一次性迁移；默认只预演不改盘）。

用法：
    uv run python scripts/migrate_blocks_v3.py <task-id>            # 预演
    uv run python scripts/migrate_blocks_v3.py <task-id> --apply    # 落盘
    uv run python scripts/migrate_blocks_v3.py --all --apply        # 全部会话

注意：本脚本**直接读写 task.json 原始 JSON**，不走 `Task.load`——
后者自 2026-09-11 起带加载期自愈（读到 v1 块会重算为 v3 并落盘），
那样"预演"就不再只读。这里显式区分预演与落盘，预演保证零副作用。

运行时其实**不需要**本脚本：新进程加载会话时会自动自愈（幂等），
活跃会话也因此不必手动迁移（旧进程会用内存里的旧数据覆盖回去，
只有重启后的新进程能治）。本脚本用于批量/离线场景与人工核对。

落盘前自动备份为 task.json.bak-<时间戳>（与 task.json 同目录）。
迁移逻辑见 wovra.blocks.migrate（纯函数、幂等、零 LLM）。
"""
import json
import shutil
import sys
import time

from wovra import task as task_module
from wovra.blocks import migrate as migrate_module


def _targets(argv: list[str]) -> list[str]:
    ids = [a for a in argv if not a.startswith("--")]
    if ids:
        return ids
    if "--all" in argv:
        root = task_module.TASKS_ROOT
        if not root.exists():
            return []
        return sorted(
            p.name for p in root.iterdir() if (p / "task.json").exists()
        )
    return []


def main() -> None:
    argv = sys.argv[1:]
    apply = "--apply" in argv
    ids = _targets(argv)
    if not ids:
        print(__doc__)
        return

    total_changed = 0
    for tid in ids:
        path = task_module.TASKS_ROOT / tid / "task.json"
        if not path.exists():
            print(f"!! {tid}: 无 task.json")
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as error:
            print(f"!! {tid}: {error}")
            continue
        rounds = data.get("rounds") or []
        changed, report = migrate_module.migrate_rounds(rounds)
        total_changed += changed
        head = f"{tid}: {changed}/{len(rounds)} 轮需要迁移"
        if not changed:
            print(f"{head}（已是 v3，无需改动）")
            continue
        print(head)
        for seq, before, after in report:
            print(f"    R{seq}: [{before}] → [{after}]")
        if not apply:
            continue
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup = path.with_name(f"task.json.bak-{stamp}")
        shutil.copy2(path, backup)
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"    已落盘（备份：{backup.name}）")

    if not apply and total_changed:
        print(f"\n预演结束：共 {total_changed} 轮待迁移。加 --apply 落盘（自动备份）。")
    elif apply:
        print(f"\n迁移完成：共改写 {total_changed} 轮。")


if __name__ == "__main__":
    main()
