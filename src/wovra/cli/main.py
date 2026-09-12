"""CLI 入口：argparse 定义、子命令分发、单发型命令（run/report/list/delete）。

入口点 `wovra = "wovra.cli:main"` 经包 __init__ 再导出，保持不变。
"""
import argparse
import json
import shutil

from .. import task as task_module
from .. import ui
from ..agent import MODE_BASELINE, MODE_MANAGED
from ..llm import LLMConfigError
from ..serve import cmd_serve
from ..tools import stop_session_backgrounds

from .prompt import _build_agent
from .session import _acquire_session_lock, _all_tasks, _child_summaries, _load_task, _process_alive, _record_leftover_maintenance, _release_session_lock, _resolve_mode, _resolve_task_id
from .render import _run_turn
from .interactive import cmd_chat

def cmd_run(args: argparse.Namespace) -> None:
    """wovra run：对既有任务执行一轮。

    不给指令时，让模型根据任务状态和最近事件自主决定下一步——
    这是"长时任务自主推进"的雏形。
    """
    task = _load_task(args.task_id)
    _acquire_session_lock(task)
    try:
        # 一次性进程：整理同步执行，退出前结果必须落盘。
        # 指令优先级：命令行 > 父任务派发时写入的 pending_instruction
        #（子任务异步派发的决策回传通道）> 自主推进
        mode = _resolve_mode(args.mode, task)
        agent = _build_agent(task, mode=mode, async_organization=False)
        instruction = args.instruction or task.pending_instruction.strip() or (
            "请根据任务状态和最近事件，自主决定下一步并继续推进。"
            "如果任务已无法推进，说明原因。"
        )
        if task.pending_instruction:
            task.pending_instruction = ""
            task.save()
        _run_turn(agent, instruction)
        # 一次性进程收尾：水位未触发的剩余未整理轮补一批整理——
        # TaskState 跟上进度，下一次自主推进才有准确的"任务状态"
        agent.organize_backlog()
        _record_leftover_maintenance(agent, task, "收尾补整理记账")
    finally:
        # 一次性进程：会话结束，其后台任务一并收掉
        stop_session_backgrounds()
        _release_session_lock(task)

def cmd_report(args: argparse.Namespace) -> None:
    """wovra report：人机协同报告（TaskState 机械渲染，零模型成本）。

    机械渲染 TaskState 与轮次——零模型成本，随时可看。大局默认，
    细节对主 agent 说"展开 R{k}-E{nn}"按事件 ID 剥开。
    """
    task = _load_task(args.task_id)
    print(ui.report_view(task, _child_summaries(task.id)))

def cmd_maint(args: argparse.Namespace) -> None:
    """wovra maint：查看整理（organization）与分裂（split）进度。

    task.json 的机械渲染，零模型成本（与 report/list 同类）——整理
    分布（org_state/代次/水位）、分裂产物（可分裂性/域归属）、维护
    批次记账（history 里 kind==maintenance 的启动/结束记录）一览。
    省略 task_id 时取最近更新的会话。
    """
    if args.task_id:
        task = _load_task(args.task_id)
    else:
        tasks = _all_tasks()
        if not tasks:
            print(ui.info("还没有任何任务。直接 `wovra chat` 开始第一个会话。"))
            return
        task = _load_task(tasks[0]["id"])
    print(ui.maint_view(task))

