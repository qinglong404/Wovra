"""探针：会话工作区绑定与越界读取（2026-09-18 用户报"我工作路径下是空的，其为什么说
当前工作路径是 wovra，且不需要我授权就去读取修改了"）。

背景（三个事实叠在一起就成了越界）：
1. `safety.workspace_root()` = **线程绑定优先，进程默认兜底**；
2. 进程默认 `PROJECT_ROOT` = serve 的**启动目录**（`Path.cwd()`）——从 Wovra 目录起
   serve，它就是 Wovra；
3. 越界检查（`_safe_directory`/`_safe_path_lexical`）只拦"解析后落在界外"的路径——
   如果绑定**没生效**，相对路径 `docs/x.md` 会解析成 `<启动目录>/docs/x.md`，
   那**恰好在界内**，于是既读得到、也无须授权。

故"工作区是空的 test 目录、却读到了 Wovra 的文档"这一现象，唯一解释是**那一轮所在
的线程没有绑定**。本探针把绑定的三种时机各验一遍，并给出"没绑时会发生什么"。

用法：uv run --no-sync python scripts/probe_workspace_bind.py <会话ID>
"""
from __future__ import annotations

import pathlib
import sys
import threading

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from wovra import task as task_module            # noqa: E402
from wovra.agent import MODE_MANAGED             # noqa: E402
from wovra.cli.prompt import _build_agent        # noqa: E402
from wovra.tools import safety as safety_module  # noqa: E402


def main() -> int:
    task_id = sys.argv[1] if len(sys.argv) > 1 else ""
    if not task_id:
        print("用法：probe_workspace_bind.py <会话ID>")
        return 2
    task = task_module.Task.load(task_id)
    print(f"会话 {task_id}")
    print(f"  盘上 workspace 字段 = {task.workspace!r}"
          f"　（is_dir={pathlib.Path(task.workspace).is_dir()}）")
    print(f"  进程默认 PROJECT_ROOT = {safety_module.PROJECT_ROOT}（serve 的启动目录）")

    rel = "docs/context-management-v4-intent.md"
    print(f"\n同一相对路径 {rel!r} 的解析结果：")

    # ① 主线程、**不绑**（serve 的 HTTP 请求线程的常态）
    safety_module.unbind_workspace()
    try:
        got = safety_module._safe_path_lexical(rel)
        exists = got.exists()
    except Exception as e:  # noqa: BLE001
        got, exists = f"{type(e).__name__}: {e}", False
    print(f"  ① 没绑（进程默认兜底）→ {got}　exists={exists}"
          f"　{'← 越界：读到了 serve 启动目录里的文件' if exists else ''}")

    # ② 主线程、经 _build_agent 绑（CLI / serve 单线程路径）
    safety_module.unbind_workspace()
    _build_agent(task, mode=task.mode or MODE_MANAGED)
    got2 = safety_module._safe_path_lexical(rel)
    print(f"  ② _build_agent 绑过 → {got2}　exists={got2.exists()}"
          f"　{'（在本会话工作区内，可能真存在）' if got2.exists() else '（工作区内没有它 → 报"文件不存在"，正确）'}")

    # ③ 另一个线程（serve 的作业线程形状）：thread-local 不继承
    box: dict = {}

    def worker() -> None:
        safety_module.unbind_workspace()           # serve 的新线程初始就是没绑的
        try:
            p = safety_module._safe_path_lexical(rel)
            box["path"], box["exists"] = p, p.exists()
        except Exception as e:  # noqa: BLE001
            box["path"], box["exists"] = f"{type(e).__name__}: {e}", False

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    print(f"  ③ 新线程、没绑     → {box['path']}　exists={box['exists']}"
          f"　{'← 这就是实测那次越界的形状' if box['exists'] else ''}")
    print("\n结论：①③ 若读到文件，说明**那一轮所在的线程没有绑定工作区**——"
          "修法是让每个干活线程在开工前必经绑定（_build_agent 已经是唯一绑定点，"
          "要确认每条起轮/预览/命令路径都真的过它）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
