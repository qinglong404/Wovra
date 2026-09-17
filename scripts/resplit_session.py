"""重跑某个会话的**分裂**并替换结果（真跑一次 LLM ＋ 走生产落地）。

## 它解决什么

分裂产物不对（过分分裂、同名重复、域树过期）时，光改代码不会让**已经落地的**
那份结果变好——注册表里的子 agent 与轮上的域树都还挂着旧产物。本工具把旧产物
清掉（轮上的 `split_state`/`domains` 与注册表里的子 agent），然后让**生产代码**
重新跑一次分裂分析并落地，于是"改完机制立刻看到新结果"。

走的是生产那条路（`agent._maybe_split_v4` → `_split_rounds` → 落地），所以输入的
指令、机械归并（同名合并 / 留痕域降级 / 从未分开的域合并）与线上逐字一致。

## 用法（项目根执行）

    uv run --no-sync python scripts/resplit_session.py <会话id>           # 副本上跑，只打印
    uv run --no-sync python scripts/resplit_session.py <会话id> --apply   # 落在会话上（先备份 task.json）

`--apply` 前会把 `task.json` 备份成 `task.json.bak-<时间戳>`（回滚＝把备份拷回去）。

**活跃会话注意**：正在跑的 `wovra serve` 内存里握着旧数据，它下一次落盘会把
`--apply` 的结果覆盖回去。要么先让它停下/结束该会话的轮，要么重启 serve 后再用。
脚本不删（§0.3）。
"""
from __future__ import annotations

import argparse
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from wovra.agent import MODE_MANAGED                    # noqa: E402
from wovra.cli import _build_agent                      # noqa: E402
from wovra import registry as registry_module           # noqa: E402
from wovra import task as task_module                   # noqa: E402

COPY_ROOT = Path("/tmp/wovra-resplit/tasks")


def _reset_split_state(task) -> tuple[int, list[str]]:
    """清掉旧分裂产物：轮上的 split_state/domains ＋ 注册表里的子 agent。

    返回 (清掉的轮数, 清掉的 agent id)。
    """
    rounds = 0
    for r in task.rounds or []:
        if not isinstance(r, dict):
            continue
        if r.get("split_state") or r.get("domains") or r.get("pending_org"):
            rounds += 1
        r["split_state"] = ""
        r.pop("domains", None)
        r.pop("pending_org", None)
    dropped: list[str] = []
    keep = []
    for entry in task.registry or []:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("id")) == registry_module.MAIN_AGENT_ID:
            keep.append(entry)
            continue
        dropped.append(str(entry.get("id")))
    task.registry[:] = keep
    return rounds, dropped


def _print_registry(task) -> None:
    print("\n=== 落地后的注册表 ===")
    for entry in task.registry or []:
        if not isinstance(entry, dict):
            continue
        files = [str(f) for f in (entry.get("files") or [])]
        print(f"  {entry.get('id')}｜{entry.get('name')}｜status={entry.get('status')}"
              f"｜文件 {len(files)}")
        for path in files[:8]:
            print(f"      - {path}")
        if len(files) > 8:
            print(f"      …另 {len(files) - 8} 个")
        print(f"      职责：{str(entry.get('description') or '')[:110]}")


def main() -> int:
    ap = argparse.ArgumentParser(description="重跑会话的分裂并替换结果")
    ap.add_argument("session_id")
    ap.add_argument("--apply", action="store_true",
                    help="落在会话上（默认只在副本上跑，原会话一个字节不碰）")
    args = ap.parse_args()

    src = task_module.TASKS_ROOT / args.session_id
    if not (src / "task.json").is_file():
        raise SystemExit(f"会话不存在：{src}")

    if args.apply:
        backup = src / f"task.json.bak-{time.strftime('%Y%m%d-%H%M%S')}"
        shutil.copy2(src / "task.json", backup)
        print(f"已备份：{backup}")
        root = task_module.TASKS_ROOT
    else:
        COPY_ROOT.mkdir(parents=True, exist_ok=True)
        dst = COPY_ROOT / args.session_id
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(src, dst)
        root = COPY_ROOT
        print(f"副本：{dst}（原会话不动）")

    task_module.TASKS_ROOT = root
    task = task_module.Task.load(args.session_id)
    rounds_n, dropped = _reset_split_state(task)
    print(f"清掉旧产物：{rounds_n} 轮标记、注册表子 agent {dropped or '（无）'}")

    agent = _build_agent(task, mode=task.mode or MODE_MANAGED,
                         async_organization=False)
    agent.rounds = task.rounds
    if not agent._live_files():
        raise SystemExit("没有活性文件——分裂无从谈起")
    print(f"活性文件 {len(agent._live_files())} 个，跑分裂…", flush=True)

    before = len(task.history or [])
    agent._maybe_split_v4()
    # **域树也要落到轮上**：注册表由"提前落实"即时可见，而 `domains`（树面板 / 过去轮
    # 渲染字段）等轮边界——这里没有开放轮，故顺手把边界那一步跑掉，否则页面上的树
    # 还是旧的那份（本工具刚把它清掉了）。
    agent._settle_after_maintenance()
    agent._persist_rounds()

    print("\n=== 本次维护留痕 ===")
    for h in (task.history or [])[before:]:
        if str(h.get("kind")) in ("maintenance", "split", "ownership"):
            print(f"  [{h.get('kind')}] {str(h.get('detail'))[:150]}")
    states = {int(r["seq"]): r.get("split_state") for r in task.rounds or []}
    tree_rounds = [int(r["seq"]) for r in task.rounds or [] if r.get("domains")]
    print(f"\n轮上的 split_state：{states}")
    print(f"带域树的轮：{tree_rounds or '（无——树没落到轮上）'}")
    _print_registry(task)
    if args.apply:
        task.save()
        print("\n已写回会话（task.json 已更新）")
    else:
        print("\n（副本演练：要落到会话上，加 --apply）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
