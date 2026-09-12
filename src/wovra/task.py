"""任务状态：Wovra 的核心数据结构（路线图阶段 2）。

一个 Task 是一个持久的工作空间，对应 README 里的那棵树：

    Task
    ├── Goal（目标）              要做成什么
    ├── Requirements（需求）      约束条件
    ├── Acceptance Criteria（验收标准）  怎样才算完成
    ├── Current State（当前状态）  status + 摘要报告
    └── History（历史）           发生过什么，只追加不修改

落盘格式刻意选择"人类可读的文件"而不是数据库：

    tasks/<task-id>/
    ├── task.json    结构化状态（给程序读）
    └── report.md    进度报告（给人看——人与 AI 的共享接口）

选择文件而不是 SQLite：阶段 2 的规模下文件完全够用，且人类
可以直接打开看、直接手改（手改后下次 load 就生效），这本身就是
一种最朴素的人工干预方式。等并发和规模成为真实问题时再换存储。

当前实现用一个 dataclass + dict 承载数据，没有引入 pydantic：
字段还很少，标准库足够；schema 复杂起来后再引入校验库也不迟。
"""

import contextlib
import itertools
import json
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from dataclasses import fields as dataclass_fields
from datetime import datetime
from pathlib import Path
from typing import Optional

from . import registry as registry_module
from . import tools as tools_module
from .tools import FAILURE_MARKERS

# 所有任务统一放在项目根目录的 tasks/ 下（本文件位于 src/wovra/）
TASKS_ROOT = Path(__file__).resolve().parent.parent.parent / "tasks"

# ---- 原子落盘（Windows 稳健性，2026-09-12）--------------------------------
#
# 为什么需要这一层：原实现是"固定 tmp 名 + os.replace"，在 Windows 上会撞车
# ——**同一个任务的保存不止一个线程在做**：主线程（账本工具、转交、轮闭合）
# 与后台整理线程会同时 save()，两边写同一个 `task.json.tmp`，或目标文件被
# 另一方短暂持有，os.replace 直接抛 PermissionError [WinError 5]，异常一路
# 穿透到 CLI，**把整个会话进程带走**（2026-09-12 实测崩掉正在进行的会话）。
# 另一类触发源是外部句柄：杀毒/搜索索引器/用户拿编辑器打开了 task.json 或
# report.md——这类占用是**短暂**的，退避重试即可过去。
#
# 故三层保护：①每次保存用**唯一** tmp 名（pid/线程/序号，跨进程也不会撞）；
# ②同一 task id 的保存按 id 串行（锁在模块级，跨 Agent 实例有效——调用方
# 散落在 ledger/cli/maintenance，逐个加锁改不动也容易漏）；③replace 遇
# PermissionError 退避重试，重试耗尽才上抛。
_SAVE_LOCKS: dict[str, threading.RLock] = {}
_SAVE_LOCKS_GUARD = threading.Lock()
_SAVE_TMP_SEQ = itertools.count(1)
_SAVE_ATTEMPTS = int(os.environ.get("WOVRA_SAVE_ATTEMPTS", "6"))
_SAVE_BACKOFF = float(os.environ.get("WOVRA_SAVE_BACKOFF", "0.05"))


def _save_lock(task_id: str) -> threading.RLock:
    """同一任务的落盘锁（模块级按 id 复用；RLock 以防 save 被重入）。"""
    with _SAVE_LOCKS_GUARD:
        lock = _SAVE_LOCKS.get(task_id)
        if lock is None:
            lock = threading.RLock()
            _SAVE_LOCKS[task_id] = lock
        return lock


def _replace_with_retry(src: Path, dst: Path) -> None:
    """os.replace + 退避重试（只重试 PermissionError：外部句柄是短暂的）。"""
    delay = _SAVE_BACKOFF
    last: Optional[Exception] = None
    for attempt in range(1, max(_SAVE_ATTEMPTS, 1) + 1):
        try:
            os.replace(src, dst)
            return
        except PermissionError as error:  # WinError 5：目标/源被别的句柄持有
            last = error
            if attempt < _SAVE_ATTEMPTS:
                time.sleep(delay)
                delay *= 2
    raise last if last is not None else PermissionError("replace 失败")


