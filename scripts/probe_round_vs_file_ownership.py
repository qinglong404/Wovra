"""查清"文件块归属"与"轮归属"的关系（只读，零 LLM）——回答用户的质疑：

用户口径："R5 没被整理，但按机制它应该已经被判断分给哪个 agent 了，这些都是一块进行的。"

要回答的是：**轮的归属（active_view）决定不了文件块的归属**——文件块只按"文件 ∈ 某域文件集合"
匹配（`views.ownership` 判据 2），匹配不到就落主 agent（判据 4）。所以要看：
  1. 每一轮的 active_view 是什么；
  2. 那些文件块实际判给了谁；
  3. 分裂的硬数据里到底有没有"哪个域碰了哪些文件"（有 → 模型能认领；没有 → 机制缺件）。

用法：`uv run --no-sync python scripts/probe_round_vs_file_ownership.py <会话ID>`
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path("src")))
from wovra import views as V  # noqa: E402
from wovra.task import Task  # noqa: E402


def main() -> int:
    sid = sys.argv[1] if len(sys.argv) > 1 else ""
    if not sid:
        print("用法：probe_round_vs_file_ownership.py <会话ID>")
        return 2
    task = Task.load(sid)
    built = V.build_views(task.rounds, task.get_state(), registry=task.registry)
    owners = built.get("owners") or {}
    index = built.get("index") or {}

    print("=== 每轮的归属（active_view）× 它的文件块被判给了谁 ===")
    for r in task.rounds:
        av = str(r.get("active_view") or "（空）")
        org = str(r.get("org_state") or "（未整理）")
        fs = []
        for b in (r.get("blocks") or []):
            if str(b.get("kind")) == "file":
                bid = b.get("id") or ""
                who = owners.get(bid) or owners.get(f"R{r.get('seq')}-{bid}") or "?"
                fs.append(f"{b.get('file')}→{who}")
        print(f"  R{r.get('seq')}  active_view={av}  org={org}  文件块 {len(fs)} 个")
        for x in fs[:6]:
            print(f"       {x}")
        if len(fs) > 6:
            print(f"       …另有 {len(fs)-6} 个")

    print("\n=== 分裂的硬数据里有没有'哪个域碰了哪些文件' ===")
    try:
        lines = V.split_coverage_lines(task.rounds, task.registry)  # type: ignore[attr-defined]
        print("  split_coverage_lines 存在，" + f"{len(lines)} 行：")
        for x in lines[:10]:
            print("   " + str(x)[:100])
    except Exception as exc:  # noqa: BLE001
        print(f"  （{type(exc).__name__}: {exc}）")
        # 退一步：找喂给分裂的其他硬数据函数
        names = [n for n in dir(V) if "split" in n.lower() or "coverage" in n.lower()
                 or "hard" in n.lower()]
        print("  views 里可疑的硬数据入口：" + "、".join(names))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