def cmd_views(args: argparse.Namespace) -> None:
    """wovra views：查看按域分化的上下文视图（机械渲染，零模型成本）。

    这是 Level 1 第二步（装配按域分化）的**检视口**：分裂产物说"哪些文件
    属哪个职责域"，本命令把每个域**实际会拿到的上下文材料**打出来——
    逐轮筛出的历史（命中轮的用户原文与用户块 + 本域块一行描述）、
    按文件域切片的账本、域卡与身份。非本域的块与轮一律不出现（隔离第一）。
    零 LLM、不改会话状态，纯读取（同 report/maint 一类）。

    不给域参数时列全部域的规模摘要；给了则打该域视图全文。
    """
    if args.task_id:
        task = _load_task(args.task_id)
    else:
        tasks = _all_tasks()
        if not tasks:
            print(ui.info("还没有任何任务。直接 `wovra chat` 开始第一个会话。"))
            return
        task = _load_task(tasks[0]["id"])

    try:
        from ..views import build_views, human_report
    except ImportError as exc:  # pragma: no cover——模块缺失属安装问题
        raise SystemExit(ui.error(f"域视图模块不可用：{exc}")) from None

    built = build_views(task.rounds, task.get_state(), registry=task.registry)
    if not args.domain:
        print(ui.rule("域视图摘要"))
        for line in human_report(built, registry=task.registry):
            if line.startswith("- "):
                print(line)
        print(ui.info("\n用 `wovra views <域id或名字>` 看某个域的完整视图。"))
        return

    want = args.domain.strip()
    view = None
    for name, v in built["views"].items():
        if want in (name, v["path_id"]) or want == v["path_id"].lower():
            view = v
            break
    if view is None:
        known = "、".join(f"{v['path_id']}({n})" for n, v in built["views"].items())
        raise SystemExit(ui.error(f"未找到域：{want}。现存：{known}"))
    print(ui.rule(f"域视图：{view['path_id']}（{view['name']}）"))
    print(view["text"])


def cmd_list(args: argparse.Namespace) -> None:
    """wovra list：列出所有任务（按更新时间倒序，带数字编号）。"""
    tasks = _all_tasks()
    if not tasks:
        print(ui.info("还没有任何任务。直接 `wovra chat` 开始第一个会话。"))
        return

    print()
    # 列对齐必须"先 pad 再 paint"（见 ui.pad 的说明），否则着色码
    # 会破坏 f-string 的补齐计算，列会粘在一起
    header = (
        ui.paint(ui.pad("编号", 6), "bold")
        + ui.paint(ui.pad("任务 id", 26), "bold")
        + ui.paint(ui.pad("状态", 10), "bold")
        + ui.paint(ui.pad("更新时间", 18), "bold")
        + ui.paint("目标", "bold")
    )
    print(header)
    for number, t in enumerate(tasks, start=1):
        goal = t["goal"] or "（目标待明确）"
        goal = goal if len(goal) <= 36 else goal[:36] + "…"
        updated = t["updated_at"].replace("T", " ")[:16]
        print(
            ui.paint(ui.pad(str(number), 6), "cyan")
            + ui.pad(t["id"], 26)
            + ui.status(t["status"], width=10)
            + ui.pad(updated, 18)
            + goal
        )
    print(ui.info("\n编号按最近更新排序，run/chat/show 可直接用编号作为 <id>。"))

def cmd_delete(args: argparse.Namespace) -> None:
    """wovra delete：删除一个会话及其全部本地数据（不可恢复）。

    支持 list 里的编号或完整 id。正被另一个进程持锁使用的会话
    拒绝删除——否则那个进程的每一步落盘都会把半份数据写回来；
    陈旧锁随目录一起清掉。删除前确认，脚本化使用加 --force。
    """
    task_id = _resolve_task_id(args.task_id)
    directory = task_module.TASKS_ROOT / task_id
    if not directory.exists():
        raise SystemExit(
            ui.error(f"任务不存在: {args.task_id}（用 `wovra list` 查看现有任务）")
        )

    # 展示字段尽力而为地读：半途损坏的 task.json 也是合法的删除对象
    try:
        data = json.loads((directory / "task.json").read_text(encoding="utf-8"))
        goal = data.get("goal") or "（目标待明确）"
        updated = data.get("updated_at", "").replace("T", " ")[:16]
    except (OSError, ValueError):
        goal, updated = "（task.json 损坏或缺失）", "未知"

    lock = directory / ".lock"
    if lock.exists():
        pid = 0
        try:
            pid = int(lock.read_text(encoding="utf-8").strip() or 0)
        except (OSError, ValueError):
            pass
        # 活进程持锁 → 拒绝；读不出的锁视为陈旧，随目录一起删掉
        if _process_alive(pid):
            raise SystemExit(
                ui.error(
                    f"会话 {task_id} 正在另一个进程中使用（pid {pid}），"
                    "请先关闭该会话再删除。"
                )
            )

    if not args.force:
        try:
            answer = input(
                f"确认删除会话 {task_id}（目标：{goal}，更新于 {updated}）？"
                "删除后不可恢复 [y/N] "
            )
        except EOFError:
            print(ui.info("无交互输入可用，已取消。确定删除请加 --force。"))
            return
        if answer.strip().lower() not in ("y", "yes"):
            print(ui.info("已取消，未删除。"))
            return

    shutil.rmtree(directory)
    print(ui.success(f"已删除会话: {task_id}"))

