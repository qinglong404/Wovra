"""把"未覆盖文件"分成两类（只读，零 LLM）——这是判"机制对不对"的关键分界：

  ① **曾是某域的活文件、后来变成历史**（被重构替代/被删/只读）→ 按口径留在主 agent 残留桶 ✓
  ② **从来没被任何域认领过** → 这才是"按文件长成域"该解释却没解释的情况

口径依据（`_SPLIT_INSTRUCTIONS`）：**文件域聚合只对 LIVE 文件**（被写过、还在的）做；
`views._file_domain_entries` 的注释说明 `history_files` 按判据 4 留主 agent 残留桶。

用法：`uv run --no-sync python scripts/probe_uncovered_files.py <会话ID>`
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path("src")))
from wovra import registry as R  # noqa: E402
from wovra import views as V  # noqa: E402
from wovra.task import Task  # noqa: E402


def main() -> int:
    sid = sys.argv[1] if len(sys.argv) > 1 else ""
    if not sid:
        print("用法：probe_uncovered_files.py <会话ID>")
        return 2
    task = Task.load(sid)
    built = V.build_views(task.rounds, task.get_state(), registry=task.registry)
    gap = V.coverage_gap(built.get("domains"), task.rounds,
                         index=built.get("index"), owners=built.get("owners"))
    uncovered = [str(f) for f in (gap.get("uncovered") or [])]

    ever_owned: dict[str, str] = {}      # 文件 → 哪个域曾经/现在持有
    now_owned: dict[str, str] = {}
    hist_owned: dict[str, str] = {}
    for e in task.registry:
        name = str(e.get("name") or e.get("id"))
        for f in (e.get("files") or e.get("file_domains") or []):
            now_owned[str(f).replace("\\", "/")] = name
        try:
            for f in R.entry_history(e):
                hist_owned[str(f).replace("\\", "/")] = name
        except Exception:  # noqa: BLE001
            pass
    ever_owned = {**hist_owned, **now_owned}

    only_history, never = [], []
    for f in uncovered:
        key = f.replace("\\", "/")
        (only_history if key in hist_owned else never).append((f, ever_owned.get(key, "")))

    # 每个未认领文件被哪些轮/块碰过（判断"是别的域的活儿顺手读的"还是"独立的一摊"）
    touch: dict[str, list[str]] = {}
    for r in task.rounds:
        for b in (r.get("blocks") or []):
            if str(b.get("kind")) != "file":
                continue
            k = str(b.get("file") or "").replace("\\", "/")
            touch.setdefault(k, []).append(f"R{r.get('seq')}")

    print(f"=== 会话 {sid}：未覆盖文件 {len(uncovered)} 个 ===")
    print(f"① 曾属于某域、现在是历史（口径允许，留主 agent 残留桶）：{len(only_history)} 个")
    for f, who in only_history[:8]:
        print(f"     {f}　（曾属：{who}）")
    print(f"\n② **从来没被任何域认领过**：{len(never)} 个　← 这才需要解释")
    for f, _ in sorted(never, key=lambda x: -len(touch.get(x[0].replace(chr(92), '/'), [])))[:18]:
        k = f.replace("\\", "/")
        print(f"     {f}　被 {','.join(touch.get(k, [])) or '（无块）'} 碰过"
              f"　块数={len(touch.get(k, []))}")
    print(f"\n总块数 {sum(len(v) for k, v in touch.items() if k in {x[0].replace(chr(92),'/') for x in never})}"
          f" 来自'从未认领'的文件")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