def _write_atomic(path: Path, text: str) -> None:
    """原子写入：唯一 tmp → fsync → replace（失败清理半截文件）。"""
    tmp = path.with_name(
        f"{path.name}.tmp.{os.getpid()}.{threading.get_ident()}.{next(_SAVE_TMP_SEQ)}"
    )
    try:
        with open(tmp, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        _replace_with_retry(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def _cleanup_stale_tmp(directory: Path, keep_younger_than: float = 600.0) -> None:
    """清掉历史崩溃留下的半截 tmp（best-effort，只动**够旧**的）。

    年龄下限是必须的：另一个线程/进程可能正在写它自己的唯一 tmp，删了就把
    对方的保存打坏——只清超过 10 分钟没人动的。
    """
    now = time.time()
    for path in directory.glob("*.tmp.*"):
        try:
            if now - path.stat().st_mtime > keep_younger_than:
                path.unlink()
        except OSError:
            pass


# TaskState 每类列表的容量上限：超出淘汰最旧。
# 被淘汰的内容仍在 History（Round/Event）里，可通过 expand_history 找回——
# 状态是"当前是什么"，历史是"过去发生了什么"，淘汰只影响前者。
# 容量按亿级使用规模设计（V2 定稿），不再使用测试期的小数值
STATE_LIST_CAP = 200

# state_patch 里允许的列表类字段与 TaskState 字段的对应关系
_PATCH_LIST_FIELDS = (
    "constraints", "decisions", "completed", "known_issues",
    "open_questions", "escalations", "experiments",
)

# 模型侧注入的节（2026-09-11 用户拍板：账本适配分裂的第一步）。
#
# `completed` **退出模型侧**，理由三条实测依据：
# 1. 它是唯一无限增长的节——活会话实测 49 条 / 19,582 字符 / 14,005 tok，
#    占全部条目文本的 62.7%，而其内容几乎全是 `[阶段] …（验收：…）`，
#    即 verify_stage 自动写入的**验收证据副本**，与 worklog、
#    report.md、events 流三重冗余；
# 2. R16 的按节预算已把它压到 143 tok——占着 62.7% 的存储，模型实际
#    只看到一句片段，等于纯噪声；
# 3. 档案的读取时刻是"人想知道"，不是"每轮都要"（人视图 `wovra report`
#    与 task.py 的报告渲染仍全量输出，本节不删数据、只不注入模型）。
#
# 留下的是"边界与待办"：目标/现状（render 的 head）+ 决策升级与待办实验
# （**没有第二个注入点**，分裂后更是子 agent 唯一的升级通道）+ 其余台账。
# decisions/known_issues/open_questions 暂留，待"装配按域分化"时按
# file_domains 分片到各域视图（一并动装配，避免对同一段代码改两次）。
_MODEL_SIDE_SECTIONS = (
    "decisions", "known_issues", "open_questions",
    "escalations", "experiments", "constraints",
)

# 结案机制（2026-09-11 用户拍板）。状态账本此前**只增不减**——全链只有
# 追加去重，没有任何删除/结案路径。实测代价：活会话 11 条 escalations 里
# 8 条已结案仍挂着（400 已修并推送、幽灵误判已按 A 修完、v1/v3 双轨已废、
# 分裂去留已拍板、旧定律清理已拍板、org 收窄已复议两次…），模型每轮都在
# 读它们，随时可能把已经做完的事重新拿来问一遍——**"现状账本"在缺结案
# 机制时会退化成"没清理的收件箱"，化石会直接污染行动判断**。
#
# 分工纪律（next-stage-intent.md §2「排除性判断不做，累积性整理可以做」）：
# **语义判断归模型、机械匹配归机制**。模型在整理时（读取时刻）给出
# "哪条已结案 + 用于识别的片段"，机制只做子串**唯一匹配**与移除；
# 找不到或匹配到多条一律**不猜**（原样保留并把片段回报出去）。机制永不
# 自行判断"这条看起来过期了"——那才是被禁的排除性判断。结案是删除操作，
# 必须留痕可见（apply_state_patch 写 maintenance history）。

# TaskState.render 的裁剪策略（2026-09-11 P0 修复）。
# 旧实现是对整块文本做 `text[:budget]` 切片，而节的输出顺序是
# 目标→现状→已完成→已决策→…→决策升级→待办实验→约束，切片保头部，
# 于是活会话实测"只剩已完成一节"：决策升级与待办实验（文档写明的人机
# 协同一等公民，且**没有第二个注入点**）被整节吃掉——需要人拍板的事
# 模型永远读不到；而且"保最旧"的取舍方向与 apply_patch 的"淘汰最旧"
# 正好相反，两头口径打架。
# 新策略：**按节分配**，每节在配额内保留**最新**条目，被裁的条数显式
# 写进标签（「前 N 条已省略，见 report」），并给整块加一条总注——
# 关键节优先取配额，其余节分剩余的，任何情况下都不静默消失一整节。
_STATE_SECTION_ORDER = (
    ("已完成", "completed"),
    ("已决策", "decisions"),
    ("已知问题", "known_issues"),
    ("待解决问题", "open_questions"),
    ("决策升级", "escalations"),
    ("待办实验", "experiments"),
    ("约束", "constraints"),
)

# 配额优先级：关键节（决策升级/待办实验/约束）先取，其余按
# 已决策 > 已知问题 > 待解决问题 > 已完成。已完成的档案价值最低——
# 原文在 history 与 report.md 里，且它是唯一会无限增长的节（实测
# 活会话单它一节就 9,356 字符，超过 8,000 的整块预算）。
_STATE_SECTION_PRIORITY = (
    "escalations", "experiments", "constraints",
    "decisions", "known_issues", "open_questions", "completed",
)

# 有节被裁剪时追加的总注（可 grep 的锚点）
_STATE_TRIM_NOTE = "(任务状态按预算裁剪，省略条目见 report)"


def _section_need(label: str, items: list[str]) -> int:
    """一节完整渲染所需的字符数（用于配额分配）。"""
    return len(label) + 1 + len("；".join(items))


def _fit_section(label: str, items: list[str], share: int) -> tuple[str, int]:
    """在 share 字符内保留尽量多的**最新**条目（与 apply_patch 淘汰最旧同向）。

    至少保留最新的一条：宁可略微超配额，也不让一节整节消失——旧实现
    正是"整节消失"。返回 (渲染文本, 被省略条数)；省略条数写进标签，
    与 report.md / history 对得上。
    """
    kept: list[str] = []
    used = len(label) + 1  # 全角冒号
    for item in reversed(items):
        cost = len(item) + (1 if kept else 0)  # 分隔用全角分号
        if kept and used + cost > share:
            break
        kept.append(item)
        used += cost
    kept.reverse()
    omitted = len(items) - len(kept)
    if not kept:
        return f"{label}（{omitted} 条已省略，见 report）", omitted
    if omitted:
        return (
            f"{label}（前 {omitted} 条已省略，见 report）：" + "；".join(kept),
            omitted,
        )
    return f"{label}：" + "；".join(kept), 0


def sanitize_surrogates(text: str) -> str:
    """把字符串里的未配对代理项替换为 U+FFFD（�），其余字节不变。

    模型的工具参数偶发把 emoji 拆成不成对的 \\uD83D 转义，json 解析后
    成为无法 UTF-8 编码的代理字符——不清洗会让落盘（json.dumps →
    write_text）和目标文件写入当场崩掉（实测 2026-09-05）。
    """
    return text.encode("utf-8", errors="surrogatepass").decode("utf-8", errors="replace")


def find_children(parent_id: str) -> list[dict]:
    """扫描任务目录，返回 parent_id 匹配的子任务摘要（id/status/goal）。

    组织运行时的派发板、report 命令、\\sub 页面共用这一个扫描。
    """
    children: list[dict] = []
    if not TASKS_ROOT.exists():
        return children
    for directory in sorted(TASKS_ROOT.iterdir()):
        path = directory / "task.json"
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if data.get("parent_id") == parent_id:
            children.append({
                "id": data.get("id", directory.name),
                "status": data.get("status", ""),
                "goal": data.get("goal", ""),
            })
    return children


@dataclass
class TaskState:
    """任务当前状态的增量可变视图（History 之外的另一本账）。

    History 回答"过去发生了什么"；TaskState 回答"现在是什么状态"。
    它通过 Round Organization 产出的 patch 增量更新，从不整体重写；
    设有大小限制，防止它自己变成新的无限上下文。
    """

    goal: str = ""
    constraints: list[str] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)
    completed: list[str] = field(default_factory=list)
    known_issues: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    # 组织运行时（organization-runtime-v1.md）：
    # escalations = 决策升级（实测与预期不符、影响方向、需人拍板的事项）
    # experiments = 待办实验（机器无法闭环验证、需要人当传感器的事项）
    escalations: list[str] = field(default_factory=list)
    experiments: list[str] = field(default_factory=list)
    current_status: str = ""
    is_done: bool = False

    def close_items(self, field: str, snippets: list[str]) -> tuple[list[str], list[str], list[str]]:
        """按片段从某个列表字段里结案（移除）条目。

        返回 `(存活条目, 已结案条目文本, 未匹配片段)`。机械匹配纪律：
        **子串命中且唯一**才移除；命中 0 条或多条一律不动、原样列出片段，
        交由调用方留痕——宁可留着化石，不可误删活账（错删比滞留危险：
        滞留只是噪，误删会丢约束）。
        """
        items = list(getattr(self, field) or [])
        closed: list[str] = []
        unmatched: list[str] = []
        for raw in snippets:
            snippet = str(raw).strip()
            if not snippet:
                continue
            hits = [x for x in items if snippet in x]
            if len(hits) != 1:
                unmatched.append(snippet)
                continue
            items.remove(hits[0])
            closed.append(hits[0])
        setattr(self, field, items)
        return items, closed, unmatched

    def apply_patch(self, patch: dict) -> dict:
        """应用一轮 Organization 产出的状态补丁。

        * 列表字段：追加去重，超出容量淘汰最旧
        * current_status / goal：直接覆盖
        * is_done：仅接受布尔
        * closed：结案清单（模型给的片段，机制唯一匹配后移除，见常量注释）
        * 非法/缺失字段一律忽略，不让坏数据进状态

        返回结案报告 `{"closed": [(field, text)], "unmatched": [(field, snippet)]}`，
        供调用方留痕——**结案是删除操作，必须可见**。
        """
        if patch.get("goal"):
            self.goal = str(patch["goal"])
        if isinstance(patch.get("is_done"), bool):
            self.is_done = patch["is_done"]
        if patch.get("current_status"):
            self.current_status = str(patch["current_status"])
        for name in _PATCH_LIST_FIELDS:
            items = patch.get(name)
            if not isinstance(items, list):
                continue
            merged = getattr(self, name)
            for item in items:
                text = str(item).strip()
                if text and text not in merged:
                    merged.append(text)
            del merged[: max(0, len(merged) - STATE_LIST_CAP)]

        report: dict = {"closed": [], "unmatched": []}
        for entry in patch.get("closed") or []:
            if not isinstance(entry, dict):
                continue
            field = str(entry.get("field") or "").strip()
            snippet = entry.get("match")
            if field not in _PATCH_LIST_FIELDS or not snippet:
                continue
            # 先追加再结案：同一批里"新增即结案"（历史里已解决的问题
            # 被重复提到）也应当能结掉，否则化石要等下一批
            _items, closed, unmatched = self.close_items(field, [str(snippet)])
            if closed:
                report["closed"].append((field, closed[0]))
            elif unmatched:
                report["unmatched"].append((field, unmatched[0]))
        return report

    def render(self, budget: int | None = None, sections: tuple[str, ...] | None = None) -> str:
        """渲染成给模型看的文本块。

        空状态返回空串——新会话不给模型一个空的任务状态头
        （否则模型会困惑"任务状态是空的"）。

        budget=None：全量渲染（人读视图、report、测试用）。
        budget=N：**按节分配**配额（见 _STATE_SECTION_PRIORITY 的注释）——
        关键节优先取满、每节在配额内保最新条目、被裁条数显式标注。绝不
        再出现旧实现那种"整块切片把整节吃掉"（实测只剩「已完成」一节，
        决策升级与待办实验全部不可见）。为保证每节至少留最新一条，极端
        情况下总长可能略超 budget——宁可略超，不可静默丢节。

        sections：只渲染指定的节（模型侧注入用 `_MODEL_SIDE_SECTIONS`——
        `completed` 是唯一无限增长且与人视图/report/events 三重冗余的节，
        退出模型侧；**人视图与 report 不传此参，照旧全量**）。
        """
        head: list[str] = []
        if self.goal:
            head.append(f"目标：{self.goal}")
        if self.current_status:
            head.append(f"当前状态：{self.current_status}")
        if self.is_done:
            head.append("任务已完成。")
        sections = [
            (label, name, getattr(self, name))
            for label, name in _STATE_SECTION_ORDER
            if getattr(self, name) and (sections is None or name in sections)
        ]
        if not head and not sections:
            return ""
        if budget is None:
            lines = head + [
                f"{label}：" + "；".join(items) for label, _name, items in sections
            ]
            return "\n".join(["[任务状态]"] + lines)

        # 按节分配：头部固定开销先扣，再给裁剪提示留一行
        fixed = len("[任务状态]") + 1 + sum(len(x) + 1 for x in head)
        quota = max(0, budget - fixed - len(_STATE_TRIM_NOTE) - 1)
        share: dict[str, int] = {}
        left = quota
        by_name = {name: (label, items) for label, name, items in sections}
        for name in _STATE_SECTION_PRIORITY:
            if name not in by_name:
                continue
            label, items = by_name[name]
            take = min(_section_need(label, items), left)
            share[name] = take
            left -= take

        trimmed = False
        lines = list(head)
        for label, name, items in sections:
            text, omitted = _fit_section(label, items, share.get(name, 0))
            trimmed = trimmed or omitted > 0
            lines.append(text)
        if trimmed:
            lines.append(_STATE_TRIM_NOTE)
        return "\n".join(["[任务状态]"] + lines)


# history 事件的 kind → 报告里显示的中文标签
_KIND_LABELS = {
    "user_input": "用户输入",
    "tool_call": "调用工具",
    "tool_result": "工具结果",
    "final_answer": "最终回答",
    "task_context_loaded": "加载上下文",
    "file_change": "文件变更",
    "usage": "用量",
}


def _one_line(text: str, limit: int = 120) -> str:
    """把任意多行文本压成一行摘要（换行折叠为空格、超长截断）。

    报告里的每个事件必须恰好占一行——否则工具返回的文件内容、
    多行回答会把 Markdown 列表结构冲垮，这正是报告"看不懂"的根源。
    """
    collapsed = " ".join(text.split())
    return collapsed if len(collapsed) <= limit else collapsed[:limit] + "…"


@dataclass
class Task:
    """一个长时运行任务的全部持久状态。

    status 取值约定（阶段 2 只需要这几个粗粒度值）：
        in_progress -- 正在进行
        blocked     -- 被阻塞（等待人类输入或外部资源）
        done        -- 已完成
        merged      -- 子任务已被父任务清算合并（历史原样保留，解散≠删除）
    """

    id: str
    goal: str
    # ⚠ V1.2 遗迹·标废不删（2026-09-11 遗产整治，方案 A）：
    # requirements 无生产者、无模型侧注入，实测 15/77 个历史会话有真实数据
    # （读得到才保得住现场），故保留字段与渲染，随 Level 1/2 落地一并处置。
    requirements: list[str] = field(default_factory=list)
    status: str = "in_progress"
    # ⚠ V1.2 遗迹·标废不删（同上）：summary 是 V1 时代 Agent 周期性写入的
    # 状态摘要（`set_summary()` 已随本次整治删除，实测 0 处消费者、0/77 数据），
    # 字段与渲染保留仅为兼容历史 task.json 与手改；现行机制下无生产者。
    summary: str = ""
    # history 只追加：每条是 {"time", "kind", "detail"}，
    # 追加式历史让"发生过什么"永远可追溯，这是可恢复性的基础
    history: list[dict] = field(default_factory=list)
    # V1 Context Runtime：Round/Event 结构化历史与任务状态（见 agent/ 包）
    rounds: list[dict] = field(default_factory=list)
    task_state: dict = field(default_factory=dict)
    # baseline 记账：累计输入 token（用于 80% 阈值压缩触发）与压缩摘要
    baseline_prompt_used: int = 0
    baseline_summary: str = ""
    # 会话绑定的工作区（创建时的 PROJECT_ROOT）。恢复会话时以此为准——
    # 无论从哪个目录启动 wovra，都回到该会话原本的文件世界
    workspace: str = ""
    # 安全模式（2026-09-12）："approve" = 一切敏感操作先问人（默认）；
    # "auto" = 自主运行——确认闸门全部放行（适合无人值守长跑）
    safety_mode: str = "approve"
    # 会话级"同意以后同类命令"白名单（审批三选项的记忆）：存标签，
    # 如 "cmd:<命中模式>" 或 "del:<路径>"
    approved_tags: list[str] = field(default_factory=list)
    # 会话的上下文模式（managed/baseline）：恢复时沿用，防止实验数据串味
    mode: str = ""
    # ⚠ V1.2 遗迹·标废不删（2026-09-11 遗产整治，方案 A）：
    # 三件套来自组织运行时（organization-runtime-v1.md），而多 agent 编排已由
    # 用户 09-07 拍板废除（提交 af40bc1 整体退场），现行机制下**都没有生产者**：
    #   parent_id          —— 父任务 id（子任务扫描/报告仍读得到）
    #   org_context        —— 逐字下传的意图快照（仅定义处，1 处引用）
    #   pending_instruction—— 父任务派发的决策回传通道（cli/main.py 仍读并清空）
    # 实测 15/77 个历史会话有真实数据，删字段会丢现场，故保留并随 Level 1/2
    # 落地一并处置（Level 1 若要派发，parent_id 可能复用）。处置建议见
    # worklog §20.5 与 §21。
    parent_id: str = ""
    org_context: str = ""
    pending_instruction: str = ""
    todo: dict = field(default_factory=dict)  # 阶段/工作项计划账本（深度恒 1）
    # agent 注册表（机制三）：路径 ID / 类别描述 / 所有权文件域 / 状态 /
    # 收件箱（单向通信的落信处）+ per-agent 运行时账（轮次/步数/上下文体量/
    # 窗口，2026-09-12 3a）。默认只有主 agent；分裂执行时扩充
    registry: list[dict] = field(default_factory=list)
    # ID 体系版本（2026-09-12）：1 = 旧（主 agent 占 `A`，顶层域 `A-1`…），
    # 2 = 新（主 agent = `Main`，顶层域 `A`、`B`、`C`…）。缺省按 1 读，
    # 加载期迁移一次后落 2（迁移**非幂等**，故必须靠这个标记把住）。
    agent_id_scheme: int = 1
    # 显式转交的落点（2026-09-12，Level 1 第三步路由）：`switch_view` 工具
    # 与接活视图的 notify 都写这里，路由时作为最高优先判定，消费后清空。
    pending_view: str = ""
    created_at: str = ""
    updated_at: str = ""

    def apply_state_patch(self, patch: dict) -> dict:
        """把 Organization 的 state_patch 合并进任务状态（持久化字段）。

        返回结案报告（`{"closed": [...], "unmatched": [...]}`）供调用方
        留痕——结案是删除操作，必须可见（见 _MODEL_SIDE_SECTIONS 上方注释）。
        """
        state = TaskState(**(self.task_state or {}))
        report = state.apply_patch(patch)
        self.task_state = asdict(state)
        # goal 同步（2026-09-11 用户拍板，遗产整治）：`Task.goal` 是**人视图**
        # 显示的目标（`wovra list`、report 的 head、启动横幅都读它），
        # `TaskState.goal` 是整理产出的**权威**当前目标。此前两者各自为政，
        # 实测活会话 `Task.goal` 为空而 `TaskState.goal` 写满了整段真实目标，
        # 于是终端与报告一直显示"（目标待明确）"、模型侧却看得到完整目标。
        # goal 是文档写明的"最慢层"——两本账必须指同一个：只要 state 里有
        # goal 就镜像过来（不只是本批带了 goal 时，故历史遗留也能自愈）。
        if state.goal:
            self.goal = state.goal
        # is_done 与粗粒度状态机打通：整理判定完成 → status=done，
        # 避免 report 里"任务已完成"与 list 里"进行中"两本账打架
        if patch.get("is_done") is True and self.status != "done":
            self.status = "done"
            self.updated_at = datetime.now().isoformat(timespec="seconds")
        return report

    def get_state(self) -> TaskState:
        """以 TaskState 对象的形式读取当前任务状态。"""
        state = TaskState()
        state.__dict__.update(self.task_state or {})
        return state

    def todo_lines(self) -> list[str]:
        """机械渲染当前阶段 / 工作项计划账本（人视图共用，零 LLM）。

        词表（2026-09-12 用户口径，worklog §53）：**阶段**（Stage，M{n}）= 可
        验收的推进增量；**工作项**（Item）= 阶段内的拆解。旧名"大步/小步"带
        "步"字，与执行账里的"步数"撞车，故只改看得见的词（落盘键不变）。
        这份账本平时只进模型上下文，人看不到；这里把它变成可读文本。
        """
        todo = self.todo or {}
        milestone = todo.get("milestone")
        steps = todo.get("steps") or []
        history = todo.get("history") or []
        if not milestone:
            lines = ["-（无进行中的阶段）"]
            if history:
                # 历史 goal 可能是整段阶段描述，人视图只留一句摘要
                last = history[-1]
                label = f"{last.get('id')} " if last.get("id") else ""
                lines.append(
                    f"- 已验收 {len(history)} 个阶段"
                    f"（最近：{label}{_one_line(str(last.get('goal', '')), 50)}）"
                )
            return lines
        done_n = sum(1 for s in steps if s.get("done"))
        detail = []
        if milestone.get("started_seq") is not None:
            detail.append(f"自 R{milestone['started_seq']}")
        if steps or milestone.get("planned"):
            detail.append(f"工作项 {done_n}/{len(steps)}")
        stage_id = f"{milestone.get('id')} " if milestone.get("id") else ""
        lines = [
            f"- 阶段 {stage_id}{_one_line(str(milestone.get('goal', '')), 80)}"
            + (f"（{'；'.join(detail)}）" if detail else "")
        ]
        if not milestone.get("planned"):
            lines.append("  - 尚未拆工作项——先 add_item 拆出阶段内的工作项")
        acceptance = milestone.get("acceptance") or []
        if acceptance:
            lines.append(
                "- 验收标准：" + "；".join(_one_line(str(a), 60) for a in acceptance)
            )
        for s in steps:
            mark = "x" if s.get("done") else " "
            lines.append(f"  - [{mark}] {_one_line(str(s.get('text', '')), 80)}")
        for d in milestone.get("deferred") or []:
            lines.append(f"  - [待人工验收] {_one_line(str(d), 60)}")
        if history:
            lines.append(f"- 已验收 {len(history)} 个阶段")
        return lines

    def todo_summary_line(self) -> str:
        """当前阶段 / 工作项的单行摘要（终端底栏用，无颜色）。

        bottom_toolbar 不解析裸 ANSI，所以这里只给纯文本；着色与否
        由调用方（终端）决定。
        """
        todo = self.todo or {}
        milestone = todo.get("milestone")
        history = todo.get("history") or []
        if not milestone:
            line = "无进行中的阶段"
            if history:
                line += f" · 已验收 {len(history)} 个"
            return line
        steps = todo.get("steps") or []
        stage_id = f"{milestone.get('id')} " if milestone.get("id") else ""
        line = f"阶段 {stage_id}{_one_line(str(milestone.get('goal', '')), 40)}"
        if milestone.get("planned") or steps:
            done_n = sum(1 for s in steps if s.get("done"))
            line += f" · 工作项 {done_n}/{len(steps)}"
        else:
            line += " · 未拆工作项"
        deferred = milestone.get("deferred") or []
        if deferred:
            line += f" · 待验收 {len(deferred)}"
        return line

    # ---- 构造与加载 -----------------------------------------------------

    @classmethod
    def create(
        cls,
        goal: str,
        requirements: list[str] | None = None,
    ) -> "Task":
        """新建一个任务。id 用日期 + 短随机串，保证可读又不冲突。

        `acceptance_criteria` 参数已删除（2026-09-11 遗产整治）：实测
        0/77 会话有数据、src 内 0 处消费者，唯一使用者是 experiments/
        脚本（改为把验收清单写进自己的 meta.json）。现行验收口径是
        阶段的 `acceptance` 字段（见 `todo` 账本与 todo-milestone-tool.md）。
        """
        now = datetime.now()
        task_id = now.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        return cls(
            id=task_id,
            goal=goal,
            requirements=list(requirements or []),
            workspace=str(tools_module.safety.PROJECT_ROOT),
            # 注册表默认只有主 agent；分裂执行时扩充（机制三/四）。
            # ID 体系 v2（2026-09-12 用户拍板）：主 agent = `Main`，第一次
            # 分裂的顶层域取 `A`、`B`、`C`…，A 满了才在 A 内裂 `A-1`。
            registry=[{
                "id": registry_module.MAIN_AGENT_ID, "name": "主agent",
                "description": "全局协调与未归属事务",
                "file_domains": [], "status": "active", "inbox": [],
                # 只留**观测**字段（2026-09-12 用户拍板：账本派生、不落盘）：
                # 轮次/步数/承载由 `views.agent_ledger` 现场算，不在这里存。
                "ctx_cur": 0, "ctx_peak": 0, "window": 0,
            }],
            agent_id_scheme=registry_module.AGENT_ID_SCHEME,
            created_at=now.isoformat(timespec="seconds"),
            updated_at=now.isoformat(timespec="seconds"),
        )

    @classmethod
    def load(cls, task_id: str) -> "Task":
        """从磁盘加载任务。task.json 是唯一的事实来源（source of truth）。

        会话绑定的工作区随加载恢复：无论从哪个目录启动 wovra，
        该会话的文件世界都回到它创建时的位置。

        加载期自愈（2026-09-11）：历史会话落盘的 `blocks` 可能是旧的 v1
        粗分块（`close_round` 曾落 v1，而整理/视图/展开一律按 v3 取块；
        两者编号空间相同而切法不同，任何按 ID 查表处都会静默错位）。加载
        时按事件流重算为 v3 并落盘——迁移是**确定性、幂等**的（同一份事件
        流重算结果恒定），故可无风险随加载进行；活跃会话也因此不必手动
        迁移（旧进程会用内存里的旧数据覆盖回去，只有重启后的新进程能治）。
        """
        from . import registry as registry_module
        from . import tools as tools_module
        from .blocks import migrate as migrate_module

        path = TASKS_ROOT / task_id / "task.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        # 已删字段的兼容过滤（2026-09-11 遗产整治）：`cls(**data)` 遇到未知键
        # **直接抛 TypeError**，而字段一旦从 dataclass 里删掉，历史 task.json 里
        # 的旧键就全成了未知键——实测 77/77 个会话都带 `acceptance_criteria`
        # （值为 `[]`，故"非空计数"口径看不出它），不过滤则所有历史会话加载即崩。
        # 这里统一丢弃任何不在当前 dataclass 字段里的键（不止本次删的那一个），
        # 往后删字段/改字段都不必再回来改加载口。
        known_fields = {f.name for f in dataclass_fields(cls)}
        dropped = sorted(set(data) - known_fields)
        for key in dropped:
            data.pop(key, None)
        task = cls(**data)
        if dropped:
            task.record(
                "maintenance",
                "load：丢弃已删字段的遗留键（"
                + "、".join(dropped)
                + "）——字段已从 Task 删除，加载期兼容过滤",
            )
            task.save()
        # Agent ID 体系迁移（2026-09-12 用户拍板：主 agent = `Main`，顶层域
        # `A`、`B`、`C`…——旧体系把主 agent 占成 `A`，顶层域只能是 `A-1`，
        # 与 `docs/思考内容与AI对话.md` §五（A=大类、A-1=子类）差一级）。
        # 走加载期自愈（与下方块迁移/回填同一模式），但**必须先看版本标记**：
        # 迁移非幂等（新体系里 `A-1` = "域 A 的子域 1"），重复迁移会把子域
        # 读成顶层域。故只在 `agent_id_scheme < 2` 时做一次，做完落 2。
        if int(getattr(task, "agent_id_scheme", 1) or 1) < registry_module.AGENT_ID_SCHEME:
            moved, samples = registry_module.migrate_agent_ids(
                task.registry, task.rounds
            )
            if str(getattr(task, "pending_view", "") or "") == (
                registry_module.LEGACY_MAIN_AGENT_ID
            ):
                task.pending_view = registry_module.MAIN_AGENT_ID
                moved += 1
            task.agent_id_scheme = registry_module.AGENT_ID_SCHEME
            task.record(
                "maintenance",
                f"agent ID 体系迁移：v1 → v2（主 agent `{registry_module.LEGACY_MAIN_AGENT_ID}` "
                f"→ `{registry_module.MAIN_AGENT_ID}`，顶层域改用 A/B/C…），"
                f"改动 {moved} 处"
                + (f"（{'、'.join(samples)}）" if samples else ""),
            )
            task.save()
        # 注册表回填（2026-09-11，Level 1 视图分化第一步的补网）：注册表
        # 落实是本日才接上的下游，此前已 promote 的分裂产物不会再触发
        # promote——不补则历史会话的注册表永远只有主 agent。幂等，故可
        # 随加载进行（与下方块迁移同一模式）。
        reg_added, reg_updated = registry_module.backfill(
            task.registry, task.rounds
        )
        if reg_added or reg_updated:
            task.record(
                "maintenance",
                "registry：加载期回填分裂产物——"
                + "，".join(
                    s for s in (
                        f"新增 {len(reg_added)}（{'、'.join(reg_added)}）"
                        if reg_added else "",
                        f"更新 {len(reg_updated)}（{'、'.join(reg_updated)}）"
                        if reg_updated else "",
                    ) if s
                ),
            )
            task.save()
        changed, report = migrate_module.migrate_rounds(task.rounds)
        if changed:
            seqs = "、".join(f"R{seq}" for seq, _b, _a in report[:12])
            task.record(
                "maintenance",
                f"历史块结构迁移：{changed} 轮由 v1 粗分块重算为 v3 按文件聚合"
                f"（{seqs}{'…' if changed > 12 else ''}）",
            )
            task.save()
        # goal 回填（2026-09-11，遗产整治）：Task.goal 与 TaskState.goal 长期
        # 分裂（前者是 V1 时代"建任务时定死"的字段，后者是整理产出的权威目标），
        # 实测活会话前者为空、后者有值，人视图因此一直显示"（目标待明确）"。
        # 与上方两处同一模式：幂等，故可随加载进行；不再整理的旧会话也能治好。
        if not task.goal and (task.task_state or {}).get("goal"):
            task.goal = task.task_state["goal"]
            task.record(
                "maintenance",
                "state：Task.goal 与 TaskState.goal 分裂，已按后者回填"
                "（人视图此前显示“目标待明确”）",
            )
            task.save()
        if task.workspace:
            workspace = Path(task.workspace)
            if workspace.is_dir():
                tools_module.safety.PROJECT_ROOT = workspace
        return task

    @classmethod
    def load_or_create(
        cls,
        task_id: str,
        goal: str,
        requirements: list[str] | None = None,
    ) -> "Task":
        """已存在则加载（断点续做），否则新建。演示"停止-恢复"的入口。"""
        if (TASKS_ROOT / task_id / "task.json").exists():
            return cls.load(task_id)
        task = cls.create(goal, requirements)
        task.id = task_id  # 固定 id，让第二次运行能找到同一个任务
        return task

    # ---- 状态更新 -------------------------------------------------------

    def record(self, kind: str, detail: str) -> None:
        """向历史追加一条事件，并刷新更新时间。"""
        self.history.append(
            {
                "time": datetime.now().isoformat(timespec="seconds"),
                "kind": kind,      # 如 user_input / tool_call / tool_result / final_answer
                "detail": detail,
            }
        )
        self.updated_at = datetime.now().isoformat(timespec="seconds")

    # 已删除（2026-09-11 遗产整治，方案 A，用户拍板）：
    #   set_summary(text)  —— 实测 0 处消费者、0/77 会话有数据（V1 时代
    #       Agent "周期性写状态摘要"的入口；现行机制下状态由整理产出的
    #       state_patch 与 Round/Event 历史承担，摘要是多余的第三本账）
    #   apply_state(goal/status/summary) —— 0 处消费者、被 apply_state_patch
    #       取代（后者还能回传结案报告，见其 docstring）
    # 两者都是"写时固化判断"的 V1 残留，删除不影响任何行为。summary 字段
    # 本身保留（兼容历史 task.json 与手改），只是不再有生产入口。

    # ---- 持久化 ---------------------------------------------------------

    def save(self) -> None:
        """把当前状态写到磁盘：task.json + report.md。

        原子性（2026-09-05 教训）：write_text 先清空再写，写一半崩掉会把
        整个会话历史截断成空文件——故走 tmp + replace。

        并发与 Windows 稳健性（2026-09-12 修复）：主线程与后台整理线程会
        同时保存，原实现（固定 tmp 名）在 Windows 上直接 PermissionError
        [WinError 5] 崩掉整个会话。现在：唯一 tmp 名 + 按 task id 串行 +
        退避重试，详见模块顶部 `_SAVE_LOCKS` 一段的注释。

        失败策略**不对称**，这是刻意的：
        * `task.json` 是数据本身——写不进去必须上抛（宁可让调用方看见失败，
          也不能假装保存成功）；
        * `report.md` 是**派生的人视图**（每次保存都整体重写）——它被外部
          查看器/编辑器占着时不该拖垮正在进行的会话，故失败只记一笔账，
          下一次保存会把它重写回来（信息没有丢，只是这一版晚了点）。
        """
        directory = TASKS_ROOT / self.id
        directory.mkdir(parents=True, exist_ok=True)
        # 兜底清洗：任何环节漏掉代理字符，落盘前最后一道闸
        data = sanitize_surrogates(
            json.dumps(asdict(self), ensure_ascii=False, indent=2)
        )
        report = sanitize_surrogates(self._render_report())
        with _save_lock(self.id):
            _write_atomic(directory / "task.json", data)
            try:
                _write_atomic(directory / "report.md", report)
            except OSError as error:
                self.record(
                    "maintenance",
                    f"report.md 写入失败（{error}）——人视图是派生物，"
                    "下一次保存会重写；task.json 已正常落盘",
                )
            _cleanup_stale_tmp(directory)

    # ---- 给模型和报告用的视图 --------------------------------------------

    def context(self, last_n: int = 10) -> str:
        """生成给模型看的任务上下文（作为 system prompt 的一部分）。

        ⚠ 遗产·待处置（2026-09-11 遗产整治残留）：src/ 内**已无消费者**——
        现行装配由 `cli/prompt.py::_system_prompt` 与 agent/assembly.py 承担，
        本方法只剩 tests 在用。保住不删是怕手改/外部脚本还在调；下一步
        （Level 1 装配按域分化）一并裁定去留。

        V1 原文：这里体现"上下文生命周期"的第一步：模型不需要整个 history，
        只需要目标、约束、当前摘要和最近几条事件就能继续工作。
        目标可能尚未成形（新会话）——明确告诉模型这一点，
        它的角色是"在对话中逐步澄清目标"，而不是硬套一个不存在的目标。
        """
        lines = ["# 当前任务"]
        if self.goal:
            lines.append(f"\n目标：{self.goal}")
        else:
            lines.append(
                "\n目标：尚未明确。请在对话中逐步理解用户想做什么，"
                "目标会随交流自动更新，不必追问或强行定义。"
            )
        if self.requirements:
            lines.append("\n## 需求\n" + "\n".join(f"- {r}" for r in self.requirements))
        lines.append(f"\n状态：{self.status}")
        if self.summary:
            lines.append("\n## 之前的进展摘要\n" + self.summary)
        if self.history:
            lines.append("\n## 最近发生\n" + "\n".join(
                f"- [{e['time']}] {e['kind']}: {e['detail']}"
                for e in self.history[-last_n:]
            ))
        return "\n".join(lines)

    def _render_report(self) -> str:
        """渲染人类可读的 report.md。

        原则：report.md 是给人"一眼看懂"的，history 里每个事件
        压成一行摘要；完整内容（工具返回的原文、多行回答）在
        task.json 里，需要追溯时再去查。
        """
        lines = [
            f"# 任务报告：{self.id}",
            "",
            f"- **目标**：{self.goal}",
            f"- **状态**：{self.status}",
            f"- **创建时间**：{self.created_at}",
            f"- **更新时间**：{self.updated_at}",
        ]
        if self.requirements:
            lines.append("\n## 需求\n")
            lines += [f"- {r}" for r in self.requirements]
        lines.append("\n## 计划（阶段 / 工作项）\n")
        lines += self.todo_lines()
        lines.append("\n## 当前进展（AI 维护）\n")
        lines.append(self.summary or "_尚无进展摘要。_")
        if self.history:
            lines.append("\n## 时间线\n")
            # 全量渲染：每条事件已降噪为单行，没有截断的理由——
            # 报告砍掉头部活动会让人"没头没尾"。超长原文仍在 task.json
            lines += self._render_timeline(self.history)
            lines.append(
                "\n> 各事件为单行摘要；未截断的工具返回原文见同目录 task.json。"
            )
        return "\n".join(lines) + "\n"

    def _render_timeline(self, events: list[dict]) -> list[str]:
        """把事件列表渲染成人类可读的时间线。

        两个降噪规则：
        1. 相邻的 tool_call + tool_result 合并成一行——正常人关心的是
           "调了什么、成功没有"，而不是工具返回的内容原文；
        2. 其余每个事件压成一行（_one_line），避免多行内容冲垮列表。
        """
        merged: list[str] = []
        i = 0
        while i < len(events):
            event = events[i]
            time = event["time"]
            label = _KIND_LABELS.get(event["kind"], event["kind"])
            detail = _one_line(event["detail"])

            if (
                event["kind"] == "tool_call"
                and i + 1 < len(events)
                and events[i + 1]["kind"] == "tool_result"
            ):
                result_event = events[i + 1]
                # tool_result 的 detail 格式为 "工具名 -> 结果"
                _, _, result = result_event["detail"].partition(" -> ")
                merged.append(
                    f"- `{time}` **{label}**：{detail}{self._result_tail(result)}"
                )
                i += 2  # 结果事件已并入本行，跳过
                continue

            if event["kind"] == "tool_result":
                # 孤立的 tool_result：通常是因为"最近 N 条"窗口恰好切在
                # 一对事件的中间，找不到配对的 tool_call。同样只报字数，
                # 不贴内容原文。
                name, _, result = event["detail"].partition(" -> ")
                merged.append(
                    f"- `{time}` **{label}**：{_one_line(name)}{self._result_tail(result)}"
                )
                i += 1
                continue

            merged.append(f"- `{time}` **{label}**：{detail}")
            i += 1
        return merged

    @staticmethod
    def _result_tail(result: str) -> str:
        """把工具结果文本转成给人看的一句话（错误原文/字数/短结果）。"""
        if any(tag in result for tag in FAILURE_MARKERS):
            return "，失败：" + _one_line(result, 80)
        if len(result) > 60:
            return f"，返回 {len(result)} 字（详见 task.json）"
        return "，结果：" + _one_line(result, 60)
