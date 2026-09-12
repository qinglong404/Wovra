"""路由回放仪器（常驻，零 LLM、只读）：在真实历史轮上回放路由规则。

为什么常驻：设计稿 §10-6 的验收标准（"对历史轮离线回放规则，记命中率与误
命中率各一条"）与 R73 的候选 B（真实会话量粘滞率/切换代价）都要同一份读数。
一次取证多处引用——别每问一次就重写一个临时脚本。

用法：

    uv run --no-sync python scripts/route_replay.py            # 最近更新的会话
    uv run --no-sync python scripts/route_replay.py <task_id>

口径：

* 输入 = 历史每轮的 `user_input.original`（用户原话，路由的唯一输入）；
* 职责表 = 当前 `task.registry`（隔离后路由只读它）；
* 文件名补强 = 材料侧真实出现过的文件（`views.files_by_domain`）；
* 粘滞 = 前一轮的（回放得出的）视图——这就是开关打开后的粘滞率预测值，
  不是"实际发生过的"（开关默认关，落盘的 active_view 恒为主 agent）。
* 多域命中按第一版口径**交主 agent**（不猜），单独计数——它是"关联最大"
  算法的取数入口（§12-10）。

纪律：真实会话**只读**——先复制到临时 TASKS_ROOT 再加载，绝不改动 tasks/
（§13.2 教训：活跃会话的进程内存握着旧 rounds，手动改盘会被覆盖）。
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from wovra import routing as routing_module  # noqa: E402
from wovra import task as task_module  # noqa: E402
from wovra import views as views_module  # noqa: E402
from wovra.task import Task  # noqa: E402

REAL_TASKS = REPO / "tasks"


def _latest_task_id() -> str:
    cands = [p for p in REAL_TASKS.glob("*/task.json") if p.parent.name]
    if not cands:
        raise SystemExit("tasks/ 下没有会话")
    return max(cands, key=lambda p: p.stat().st_mtime).parent.name


def _load_copy(task_id: str, tmp: Path) -> Task:
    src = REAL_TASKS / task_id
    if not src.exists():
        raise SystemExit(f"会话不存在: {task_id}")
    shutil.copytree(src, tmp / task_id, dirs_exist_ok=True)
    task_module.TASKS_ROOT = tmp
    return Task.load(task_id)


def report(task_id: str) -> None:
    with tempfile.TemporaryDirectory(prefix="wovra-route-") as td:
        task = _load_copy(task_id, Path(td))
        registry = task.registry
        rounds = [r for r in task.rounds if isinstance(r, dict)]
        domains = views_module.latest_domains(rounds)
        hints = views_module.files_by_domain(rounds, domains) if domains else {}
        entries = [e for e in registry if str(e.get("id")) != "A"]
        print(f"会话 {task_id}　轮 {len(rounds)}　注册表 {len(registry)} 条"
              f"（子域 {len(entries)}）　域树 {len(domains)} 域")

        sticky = ""
        switches = 0
        reasons: dict[str, int] = {}
        per_view: dict[str, int] = {}
        multi = 0
        no_text = 0
        examples: list[str] = []
        for r in rounds:
            text = str((r.get("user_input") or {}).get("original") or "")
            if not text.strip():
                no_text += 1
                continue
            result = routing_module.route(
                text, registry, sticky=sticky, explicit="", file_hints=hints,
            )
            view = str(result["view"])
            if len(result.get("matched") or []) > 1:
                multi += 1
                if len(examples) < 5:
                    examples.append(
                        f"R{r.get('seq')}：{'、'.join(result['matched'])}"
                        f"｜{text[:26]}"
                    )
            if view != sticky and sticky:
                switches += 1
            sticky = view
            per_view[view] = per_view.get(view, 0) + 1
            key = str(result["reason"]).split("：")[0].split("（")[0]
            reasons[key] = reasons.get(key, 0) + 1

        answered = len(rounds) - no_text
        domain_hits = sum(n for v, n in per_view.items()
                          if v != views_module.MAIN_AGENT_ID)
        print("── 判定分布（回放）──")
        for key, n in sorted(reasons.items(), key=lambda kv: -kv[1]):
            print(f"  {key}: {n}")
        print(f"多域命中（第一版交主 agent）：{multi}"
              + (f"　例：{examples[0]}" if examples else ""))
        if multi and len(examples) > 1:
            for line in examples[1:]:
                print(f"    {line}")
        print("── 视图分布与粘滞 ──")
        top = sorted(per_view.items(), key=lambda kv: -kv[1])
        print("  " + "　".join(f"{name}:{n}" for name, n in top[:8]))
        rate = (answered - switches) / answered if answered else 0.0
        print(f"回放轮 {answered}（无用户原文 {no_text} 跳过）　"
              f"切换 {switches} 次　粘滞率 {rate:.1%}（目标 >80%）")
        print(f"命中子域 {domain_hits}/{answered}"
              f"（{domain_hits / answered:.0%}）　落主 agent "
              f"{per_view.get(views_module.MAIN_AGENT_ID, 0)}/{answered}")
        # 实测落盘的 active_view（开关关闭时恒为主 agent——这行用来证明"开关没开"）
        recorded = {
            str(r.get("active_view") or views_module.MAIN_AGENT_ID)
            for r in rounds
        }
        print(f"落盘 active_view 取值集合：{sorted(recorded)}")


def main() -> int:
    args = sys.argv[1:]
    report(args[0] if args else _latest_task_id())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
