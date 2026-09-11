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

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from . import tools as tools_module
from .tools import FAILURE_MARKERS

# 所有任务统一放在项目根目录的 tasks/ 下（本文件位于 src/wovra/）
TASKS_ROOT = Path(__file__).resolve().parent.parent.parent / "tasks"

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

    def apply_patch(self, patch: dict) -> None:
        """应用一轮 Organization 产出的状态补丁。

        * 列表字段：追加去重，超出容量淘汰最旧
        * current_status / goal：直接覆盖
        * is_done：仅接受布尔
        * 非法/缺失字段一律忽略，不让坏数据进状态
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

    def render(self, budget: int | None = None) -> str:
        """渲染成给模型看的文本块。

        空状态返回空串——新会话不给模型一个空的任务状态头
        （否则模型会困惑"任务状态是空的"）。
        """
        lines = []
        if self.goal:
            lines.append(f"目标：{self.goal}")
        if self.current_status:
            lines.append(f"当前状态：{self.current_status}")
        if self.is_done:
            lines.append("任务已完成。")
        for label, name in (
            ("已完成", "completed"),
            ("已决策", "decisions"),
            ("已知问题", "known_issues"),
            ("待解决问题", "open_questions"),
            ("决策升级", "escalations"),
            ("待办实验", "experiments"),
            ("约束", "constraints"),
        ):
            items = getattr(self, name)
            if items:
                lines.append(f"{label}：" + "；".join(items))
        if not lines:
            return ""
        lines.insert(0, "[任务状态]")
        text = "\n".join(lines)
        if budget and len(text) > budget:
            text = text[:budget] + "\n(任务状态过长已截断)"
        return text


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
    requirements: list[str] = field(default_factory=list)
    acceptance_criteria: list[str] = field(default_factory=list)
    status: str = "in_progress"
    # summary 是 Agent 周期性生成的状态摘要（markdown 片段），
    # 对应 README 里 "What happened? Where are we now? ..." 的那份报告
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
    # 会话的上下文模式（managed/baseline）：恢复时沿用，防止实验数据串味
    mode: str = ""
    # 组织运行时（organization-runtime-v1.md）：
    # parent_id = 父任务 id（根任务为空）；org_context = 逐字下传的
    # 意图快照（父目标原文 + 拆解上下文 + 本块所有权边界），每层只追加；
    # pending_instruction = 父任务派发时带给子任务下一轮的指令
    # （如用户拍板的决策），由 `wovra run` 读取并清空——指令走磁盘
    # 而不走命令行参数，绕开 Windows 的引号转义地狱
    parent_id: str = ""
    org_context: str = ""
    pending_instruction: str = ""
    todo: dict = field(default_factory=dict)  # 大步/小步计划账本（深度恒 1）
    # agent 注册表（机制三）：路径 ID / 类别描述 / 所有权文件域 / 状态 /
    # 收件箱（单向通信的落信处）。默认只有主 agent；分裂执行时扩充
    registry: list[dict] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""

    def apply_state_patch(self, patch: dict) -> None:
        """把 Organization 的 state_patch 合并进任务状态（持久化字段）。"""
        state = TaskState(**(self.task_state or {}))
        state.apply_patch(patch)
        self.task_state = asdict(state)
        # is_done 与粗粒度状态机打通：整理判定完成 → status=done，
        # 避免 report 里"任务已完成"与 list 里"进行中"两本账打架
        if patch.get("is_done") is True and self.status != "done":
            self.status = "done"
            self.updated_at = datetime.now().isoformat(timespec="seconds")

    def get_state(self) -> TaskState:
        """以 TaskState 对象的形式读取当前任务状态。"""
        state = TaskState()
        state.__dict__.update(self.task_state or {})
        return state

    def todo_lines(self) -> list[str]:
        """机械渲染当前大步/小步计划账本（人视图共用，零 LLM）。

        大步 = 阶段（最小可行 → 逐步增加功能），小步 = 阶段内的拆解。
        这份账本平时只进模型上下文，人看不到；这里把它变成可读文本。
        """
        todo = self.todo or {}
        milestone = todo.get("milestone")
        steps = todo.get("steps") or []
        history = todo.get("history") or []
        if not milestone:
            lines = ["-（无开启中的大步）"]
            if history:
                # 历史 goal 可能是整段阶段描述，人视图只留一句摘要
                last = _one_line(str(history[-1].get("goal", "")), 50)
                lines.append(f"- 已验收 {len(history)} 个大步（最近：{last}）")
            return lines
        done_n = sum(1 for s in steps if s.get("done"))
        detail = []
        if milestone.get("started_seq") is not None:
            detail.append(f"自 R{milestone['started_seq']}")
        if steps or milestone.get("planned"):
            detail.append(f"小步 {done_n}/{len(steps)}")
        lines = [
            f"- 大步：{_one_line(str(milestone.get('goal', '')), 80)}"
            + (f"（{'；'.join(detail)}）" if detail else "")
        ]
        if not milestone.get("planned"):
            lines.append("  - 尚未拆小步——先 add_step 拆出阶段内工作项")
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
            lines.append(f"- 已验收 {len(history)} 个大步")
        return lines

    def todo_summary_line(self) -> str:
        """当前大步/小步的单行摘要（终端底栏用，无颜色）。

        bottom_toolbar 不解析裸 ANSI，所以这里只给纯文本；着色与否
        由调用方（终端）决定。
        """
        todo = self.todo or {}
        milestone = todo.get("milestone")
        history = todo.get("history") or []
        if not milestone:
            line = "无开启大步"
            if history:
                line += f" · 已验收 {len(history)} 个"
            return line
        steps = todo.get("steps") or []
        line = f"阶段 {_one_line(str(milestone.get('goal', '')), 40)}"
        if milestone.get("planned") or steps:
            done_n = sum(1 for s in steps if s.get("done"))
            line += f" · 小步 {done_n}/{len(steps)}"
        else:
            line += " · 未拆小步"
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
        acceptance_criteria: list[str] | None = None,
    ) -> "Task":
        """新建一个任务。id 用日期 + 短随机串，保证可读又不冲突。"""
        now = datetime.now()
        task_id = now.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        return cls(
            id=task_id,
            goal=goal,
            requirements=list(requirements or []),
            acceptance_criteria=list(acceptance_criteria or []),
            workspace=str(tools_module.safety.PROJECT_ROOT),
            # 注册表默认只有主 agent；分裂执行时扩充（机制三/四）
            registry=[{
                "id": "A", "name": "主agent",
                "description": "全局协调与未归属事务",
                "file_domains": [], "status": "active", "inbox": [],
            }],
            created_at=now.isoformat(timespec="seconds"),
            updated_at=now.isoformat(timespec="seconds"),
        )

    @classmethod
    def load(cls, task_id: str) -> "Task":
        """从磁盘加载任务。task.json 是唯一的事实来源（source of truth）。

        会话绑定的工作区随加载恢复：无论从哪个目录启动 wovra，
        该会话的文件世界都回到它创建时的位置。"""
        from . import tools as tools_module

        path = TASKS_ROOT / task_id / "task.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        task = cls(**data)
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
        acceptance_criteria: list[str] | None = None,
    ) -> "Task":
        """已存在则加载（断点续做），否则新建。演示"停止-恢复"的入口。"""
        if (TASKS_ROOT / task_id / "task.json").exists():
            return cls.load(task_id)
        task = cls.create(goal, requirements, acceptance_criteria)
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

    def set_summary(self, text: str) -> None:
        """更新状态摘要（由 Agent 周期性调用）。"""
        self.summary = text.strip()
        self.updated_at = datetime.now().isoformat(timespec="seconds")

    def apply_state(
        self,
        goal: str | None = None,
        status: str | None = None,
        summary: str | None = None,
    ) -> None:
        """AI 每轮对任务状态的更新入口。

        目标不是建任务时定死的，而是随对话逐步成形、也可能被修正——
        所以 goal/status/summary 都是"谁最新谁说了算"。只有传入了
        的字段才覆盖；status 只接受合法值，防止模型输出污染状态机。
        """
        if goal:
            self.goal = goal
        if status in ("in_progress", "done"):
            self.status = status
        if summary is not None:
            self.summary = summary
        self.updated_at = datetime.now().isoformat(timespec="seconds")

    # ---- 持久化 ---------------------------------------------------------

    def save(self) -> None:
        """把当前状态写到磁盘：task.json + report.md。

        task.json 用临时文件 + 原子替换落盘：write_text 先清空再写，
        写一半崩掉会把整个会话历史截断成空文件（实测 2026-09-05）。
        """
        directory = TASKS_ROOT / self.id
        directory.mkdir(parents=True, exist_ok=True)
        # 兜底清洗：任何环节漏掉代理字符，落盘前最后一道闸
        data = sanitize_surrogates(
            json.dumps(asdict(self), ensure_ascii=False, indent=2)
        )
        tmp = directory / "task.json.tmp"
        tmp.write_text(data, encoding="utf-8")
        tmp.replace(directory / "task.json")
        report = sanitize_surrogates(self._render_report())
        (directory / "report.md").write_text(report, encoding="utf-8")

    # ---- 给模型和报告用的视图 --------------------------------------------

    def context(self, last_n: int = 10) -> str:
        """生成给模型看的任务上下文（作为 system prompt 的一部分）。

        这里体现"上下文生命周期"的第一步：模型不需要整个 history，
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
        if self.acceptance_criteria:
            lines.append(
                "\n## 验收标准\n"
                + "\n".join(f"- {c}" for c in self.acceptance_criteria)
            )
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
        if self.acceptance_criteria:
            lines.append("\n## 验收标准\n")
            lines += [f"- {c}" for c in self.acceptance_criteria]
        lines.append("\n## 当前阶段（大步 / 小步）\n")
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
