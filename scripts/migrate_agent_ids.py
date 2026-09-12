"""老会话 Agent ID 全量迁移：v1（主 agent 占 `A`）→ v2（主 agent = `Main`）。

背景（2026-09-12 用户拍板）：v1 把主 agent 占成 `A`，于是第一次分裂的顶层域
只能是 `A-1`、`A-2`…（看起来像"主 agent 的子目录"），再裂一层成 `A-1-1`——
与 `docs/思考内容与AI对话.md` §五（A = 大类、A-1 = 子类）正好差一级。v2：
主 agent = `Main`，顶层域取 `A`、`B`、`C`…，`A` 满了才在 A 内裂 `A-1`。

本脚本把这批数据在**盘上**一次搬完（`Task.load` 也会自愈，但 serve/webui 有
直接读 task.json 的路径，故盘面必须一致）。迁移只动 **ID 形态**的字段：
注册表 `id`、收件箱 `from/to`、轮上的 `active_view` / `route_explicit` /
`route_handoff`，以及 `pending_view`；自由文本与职责描述一字不动。
迁移**非幂等**，故靠 `agent_id_scheme` 把住（做完落 2）。

用法：
    uv run --no-sync python scripts/migrate_agent_ids.py            # 预演（不改盘）
    uv run --no-sync python scripts/migrate_agent_ids.py --apply    # 落盘（先备份）
输出只打结论（≤20 行）：扫了多少会话、要迁几个、每处改动计数。
"""
import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from wovra import registry as registry_module  # noqa: E402
from wovra import task as task_module  # noqa: E402


def _needs_migration(data: dict) -> bool:
    """是否需要迁移：scheme 未到 2，且盘面真有旧形态（空会话不白动）。"""
    if int(data.get("agent_id_scheme") or 1) >= registry_module.AGENT_ID_SCHEME:
        return False
    for entry in data.get("registry") or []:
        if not isinstance(entry, dict):
            continue
        if registry_module.migrate_legacy_id(str(entry.get("id") or "")) != str(
            entry.get("id") or ""
        ):
            return True
        for item in entry.get("inbox") or []:
            if isinstance(item, dict) and any(
                str(item.get(k) or "") == registry_module.LEGACY_MAIN_AGENT_ID
                for k in ("from", "to")
            ):
                return True
    for r in data.get("rounds") or []:
        if not isinstance(r, dict):
            continue
        if any(
            str(r.get(k) or "") == registry_module.LEGACY_MAIN_AGENT_ID
            for k in ("active_view", "route_explicit")
        ):
            return True
        handoff = r.get("route_handoff")
        if isinstance(handoff, dict) and any(
            str(handoff.get(k) or "") == registry_module.LEGACY_MAIN_AGENT_ID
            for k in ("from", "to")
        ):
            return True
    return str(data.get("pending_view") or "") == registry_module.LEGACY_MAIN_AGENT_ID


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真的落盘（默认只预演）")
    args = ap.parse_args()

    root = task_module.TASKS_ROOT
    scanned = migrated = 0
    total_changes = 0
    samples: list[str] = []
    for path in sorted(root.glob("*/task.json")):
        scanned += 1
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            print(f"  跳过 {path.parent.name}：读取失败 {error!r}")
            continue
        if not _needs_migration(data):
            continue
        rounds = data.get("rounds") or []
        moved, detail = registry_module.migrate_agent_ids(
            data.get("registry"), rounds
        )
        if str(data.get("pending_view") or "") == registry_module.LEGACY_MAIN_AGENT_ID:
            data["pending_view"] = registry_module.MAIN_AGENT_ID
            moved += 1
        data["agent_id_scheme"] = registry_module.AGENT_ID_SCHEME
        migrated += 1
        total_changes += moved
        if len(samples) < 3:
            samples.append(f"{path.parent.name}({moved}处 {'、'.join(detail[:4])})")
        if not args.apply:
            continue
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        shutil.copy2(path, path.with_suffix(f".json.bak-agentids-{stamp}"))
        text = json.dumps(data, ensure_ascii=False, indent=2)
        task_module._write_atomic(path, text)

    mode = "已落盘" if args.apply else "预演（未改盘）"
    print(f"agent ID v1→v2 迁移：{mode}　扫描 {scanned} 个会话，"
          f"需迁移 {migrated} 个，改动 {total_changes} 处")
    for s in samples:
        print(f"  - {s}")
    if not args.apply and migrated:
        print("  加 --apply 落盘（每个会话先写 task.json.bak-agentids-<时间戳> 备份）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