def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="wovra",
        description=ui.paint("Wovra：面向结构化、长时运行 AI 工作的运行时。", "bold"),
        epilog=(
            "示例：\n"
            "  wovra chat                  开一个新会话直接聊，目标随对话成形\n"
            "  wovra list                  查看所有会话（带编号）\n"
            "  wovra chat 1                用编号续上某个会话\n"
            "  wovra delete 1              删除某个会话（不可恢复）\n"
            "\n"
            "<id> 位置既可用编号（list 里的 1、2、3…），也可用完整任务 id。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command")

    p_run = sub.add_parser("run", help="对任务执行一轮")
    p_run.add_argument("task_id", help="任务编号或完整任务 id")
    p_run.add_argument("instruction", nargs="?", help="本轮指令；省略则由 AI 自主继续")
    p_run.add_argument("--mode", choices=[MODE_MANAGED, MODE_BASELINE], default=None,
                       help="上下文策略：省略则沿用会话记录（无记录则 managed）")
    p_run.set_defaults(func=cmd_run)

    p_chat = sub.add_parser("chat", help="交互式多轮对话（不带 id 则新建会话）")
    p_chat.add_argument("task_id", nargs="?", default="", help="任务编号或完整任务 id；省略则新建")
    p_chat.add_argument("--mode", choices=[MODE_MANAGED, MODE_BASELINE], default=None,
                        help="上下文策略：省略则沿用会话记录（无记录则 managed）")
    p_chat.set_defaults(func=cmd_chat)

    p_list = sub.add_parser("list", help="列出所有任务（带编号）")
    p_list.set_defaults(func=cmd_list)

    p_delete = sub.add_parser("delete", help="删除会话及其全部本地数据（不可恢复）")
    p_delete.add_argument("task_id", help="任务编号或完整任务 id")
    p_delete.add_argument(
        "-f", "--force", action="store_true", help="跳过确认提示（脚本化使用）"
    )
    p_delete.set_defaults(func=cmd_delete)

    p_report = sub.add_parser("report", help="人机协同报告（机械渲染，零模型成本）")
    p_report.add_argument("task_id", help="任务 id 或列表编号")
    p_report.set_defaults(func=cmd_report)

    p_maint = sub.add_parser("maint", help="查看整理与分裂进度（机械渲染，零模型成本）")
    p_maint.add_argument("task_id", nargs="?", default="",
                         help="任务 id 或列表编号；省略取最近更新的会话")
    p_maint.set_defaults(func=cmd_maint)

    p_serve = sub.add_parser("serve", help="本地只读可视化（前端仪表盘，零模型成本）")
    p_serve.add_argument("--host", default="127.0.0.1",
                         help="绑定地址（默认 127.0.0.1，数据不对外网暴露）")
    p_serve.add_argument("--port", type=int, default=8600, help="端口（默认 8600）")
    p_serve.set_defaults(func=cmd_serve)

    p_views = sub.add_parser(
        "views", help="查看按域分化的上下文视图（机械渲染，零模型成本）"
    )
    p_views.add_argument("domain", nargs="?", default="",
                         help="域 id（如 A、A-1）或域名；省略则列全部域摘要")
    p_views.add_argument("task_id", nargs="?", default="",
                         help="任务 id 或列表编号；省略取最近更新的会话")
    p_views.set_defaults(func=cmd_views)

    sub.add_parser("help", help="显示帮助")

    args = parser.parse_args(argv)

    # 裸 `wovra` / `wovra help`：打印帮助而不是报错
    if args.command is None or args.command == "help":
        parser.print_help()
        return

    try:
        args.func(args)
    except LLMConfigError as error:
        # 配置错误用户可自行修复：一条人话 + 指向 .env 的检查项，
        # 不打 traceback（服务端原始信息已附在提示里）
        print(ui.error(str(error)))
        raise SystemExit(1) from None
