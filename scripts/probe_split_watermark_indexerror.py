"""复现探针：定位 promote 时 `_record_split_lifecycle` 吞掉的 IndexError。

背景（2026-09-12，会话 20260912-110034-98d42c）：promote 落注册表成功，但紧接
着的 `views.view_watermarks(...)` 抛 `IndexError('list index out of range')`，
被 `_record_split_lifecycle` 的 except 吞掉 → 生命周期动作 / status / 经济判据
全部没落账（7 个域至今全 dormant）。

本探针遍历"promote 那一刻可能的现场差异"，打印每个复现用例的最小签名：
  0. 开新轮瞬间（新轮 events 为空）——promote 就发生在这一刻；
  1. 轮数前缀截断；
  2. 某轮事件被截短（只留前 k 条）。
"""
import shutil
import sys
import tempfile
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from wovra import registry as reg  # noqa: E402
from wovra import task as tm  # noqa: E402
from wovra import views  # noqa: E402

TID = "20260912-110034-98d42c"


def main() -> None:
    root = Path(tempfile.mkdtemp())
    shutil.copytree(REPO / "tasks" / TID, root / TID)
    tm.TASKS_ROOT = root
    t = tm.Task.load(TID)
    domains = reg.latest_domains(t.rounds)
    state = t.get_state()
    hits: list[str] = []
    calls = 0

    def run(rounds, tag: str) -> bool:
        nonlocal calls
        calls += 1
        try:
            views.view_watermarks(
                rounds, state, domains=domains, registry=t.registry
            )
        except Exception:  # noqa: BLE001——探针就是要抓这个
            tail = traceback.format_exc().strip().splitlines()[-1].strip()
            hits.append(f"ERR {tag}  ←  {tail}")
            return False
        return True

    # 用例 0：开新轮瞬间——promote 的真实现场（新轮只有 user_input，events 空）
    fresh = {
        "seq": len(t.rounds) + 1,
        "user_input": {"original": "现在分裂是什么情况？", "normalized": ""},
        "events": [], "refined_index": {}, "end_state": "open",
        "org_state": "", "active_view": "", "route_hops": 0,
    }
    run(list(t.rounds) + [fresh], "开新轮瞬间（新轮 events=[]）")

    run(t.rounds, "原样")
    for n in range(1, len(t.rounds) + 1):
        run([dict(r) for r in t.rounds[:n]], f"截断到前 {n} 轮")
    for i, r in enumerate(t.rounds):
        for k in (0, 1, 2):
            rs = [dict(x) for x in t.rounds]
            rr = dict(rs[i])
            rr["events"] = (rr.get("events") or [])[:k]
            rs[i] = rr
            run(rs, f"R{r.get('seq')} 只留前 {k} 事件")

    errs = [h for h in hits if h.startswith("ERR")]
    # 计数修正（2026-09-12）：旧版打的是 `len(hits) + len(errs)`（只数错误，
    # 还重复计一遍），修好后会打出"探针：0 次调用"这种自相矛盾的结论——
    # 仪器给错数比没数更糟，故如实数调用次数。
    print(f"探针：{calls} 次调用，复现 {len(errs)} 次"
          + ("（0 = 越界守卫已生效）" if not errs else ""))
    for h in errs:
        print(" ", h)


if __name__ == "__main__":
    main()
