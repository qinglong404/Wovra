"""wovra serve：任务现场的只读 HTTP 投影（前端可视化的接口层）。

设计契约见 docs/frontend-visualization-plan.md §3。三条铁律：
* **GET-only**——可视化是只读的，任何状态变更都走 CLI 交互通道；
* **task.json 是唯一事实源**——本层只做"磁盘事实 → JSON"的机械派生，
  不定义新口径（成本/命中率口径以 llm.py 的落账行为准，这里只把
  落账字符串解析成数值）；
* **默认绑定 127.0.0.1**——数据含工作区路径与模型输出，不对外网暴露。

体量纪律：task.json 可达 16MB×78 会话，全量扫一遍不可接受——摘要按
mtime 缓存（后台线程常驻增量重扫），事件原文按需单轮取（expand 语义）。
"""
from __future__ import annotations

import argparse
import itertools
import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import registry as registry_module
from . import task as task_module

_WEBUI = Path(__file__).resolve().parents[2] / "webui" / "index.html"
_STARTED = time.strftime("%Y-%m-%d %H:%M:%S")
_ID_SAFE = re.compile(r"^[0-9A-Za-z_-]+$")
_LLM_CALL = re.compile(r"^\[(\w+)\]\s+(.*)$")
_KV = re.compile(r"(\w+)=([\d,]+(?:\.\d+)?)")
_FINISH = re.compile(r"finish=(\S+)")
_USAGE_HIT = re.compile(r"缓存命中 ([\d,]+) tok")
_USAGE_MISS = re.compile(r"未命中 ([\d,]+) tok")


def parse_usage_row(detail: str) -> dict | None:
    """解析轮尾 usage 落账行（steps/context/prompt/completion + 中文命中段）。"""
    out: dict = {}
    for k, v in _KV.findall(detail or ""):
        v = v.replace(",", "")
        out[k] = float(v) if "." in v else int(v)
    if "prompt" not in out:
        return None
    mh = _USAGE_HIT.search(detail)
    mm = _USAGE_MISS.search(detail)
    out["cached"] = int(mh.group(1).replace(",", "")) if mh else 0
    out["miss"] = int(mm.group(1).replace(",", "")) if mm else 0
    return out


def round_usage_map(data: dict) -> dict[int, dict]:
    """每轮的用量账（steps 签名归属，与 agent_stats 同一口径）。

    一轮只在一个 active_view 上跑——轮级账天然就是该轮 agent 的账。
    context = 轮内各分段上下文峰值。
    """
    rows = [parse_usage_row(h.get("detail", ""))
            for h in data.get("history") or [] if h.get("kind") == "usage"]
    rows = [x for x in rows if x]
    it = iter(rows)
    out: dict[int, dict] = {}
    for r in data.get("rounds") or []:
        target = r.get("steps_used") or 0
        acc = 0
        agg: dict = {"steps": 0, "calls": 0, "prompt": 0, "cached": 0,
                     "miss": 0, "completion": 0, "context": 0}
        while acc < target:
            row = next(it, None)
            if row is None:
                break
            acc += row.get("steps", 0)
            agg["steps"] += row.get("steps", 0)
            agg["calls"] += 1
            for k in _USAGE_KEYS:
                agg[k] += row.get(k, 0)
            agg["context"] = max(agg["context"], row.get("context", 0))
        out[r.get("seq")] = agg
    return out


_ATTR_RE = re.compile(r"R(\d+)→([^、（）]+)")


def attributed_seqs(data: dict) -> dict:
    """渐近归属补判过的轮（seq → 补判到的视图），从 history 的 route 行机械解析。

    这些轮**实际由主 agent 执行**（它们到达时域树尚不存在），只是材料归属
    后来补判给了某个域。故计数与用量都该记在实际执行方，归属另列。
    """
    out: dict = {}
    for h in data.get("history") or []:
        if h.get("kind") != "route":
            continue
        detail = str(h.get("detail") or "")
        if "渐近归属" not in detail:
            continue
        for m in _ATTR_RE.finditer(detail):
            out[int(m.group(1))] = m.group(2).strip()
    return out


def agent_stats(
    data: dict, main_id: str = "", registry: list | None = None
) -> list[dict]:
    """按 agent 聚合：轮次/步数（精确，来自轮元数据 active_view）+ 用量。

    用量归属是**近似口径**：usage 落账行按 steps 签名顺序归属到轮
    （轮的 steps_used 累计值 = 该轮各分段 usage 行 steps 之和）；中断
    分段并帐，漂移只可能出现在未闭合会话尾部。上下文窗口 = 该 agent
    各轮 context 峰值；单轮消费 = Σprompt / 有账轮数。

    3a（2026-09-12）：注册表条目自带**运行时账**（rounds/steps/handoffs/
    ctx_cur/ctx_peak/window，`registry.runtime_stats`），那才是权威口径——
    本函数在聚合出用量后用它覆盖轮次/步数/当前占比，字段只增不改。
    """
    main_id = main_id or registry_module.MAIN_AGENT_ID
    rounds = data.get("rounds") or []
    per: dict[str, dict] = {}
    order: list[str] = []

    def bucket(aid: str) -> dict:
        if aid not in per:
            per[aid] = {"agent": aid, "rounds": 0, "steps": 0, "prompt": 0,
                        "cached": 0, "miss": 0, "completion": 0,
                        "ctx_peak": 0, "billed_rounds": 0,
                        "attributed_rounds": 0, "attributed_steps": 0}
            order.append(aid)
        return per[aid]

    usage = round_usage_map(data)
    attr = attributed_seqs(data)
    for r in rounds:
        view = r.get("active_view") or main_id
        # 渐近归属补判的轮：实际执行方是主 agent（到达时域树还没建），
        # 计数与用量都记它；补判到的域另计 attributed_*（只归位、没干活）
        executor = main_id if r.get("seq") in attr else view
        b = bucket(executor)
        b["rounds"] += 1
        b["steps"] += r.get("steps_used") or 0
        agg = usage.get(r.get("seq")) or {}
        if agg.get("calls"):
            b["billed_rounds"] += 1
        for k in _USAGE_KEYS:
            b[k] += agg.get(k, 0)
        b["ctx_peak"] = max(b["ctx_peak"], agg.get("context", 0))
        if r.get("seq") in attr and view != executor:
            ab = bucket(view)
            ab["attributed_rounds"] += 1
            ab["attributed_steps"] += r.get("steps_used") or 0

    out = []
    runtime = registry_module.runtime_stats(
        registry if registry is not None else (data.get("registry") or [])
    )
    for aid in order:
        b = per[aid]
        b["avg_prompt"] = (b["prompt"] // b["billed_rounds"]) if b["billed_rounds"] else 0
        # 运行时账覆盖（3a）：注册表是权威口径；缺账的历史会话保留聚合值。
        stat = runtime.get(aid)
        if stat:
            b["id"] = stat["id"]
            if stat["rounds"]:
                b["rounds"] = stat["rounds"]
            if stat["steps"]:
                b["steps"] = stat["steps"]
            b["handoffs"] = stat["handoffs"]
            b["ctx_cur"] = stat["ctx_cur"]
            b["window"] = stat["window"]
            b["share"] = stat["share"]
            if stat["ctx_peak"]:
                b["ctx_peak"] = max(b["ctx_peak"], stat["ctx_peak"])
        out.append(b)
    return out




# ---- C4：受控写通道的作业系统 --------------------------------------------
# 唯一的写形态 = 新建会话 / 给会话追加一轮对话。轮执行复用 CLI 的 agent
# 管线（懒导入避免 cli↔serve 循环依赖）。互斥三道：
#   1. _TURN_GATE——serve 进程内同时只跑一轮（本地单用户）；
#   2. CLI 会话锁文件——与正在运行的 chat/run 进程互斥；
#   3. 任务级 job 去重——同一会话不叠加排队。
_JOBS: dict[str, dict] = {}
_JOB_SEQ = itertools.count(1)
_TURN_GATE = threading.Lock()
_job_lock = threading.Lock()


_ASK_TIMEOUT = int(os.environ.get("WOVRA_ASK_TIMEOUT", "1800"))


def _execute_turn(job_id: str, task_id: str, content: str) -> None:
    """轮执行线程：CLI 同款管线（会话锁 → agent → run → 补整理）。

    交互桥（C4.5）：轮内 ask_user / 敏感操作确认不再走 stdin——
    发布到 job.pending（前端渲染选项/输入框），POST /api/jobs/{id}/answer
    回填后放行；超时（WOVRA_ASK_TIMEOUT，默认 30 分钟）保守回退：
    ask → 空回答、confirm → 拒绝。补丁只在轮线程生命周期内生效并恢复。
    """
    job = _JOBS[job_id]
    from .agent import MODE_MANAGED
    from .cli import prompt as _cli_prompt
    from .cli.prompt import _build_agent  # 懒导入：避免 cli↔serve 循环依赖
    from .cli.session import _acquire_session_lock, _release_session_lock
    from .tools import interaction as _interaction
    from .tools import safety as _safety

    state: dict = {"answer": None}
    ev = threading.Event()
    job["_ev"] = ev
    job["_state"] = state   # answer 路由由此回填（桥的闭包读它）

    def _wait(kind: str, text: str, choices: list | None = None,
              multi: bool = False):
        ev.clear()
        job["pending"] = {"type": kind, "question": text,
                          "choices": choices or [], "multi": bool(multi)}
        got = ev.wait(_ASK_TIMEOUT)
        job["pending"] = None
        return (state.get("answer") or ""), got

    def web_ask_user(question: str, choices: str = "", multi: bool = False) -> str:
        opts = _interaction._split_choices(choices)[:8]
        ans, got = _wait("ask", question, opts, multi)
        ans = (ans or "").strip()
        letters = "ABCDEFGH"
        if opts and ans:
            tokens = ([t.strip().rstrip(".").upper() for t in ans.split(",")]
                      if multi else [ans.rstrip(".").upper()])
            last = letters[len(opts) - 1]
            if all(len(t) == 1 and "A" <= t <= last for t in tokens):
                picked = " | ".join(opts[ord(t) - ord("A")] for t in tokens)
                return f"用户的回答: {picked}"
        return f"用户的回答: {ans or '（空）'}"

    def web_ask_yes_no(question: str) -> bool:
        """确认闸门（审批模式）：自主模式全放行；"以后同类"命中白名单放行；
        三选项回答 always 时把标签记进会话白名单。"""
        tag = confirm_tag(question)
        mode = str(getattr(task, "safety_mode", "approve") or "approve")
        if mode == "auto":
            job["live"].append({"k": "status", "s": "自主运行：敏感操作自动放行"})
            return True
        if tag in (getattr(task, "approved_tags", None) or []):
            job["live"].append({"k": "status", "s": f"已授权同类操作：{tag}"})
            return True
        ans, got = _wait("confirm", question)
        a = (ans or "").strip().lower()
        if a in ("always", "总是", "以后同类", "a"):
            tags = list(getattr(task, "approved_tags", None) or [])
            if tag not in tags:
                tags.append(tag)
            task.approved_tags = tags
            task.save()
            return True
        return got and a in ("y", "yes")

    orig_ask, orig_yes = _cli_prompt.ask_user, _safety._ask_yes_no
    _cli_prompt.ask_user = web_ask_user
    _safety._ask_yes_no = web_ask_yes_no
    try:
        job["status"] = "running"
        task = task_module.Task.load(task_id)
        if not task.goal:
            task.goal = content.strip()[:80]   # 目标随对话成形（对齐 CLI）
            task.save()
        if task.workspace and Path(task.workspace).is_dir():
            # Task.load 已切 safety.PROJECT_ROOT；cli 侧是 import 时值快照，
            # 系统提示词里的"工作区：…"必须一起切（全局单飞下无竞争）
            _cli_prompt.PROJECT_ROOT = Path(task.workspace)
        with _TURN_GATE:
            _acquire_session_lock(task)  # 被占用时抛 SystemExit（CLI 语义）
            try:
                agent = _build_agent(task, mode=task.mode or MODE_MANAGED,
                                     async_organization=False)

                def _live(chunk: dict) -> None:
                    # 轮直播流（思考/回答增量、步骤状态）。单消费者轮询读，
                    # append 原子足够；量级 = 单轮流式分片，无需封顶。
                    job["live"].append(chunk)

                job["live"] = []
                agent.on_progress = lambda s: _live({"k": "status", "s": s})
                # ⏹ 终止（对齐 CLI Ctrl+C）：每步与流中分片间检查，触发处
                # 抛 KeyboardInterrupt，由下方 except 收尾成开放轮
                agent.cancel_check = lambda: bool(job.get("cancel"))
                try:
                    if content is None:   # /c 续跑：不注入新消息
                        answer = agent.resume(
                            on_thinking=lambda d: _live({"k": "think", "s": d}),
                            on_answer_delta=lambda d: _live({"k": "ans", "s": d}),
                        )
                    else:
                        answer = agent.run(
                            content,
                            on_thinking=lambda d: _live({"k": "think", "s": d}),
                            on_answer_delta=lambda d: _live({"k": "ans", "s": d}),
                        )
                except KeyboardInterrupt:
                    agent.finalize_round("open")   # 中断不闭合轮次（CLI 同款）
                    job["status"] = "cancelled"
                    job["answer"] = "已终止——轮保持开放，发 /c 可续跑"
                    return
                agent.organize_backlog()
            finally:
                _release_session_lock(task)
        job["status"] = "done"
        job["answer"] = answer
    except SystemExit:
        job["status"] = "error"
        job["error"] = "会话被其它进程占用（请先关闭占用它的 CLI 窗口）"
    except Exception as error:  # noqa: BLE001——错误原样回给发起页
        job["status"] = "error"
        job["error"] = f"{type(error).__name__}: {error}"
    finally:
        _cli_prompt.ask_user = orig_ask
        _safety._ask_yes_no = orig_yes
        job["pending"] = None

_ORG_STATES = ("done", "pending", "failed")


_WEB_HELP = """网页斜杠命令（零模型成本，除 /c 续跑外均为本地操作）：
  /c /继续          续跑开放轮（步数超限或 ⏹ 终止后的标准恢复方式）
  /undo /撤销       撤销最近一条开放轮（含其全部事件；已闭合轮不可撤）
  /todo /阶段 /计划 跳到计划页
  /report /报告     跳到时间线页
  /maint /维护 /进度整理与分裂进度（右侧抽屉）
  /bg [编号]        后台任务：查看状态与增量输出
  /bg stop 编号     停止某个后台任务
  /help             显示本帮助"""



_USAGE_KEYS = ("prompt", "cached", "miss", "completion")

# 项目树排除目录（重型/生成物；工作区里这些没有浏览价值）
_IGNORE_DIRS = frozenset({
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
    "env", "dist", "build", "target", ".pytest_cache", ".mypy_cache",
    ".shots",   # 本会话前端截图的临时目录（工作区噪声）
    ".ruff_cache", ".idea", ".vscode", ".tox", "site-packages",
})

_TREE_MAX = 4000

_DUMP_CAP = 6000   # 上下文导出的单条消息上限（页面展示用，机械截断并标注）


def project_tree(workspace: str) -> dict:
    """会话工作区的文件树（扁平相对路径列表，前端建树）。

    排除重型/生成目录（.git、node_modules、虚拟环境、构建产物…）；
    条数上限 _TREE_MAX，超出标注 truncated（大仓不拖垮页面）。
    """
    root = Path(workspace or "")
    if not workspace or not root.is_dir():
        return {"root": workspace, "files": [], "error": "工作目录不存在"}
    files: list[dict] = []
    truncated = False
    for cur, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if d not in _IGNORE_DIRS]
        for n in names:
            p = Path(cur) / n
            try:
                st = p.stat()
            except OSError:
                continue
            files.append({"p": str(p.relative_to(root)).replace("\\", "/"),
                          "s": st.st_size})
            if len(files) >= _TREE_MAX:
                truncated = True
                break
        if truncated:
            break
    files.sort(key=lambda x: x["p"].lower())
    return {"root": str(root), "files": files, "truncated": truncated}


def fs_list(path: str | None) -> dict:
    """目录浏览（新建会话选工作目录用）：只列子目录，只读。

    path 为空 → 列盘根（Windows）/文件系统根（POSIX）。
    """
    if not path:
        if os.name == "nt":
            roots = []
            for L in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
                if Path(f"{L}:\\").exists():
                    roots.append(f"{L}:\\")
            return {"roots": roots, "dirs": [], "path": ""}
        return {"roots": ["/"], "dirs": [], "path": ""}
    p = Path(path)
    if not p.is_absolute() or not p.is_dir():
        return {"roots": [], "dirs": [], "path": str(p), "error": "目录不存在"}
    dirs = []
    try:
        for child in p.iterdir():
            try:
                if child.is_dir():
                    dirs.append(child.name)
            except OSError:
                continue
    except OSError as e:
        return {"roots": [], "dirs": [], "path": str(p), "error": str(e)}
    return {"roots": [], "dirs": sorted(dirs, key=str.lower), "path": str(p)}


def parse_llm_call(detail: str) -> dict | None:
    """把 llm_call 落账行解析成数值字典（口径 = llm.py 落账，零新口径）。

    例：'[working] prompt=5,226 cached=0 miss=5,226 completion=185
    ttft=2.0s dur=2.7s finish=tool_calls'
    """
    m = _LLM_CALL.match(detail or "")
    if not m:
        return None
    out: dict = {"purpose": m.group(1)}
    for k, v in _KV.findall(m.group(2)):
        v = v.replace(",", "")
        out[k] = float(v) if "." in v else int(v)
    if "prompt" not in out:
        return None  # usage 汇总行等其它 [kind] 前缀不算调用账
    fm = _FINISH.search(m.group(2))
    if fm:
        out["finish"] = fm.group(1)
    return out


def usage_totals(history: list[dict] | None) -> dict:
    """从 history 的 llm_call 行汇总用量，并保留逐行序列（前端画图用）。"""
    totals: dict = {"calls": 0, "prompt": 0, "cached": 0, "miss": 0,
                    "completion": 0, "ttft_sum": 0.0, "finish": {}, "rows": []}
    for h in history or []:
        if h.get("kind") != "llm_call":
            continue
        row = parse_llm_call(h.get("detail", ""))
        if not row:
            continue
        totals["calls"] += 1
        for k in _USAGE_KEYS:
            totals[k] += row.get(k, 0)
        totals["ttft_sum"] += row.get("ttft", 0.0)
        f = row.get("finish", "?")
        totals["finish"][f] = totals["finish"].get(f, 0) + 1
        totals["rows"].append({"time": h.get("time", ""), **row})
    return totals


def confirm_tag(question: str) -> str:
    """把确认问题归成一个"同类"标签（审批三选项的"以后同类"靠它）。

    命中模式（命令含敏感操作）→ 用模式串做标签（最稳的同类判据）；
    文件删除 → 用路径；其余退回问题前 60 字。
    """
    q = str(question or "")
    m = re.search(r"命中 `([^`]+)`", q)
    if m:
        return "cmd:" + m.group(1)
    m = re.search(r"确认删除文件 (.+?)[？（]", q)
    if m:
        return "del:" + m.group(1).strip()
    return "q:" + q.strip()[:60]


def todo_log(data: dict, limit: int = 60) -> list[dict]:
    """todo 工具调用流水（从轮事件机械派生，零新口径）。

    计划页要能看到"模型对计划做了什么"——check_step/push/verify_milestone
    这些动作此前只落在事件里，页面上无处可看。
    """
    out: list[dict] = []
    for r in data.get("rounds") or []:
        results: dict = {}
        for e in r.get("events") or []:
            msg = e.get("message") or {}
            if msg.get("role") == "tool" and msg.get("tool_call_id"):
                results[msg["tool_call_id"]] = str(msg.get("content") or "")[:200]
        for e in r.get("events") or []:
            msg = e.get("message") or {}
            for tc in msg.get("tool_calls") or []:
                fn = tc.get("function") or {}
                if fn.get("name") != "todo":
                    continue
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                out.append({
                    "seq": r.get("seq"), "time": e.get("timestamp", ""),
                    "action": str(args.get("action") or ""),
                    "text": str(args.get("text") or args.get("goal")
                                or args.get("evidence") or "")[:160],
                    "result": results.get(tc.get("id"), "")[:200],
                })
    return out[-limit:]


def session_summary(task_id: str, data: dict) -> dict:
    """会话摘要（列表视图用；小对象，常驻缓存）。"""
    rounds = data.get("rounds") or []
    org = {"done": 0, "pending": 0, "failed": 0, "raw": 0}
    for r in rounds:
        state = r.get("org_state")
        org[state if state in _ORG_STATES else "raw"] += 1
    ts = data.get("task_state") or {}
    todo = data.get("todo") or {}
    ms = todo.get("milestone") or {}
    return {
        "id": task_id,
        "goal": data.get("goal", ""),
        "status": data.get("status", ""),
        "workspace": data.get("workspace", ""),
        "mode": data.get("mode", ""),
        "created_at": data.get("created_at", ""),
        "updated_at": data.get("updated_at", ""),
        "rounds": len(rounds),
        "steps": sum(r.get("steps_used") or 0 for r in rounds),
        "org": org,
        "escalations": len(ts.get("escalations") or []),
        "experiments": len(ts.get("experiments") or []),
        "todo_milestone": ms.get("goal"),
        "todo_steps_left": sum(1 for s in todo.get("steps") or []
                               if not s.get("done")),
        "last_round": ({"seq": rounds[-1].get("seq"),
                        "events": len(rounds[-1].get("events") or []),
                        "end_state": rounds[-1].get("end_state")} if rounds
                       else None),
        "usage": usage_totals(data.get("history")),
    }


def pending_views(task_id: str, domain: str = "") -> dict | None:
    """物化"待生效"分裂产物的域视图（内存模拟生效，绝不落盘）。

    暂存中的域还没并入注册表，正常路径取不到视图。这里：Task.load（走真实
    加载器，含块结构迁移）→ 把 agent._persist_rounds 换成空操作 → 在内存里
    跑一遍产物生效流程 → 物化各域装配视图。用户因此能在产物生效前就核对
    "每个子 agent 重组后会看到什么"，且会话一个字节都不变。
    """
    from .agent import MODE_MANAGED
    from .cli.prompt import _build_agent  # 懒导入：避免 cli↔serve 循环依赖
    try:
        task = task_module.Task.load(task_id)
        agent = _build_agent(task, mode=task.mode or MODE_MANAGED)
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    agent._persist_rounds = lambda: None       # 预览：绝不落盘
    # 重组前全文必须在 settle **之前**取：settle 会应用本次产物并触发分档折叠，
    # 之后再装配拿到的是"已重组口径"（实测 5 条 vs 重组前 27 条）
    try:
        _pre_settle = agent._assemble_messages()
    except Exception:  # noqa: BLE001
        _pre_settle = None
    try:
        # 完整开场状态（promote + 渐近归属 _settle_views）——只 promote 不够：
        # 视图装配依赖各轮/块的域归属判定（505a101 起归属在 settle 里做）
        agent._settle_after_maintenance()
    except Exception:  # noqa: BLE001——预览失败不该影响任何东西
        pass
    out = []
    # 主 agent 也有"重组后视图"（不含分出去的域块），与全量装配不是一回事——
    # 同样列出来，用户才能对照"主 agent 现在看到什么"
    entries = list(task.registry or [])
    if not any(str(e.get("id")) == "Main" for e in entries):
        entries.insert(0, {"id": "Main", "name": "主agent"})
    for e in entries:
        if domain and domain not in (str(e.get("name")), str(e.get("id"))):
            continue
        got = None
        # 必须用 agent.rounds：settle 的产物写进 agent 自己的那份列表，
        # task.rounds 是加载时的另一份副本（否则域信息看不见 → 装配降级 None）
        for key in (e.get("name"), e.get("id")):
            got = agent._assemble_view_messages(str(key or ""), agent.rounds or [])
            if got is not None:
                break
        if got is None:
            continue
        try:
            tok = int(agent._estimate_messages(got))   # 与运行时同口径（防单位混用）
        except Exception:  # noqa: BLE001
            tok = sum(len(str(m.get("content") or "")) for m in got) // 3
        msgs, total, trunc = [], 0, 0
        for m in got:
            raw = str(m.get("content") or "")
            total += len(raw)
            body = raw
            if len(raw) > _DUMP_CAP:
                body = raw[:_DUMP_CAP] + chr(10) + f"…（本条截断，原 {len(raw):,} 字符）"
                trunc += 1
            msgs.append({"role": m.get("role"), "content": body, "len": len(raw),
                         "tool_calls": len(m.get("tool_calls") or [])})
        is_main = str(e.get("id")) == "Main"
        out.append({"id": e.get("id"), "name": e.get("name"),
                    "is_main": is_main,
                    "description": str(e.get("description") or "")[:160],
                    "file_domains": list(e.get("file_domains") or []),
                    "messages": msgs, "count": len(msgs),
                    "total_chars": total, "tokens": tok, "truncated": trunc})
    if out:
        # 主 agent 的"整理后、未按域重组"全文 = settle 之前的装配（见上）；
        full = _pre_settle if _pre_settle is not None else agent._assemble_messages()
        alt_chars = sum(len(str(m.get("content") or "")) for m in full)
        alt_msgs, alt_trunc = [], 0
        for m in full:
            raw = str(m.get("content") or "")
            body = raw
            if len(raw) > _DUMP_CAP:
                body = raw[:_DUMP_CAP] + chr(10) + f"…（本条截断，原 {len(raw):,} 字符）"
                alt_trunc += 1
            alt_msgs.append({"role": m.get("role"), "content": body, "len": len(raw),
                             "tool_calls": len(m.get("tool_calls") or [])})
        try:
            alt_tok = int(agent._estimate_messages(full))
        except Exception:  # noqa: BLE001
            alt_tok = alt_chars // 3
        out[0]["alt_chars"] = alt_chars
        out[0]["alt_count"] = len(full)
        out[0]["alt_tokens"] = alt_tok
        out[0]["alt_messages"] = alt_msgs
        out[0]["alt_truncated"] = alt_trunc
    if not out and _pre_settle is not None:
        # 无分裂产物（或视图降级）：至少给出"当前装配"一档，别让抽屉空着
        msgs, total, trunc = [], 0, 0
        for m in _pre_settle:
            raw = str(m.get("content") or "")
            total += len(raw)
            body = raw
            if len(raw) > _DUMP_CAP:
                body = raw[:_DUMP_CAP] + chr(10) + f"…（本条截断，原 {len(raw):,} 字符）"
                trunc += 1
            msgs.append({"role": m.get("role"), "content": body,
                         "len": len(raw), "tool_calls": len(m.get("tool_calls") or [])})
        try:
            tok = int(agent._estimate_messages(_pre_settle))
        except Exception:  # noqa: BLE001
            tok = total // 3
        out.append({"id": "Main", "name": "主agent", "is_main": True, "count": len(msgs),
                    "total_chars": total, "tokens": tok, "truncated": trunc,
                    "messages": msgs, "description": "", "file_domains": []})
    return {"agents": out, "pending": True,
            "note": "在内存中模拟产物生效所得（不改动会话）；真实生效发生在轮闭合或开新轮时"}


def view_sizes(task_id: str) -> dict | None:
    """各 agent 重组后视图体量（走 pending_views 的物化，剥掉消息正文）。

    顶部状态栏用：未生效的分裂产物也要显示"重组后各 agent 各看到多少"，
    不然注册表里只剩分裂前那次装配的旧数（实测：显示 17K，实际重组后 5.9K）。
    """
    got = pending_views(task_id)
    if got is None:
        return None
    return {"agents": [{k: a.get(k) for k in
                        ("id", "name", "is_main", "count", "total_chars",
                         "tokens", "alt_count", "alt_chars",
                         "alt_tokens")}
                       for a in got.get("agents") or []],
            "pending": True, "note": got.get("note")}


def _event_agents(r: dict, main_id: str) -> list[str]:
    """轮内逐事件的 agent 归属（机械派生，零推断）。

    口径（2026-09-12 用户拍板 + route_to 实现）：每轮恒由主 agent 起手并
    路由原话；`route_to` 在**工具批次跑完后**才换手，故换手点 = 它的工具
    结果之后。`switch_view` 只管下一轮，不在本规则内。
    """
    evs = r.get("events") or []
    routes: dict = {}
    for e in evs:
        msg = e.get("message") or {}
        for tc in (msg.get("tool_calls") or []):
            fn = tc.get("function") or {}
            if fn.get("name") not in ("route_to", "switch_view"):
                continue
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            tgt = str(args.get("agent") or "").strip()
            if tgt and tc.get("id"):
                routes[tc["id"]] = (tgt, fn.get("name"))
    cur = main_id
    out = []
    for e in evs:
        msg = e.get("message") or {}
        out.append(cur)
        if msg.get("role") == "tool" and msg.get("tool_call_id") in routes:
            tgt, name = routes[msg["tool_call_id"]]
            if name == "route_to":     # 本回合内换手；switch_view 下一轮才生效
                cur = tgt
    return out


def _split_meta(r: dict) -> dict | None:
    """分裂结果（已落实的轮字段 + 尚未落实的 pending_org）——机械透出。

    split_assessment/domains 落盘在轮上；未落实时暂存 pending_org，
    下一轮开启才并入注册表。"分裂成功没/为什么/结果如何"全在这里。
    """
    po = r.get("pending_org") or {}
    sa = r.get("split_assessment") or po.get("split_assessment")
    dm = r.get("domains") or po.get("domains")
    un = r.get("unassigned") or po.get("unassigned")
    if not (sa or dm or un):
        return None
    return {
        "assessment": sa if isinstance(sa, dict) else {},
        "domains": [{"name": d.get("name"), "description": d.get("description") or "",
                     "file_domains": d.get("file_domains") or []}
                    for d in (dm or []) if isinstance(d, dict)],
        "unassigned": len(un or []),
        "pending": bool(po.get("domains") or po.get("split_assessment")),
    }


def _round_meta(r: dict, usage: dict | None = None) -> dict:
    """轮元数据（不含 events 原文——16MB 级会话事件按需单轮取）。"""
    evs = r.get("events") or []
    return {
        "seq": r.get("seq"),
        "user_input": (r.get("user_input") or {}).get("original", ""),
        "end_state": r.get("end_state"),
        "org_state": r.get("org_state"),
        "org_generation": r.get("org_generation", 1),
        "steps_used": r.get("steps_used"),
        "active_view": r.get("active_view") or "",
        "route_hops": r.get("route_hops", 0),   # 轮内转交次数（>0 = 主 agent 路由过）
        "events": len(evs),
        "t0": (evs[0].get("timestamp") or "") if evs else "",
        "t1": (evs[-1].get("timestamp") or "") if evs else "",
        "usage": usage or {},
        "split": _split_meta(r),
        "blocks": r.get("blocks") or [],
    }


def session_meta(task_id: str, data: dict) -> dict:
    """会话元数据（详情页用：摘要 + 账本 + 计划 + 注册表 + 轮元数据）。"""
    meta = session_summary(task_id, data)
    meta["task_state"] = data.get("task_state") or {}
    meta["todo"] = data.get("todo") or {}
    meta["registry"] = data.get("registry") or []
    # 窗口语义纠正（2026-09-12）：旧数据把整理水位 100K 存进了 registry
    # window——投影层按真实窗口展示；落盘值随下一轮活动由 core 自愈
    from .agent.support import _DEFAULT_CONTEXT_LIMIT
    for e in meta["registry"] or []:
        if isinstance(e, dict) and (not int(e.get("window") or 0)
                                    or int(e["window"]) == 100_000):
            e["window"] = _DEFAULT_CONTEXT_LIMIT
    usage = round_usage_map(data)
    meta["round_list"] = [_round_meta(r, usage.get(r.get("seq")))
                          for r in data.get("rounds") or []]
    meta["agent_stats"] = agent_stats(data)
    return meta


def round_detail(data: dict, seq: int,
                 after: str | None = None) -> dict | None:
    """单轮完整事件与块（expand 语义：块/事件原文按需取）。

    after='R{n}-E{m}'：只返回该事件之后的部分（C3 实时跟随身增量）。
    """
    for r in data.get("rounds") or []:
        if r.get("seq") != seq:
            continue
        after_n = None
        if after:
            ma = re.fullmatch(r"R\d+-E(\d+)", str(after))
            after_n = int(ma.group(1)) if ma else None

        def _num(eid) -> int:
            me = re.match(r"R\d+-E(\d+)", str(eid or ""))
            return int(me.group(1)) if me else -1

        evs_all = r.get("events") or []
        agents = _event_agents(r, "Main")
        events = []
        for ei, e in enumerate(evs_all):
            msg = e.get("message") or {}
            if after_n is not None and _num(e.get("id")) <= after_n:
                continue
            events.append({
                "id": e.get("id"), "type": e.get("type"),
                "agent": agents[ei] if ei < len(agents) else "",
                "time": e.get("timestamp", ""),
                "thinking": e.get("thinking", ""),
                "status": e.get("status", ""), "role": msg.get("role"),
                "content": msg.get("content", ""),
                "tool_calls": msg.get("tool_calls"),
                "tool_call_id": msg.get("tool_call_id"),
            })
        return {"seq": seq,
                "user_input": (r.get("user_input") or {}).get("original", ""),
                "events": events, "blocks": r.get("blocks") or []}
    return None


def view_messages(task_id: str, view: str) -> dict | None:
    """物化某个 agent 的上下文视图（= 路由到它时模型所见，确定性派生）。

    用于人工检查分裂/重组后的视图是否正确。view 先按域名再按 id 尝试。
    """
    from .agent import MODE_MANAGED
    from .cli.prompt import _build_agent  # 懒导入：避免 cli↔serve 循环依赖
    try:
        task = task_module.Task.load(task_id)
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    agent = _build_agent(task, mode=task.mode or MODE_MANAGED)
    agent.current_round = None
    msgs = agent._assemble_view_messages(view, task.rounds or [])
    if msgs is None:
        return {"view": view, "available": False, "messages": [],
                "total_chars": 0,
                "note": "无分裂产物或视图降级：装配走全量路径（无独立视图）"}
    return {"view": view, "available": True,
            "messages": [{"role": m.get("role"),
                          "content": m.get("content", "")} for m in msgs],
            "total_chars": sum(len(m.get("content", "")) for m in msgs)}


def context_dump(task_id: str, mode: str = "view",
                 view: str = "") -> dict | None:
    """导出上下文供人工核对（全量原文 / 当前装配视图）。

    * mode="raw"  —— 所有轮事件原文，未经整理压缩（"我看到过什么"）
    * mode="view" —— 当前装配后的上下文（整理/压缩生效后的样子）；
                    带 view 参数时物化该 agent 的独立视图
    每条消息按 _DUMP_CAP 截断（16MB 级会话不能整包塞给页面），
    截断为机械行为并在响应里标注。
    """
    from .agent import MODE_MANAGED
    from .cli.prompt import _build_agent  # 懒导入：避免 cli↔serve 循环依赖
    try:
        task = task_module.Task.load(task_id)
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    agent = _build_agent(task, mode=task.mode or MODE_MANAGED)
    agent.current_round = None
    note = ""
    if mode == "raw":
        msgs = []
        for r in task.rounds or []:
            msgs.extend(e.get("message") or {} for e in (r.get("events") or []))
        note = ("全量原文：所有轮事件未经整理/压缩（未整理轮原样、已整理轮也按原文）"
                "——与模型当前实际所见不同，仅供对照")
    elif view:
        got = agent._assemble_view_messages(view, task.rounds or [])
        if got is None:
            # 无独立视图（未分裂/视图降级）：回退到主装配——用户要的是
            # "这个 agent 现在看到什么"，不是一句"不可用"
            msgs = agent._assemble_messages()
            note = (f"{view} 无独立视图（未分裂或走全量路径）——"
                    "以下为主装配视图（整理/压缩生效后）")
        else:
            msgs = got
    else:
        msgs = agent._assemble_messages()
        note = "当前装配视图：整理/压缩/分档生效后，模型下一轮实际会看到的上下文"
    out, total, truncated = [], 0, 0
    for m in msgs:
        body = str(m.get("content") or "")
        total += len(body)
        if len(body) > _DUMP_CAP:
            body = (body[:_DUMP_CAP]
                    + chr(10) + f"…（本条截断，原 {len(body):,} 字符）")
            truncated += 1
        out.append({"role": m.get("role"), "content": body,
                    "len": len(str(m.get("content") or "")),
                    "tool_calls": len(m.get("tool_calls") or [])})
    return {"mode": mode, "view": view, "available": True, "messages": out,
            "count": len(out), "total_chars": total, "truncated": truncated,
            "note": note}


class SummaryCache:
    """mtime 缓存：文件没变不重解析；摘要常驻，原文按需重读。"""

    def __init__(self, tasks_root: Path):
        self._root = tasks_root
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[float, dict]] = {}
        self.scanning = True

    def scan(self) -> None:
        """增量扫 tasks/：只重解析 mtime 变化的会话（新→旧，热头优先）。"""
        try:
            dirs = sorted(self._root.iterdir(),
                          key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            dirs = []
        for path in dirs:
            tf = path / "task.json"
            if not _ID_SAFE.match(path.name) or not tf.is_file():
                continue
            try:
                st = tf.stat()
            except OSError:
                continue
            with self._lock:
                cached = self._cache.get(path.name)
            if cached and cached[0] == st.st_mtime:
                continue
            try:
                data = json.loads(tf.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            summary = session_summary(path.name, data)
            with self._lock:
                self._cache[path.name] = (st.st_mtime, summary)

    def snapshot(self) -> list[dict]:
        with self._lock:
            items = [s for _, s in self._cache.values()]
        items.sort(key=lambda s: s.get("updated_at") or "", reverse=True)
        return items


def _warm(cache: SummaryCache, stop: threading.Event) -> None:
    """后台常驻：首轮全量、之后 3s 一轮增量（mtime 没变即跳过，廉价）。"""
    while not stop.is_set():
        cache.scan()
        cache.scanning = False
        stop.wait(3.0)


class _Handler(BaseHTTPRequestHandler):
    cache: SummaryCache = None  # type: ignore[assignment]
    tasks_root: Path = None  # type: ignore[assignment]

    def log_message(self, fmt, *args):  # 静默访问日志（默认 stdout 刷屏）
        pass

    # ---- 响应辅助 ----
    def _json(self, payload, code: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _bytes(self, body: bytes, ctype: str) -> None:
        # no-store 一律带：前端热更新靠浏览器每次拿到最新 index.html
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _html(self) -> None:
        try:
            body = _WEBUI.read_bytes()
        except OSError:
            self._json({"error": "webui/index.html 缺失"}, 500)
            return
        self._bytes(body, "text/html; charset=utf-8")

    def _load_task(self, task_id: str) -> dict | None:
        if not _ID_SAFE.match(task_id):
            return None
        try:
            return json.loads(
                (self.tasks_root / task_id / "task.json").read_text(
                    encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            return None

    # ---- 路由 ----
    def do_GET(self):  # noqa: N802
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            return self._html()
        mv = re.fullmatch(r"/vendor/([\w.-]+)", path)
        if mv:
            name = mv.group(1)
            ctype = ("application/javascript" if name.endswith(".js")
                     else "text/css" if name.endswith(".css")
                     else "application/octet-stream")
            f = (_WEBUI.parent / "vendor" / name)
            try:
                return self._bytes(f.read_bytes(), ctype)
            except OSError:
                return self._json({"error": "not found"}, 404)
        if path == "/api/fs/ls":
            qs = parse_qs(urlparse(self.path).query)
            return self._json(fs_list((qs.get("path") or [None])[0]))
        if path == "/api/sessions":
            self.cache.scan()  # 增量：mtime 没变的会话直接跳过（廉价）
            items = self.cache.snapshot()
            return self._json({"scanning": self.cache.scanning,
                               "server_started": _STARTED,
                               "sessions": items})
        m = re.fullmatch(r"/api/sessions/([^/]+)", path)
        if m:
            data = self._load_task(m.group(1))
            if data is None:
                return self._json({"error": "session not found"}, 404)
            meta = session_meta(m.group(1), data)
            with _job_lock:   # 页面刷新后重新挂上运行中轮的直播流
                live = [jid for jid, j in _JOBS.items()
                        if j["task_id"] == m.group(1)
                        and j["status"] in ("queued", "running")]
            meta["live_job"] = live[0] if live else None
            meta["safety_mode"] = str(data.get("safety_mode") or "approve")
            meta["approved_tags"] = list(data.get("approved_tags") or [])
            from .agent.support import _ORG_WATERMARK_DEFAULT
            meta["org_watermark"] = _ORG_WATERMARK_DEFAULT   # 整理水位（账本产出条件）
            meta["todo_log"] = todo_log(data)                 # todo 工具调用流水
            return self._json(meta)
        m = re.fullmatch(r"/api/sessions/([^/]+)/views/(.+)", path)
        if m:
            view = unquote(m.group(2))
            result = view_messages(m.group(1), view)
            if result is None:
                return self._json({"error": "session not found"}, 404)
            return self._json(result)
        mvs = re.fullmatch(r"/api/sessions/([^/]+)/view-sizes", path)
        if mvs:
            result = view_sizes(mvs.group(1))
            if result is None:
                return self._json({"error": "session not found"}, 404)
            return self._json(result)
        mpv = re.fullmatch(r"/api/sessions/([^/]+)/pending-views", path)
        if mpv:
            qs = parse_qs(urlparse(self.path).query)
            dom = (qs.get("domain") or [""])[0]
            result = pending_views(mpv.group(1), dom)
            if result is None:
                return self._json({"error": "session not found"}, 404)
            return self._json(result)
        mctx = re.fullmatch(r"/api/sessions/([^/]+)/context", path)
        if mctx:
            qs = parse_qs(urlparse(self.path).query)
            mode = (qs.get("mode") or ["view"])[0]
            view = (qs.get("view") or [""])[0]
            result = context_dump(mctx.group(1), mode, view)
            if result is None:
                return self._json({"error": "session not found"}, 404)
            return self._json(result)
        mtree = re.fullmatch(r"/api/sessions/([^/]+)/tree", path)
        if mtree:
            data = self._load_task(mtree.group(1))
            if data is None:
                return self._json({"error": "session not found"}, 404)
            return self._json(project_tree(str(data.get("workspace") or "")))
        m = re.fullmatch(r"/api/sessions/([^/]+)/rounds/(\d+)", path)
        if m:
            data = self._load_task(m.group(1))
            if data is None:
                return self._json({"error": "round not found"}, 404)
            after = (parse_qs(urlparse(self.path).query).get("after")
                     or [None])[0]
            detail = round_detail(data, int(m.group(2)), after=after)
            if detail is None:
                return self._json({"error": "round not found"}, 404)
            return self._json(detail)
        mj = re.fullmatch(r"/api/jobs/([^/]+)", path)
        if mj:
            job = _JOBS.get(mj.group(1))
            if job is None:
                return self._json({"error": "job not found"}, 404)
            return self._json({k: job.get(k)
                               for k in ("status", "answer", "error",
                                         "pending")})
        ml = re.fullmatch(r"/api/jobs/([^/]+)/live", path)
        if ml:
            job = _JOBS.get(ml.group(1))
            if job is None:
                return self._json({"error": "job not found"}, 404)
            qs = parse_qs(urlparse(self.path).query)
            after = int((qs.get("after") or ["0"])[0] or 0)
            chunks = job.get("live") or []
            return self._json({"status": job["status"],
                               "pending": job.get("pending"),
                               "chunks": chunks[after:],
                               "next": len(chunks)})
        mss = re.fullmatch(r"/api/jobs/([^/]+)/stream", path)
        if mss:
            # SSE 推送：一条长连接，分片到达即推——比任何轮询率都快且无
            # 连接/线程churn。50ms 批次粒度 = 人眼无感的合成延迟。
            job = _JOBS.get(mss.group(1))
            if job is None:
                return self._json({"error": "job not found"}, 404)
            self.send_response(200)
            self.send_header("Content-Type",
                             "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            last = 0
            try:
                while True:
                    chunks = job.get("live") or []
                    if len(chunks) > last:
                        payload = b"".join(
                            b"data: "
                            + json.dumps(c, ensure_ascii=False).encode("utf-8")
                            + b"\n\n" for c in chunks[last:])
                        self.wfile.write(payload)
                        last = len(chunks)
                    if job["status"] not in ("queued", "running"):
                        self.wfile.write(
                            b"event: done\ndata: "
                            + json.dumps({"status": job["status"]}).encode()
                            + b"\n\n")
                        return
                    time.sleep(0.05)
            except (BrokenPipeError, ConnectionResetError, OSError):
                return   # 客户端断开（刷新/关页/服务关停）
        return self._json({"error": "not found"}, 404)

    # ---- C4：受控写通道（唯一的写形态 = 新建会话 / 追加一轮对话） ----
    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self._json({"error": "bad json"}, 400)
        path = urlparse(self.path).path
        if path == "/api/sessions":
            return self._create_session(body)
        if path == "/api/shutdown":
            with _job_lock:
                busy = any(j["status"] in ("queued", "running")
                           for j in _JOBS.values())
            if busy:
                return self._json({"error": "有正在运行的轮，先等它结束"}, 409)
            # 先应答、后关停：响应送达前端再停主循环并关闭监听 socket；
            # 作业线程皆 daemon，主线程（serve_forever）返回后进程自然退出
            threading.Timer(0.3, lambda: (self.server.shutdown(),
                                          self.server.server_close())).start()
            return self._json({"ok": True})
        m = re.fullmatch(r"/api/sessions/([^/]+)/turn", path)
        if m:
            return self._start_turn(m.group(1), str(body.get("content") or ""))
        mr = re.fullmatch(r"/api/sessions/([^/]+)/resume", path)
        if mr:
            return self._start_resume(mr.group(1))
        msf = re.fullmatch(r"/api/sessions/([^/]+)/safety", path)
        if msf:
            mode = str(body.get("mode") or "")
            if mode not in ("approve", "auto"):
                return self._json({"error": "mode 只能是 approve 或 auto"}, 400)
            try:
                task = task_module.Task.load(msf.group(1))
            except (OSError, ValueError, json.JSONDecodeError):
                return self._json({"error": "session not found"}, 404)
            task.safety_mode = mode
            task.save()
            with self.cache._lock:
                self.cache._cache.pop(msf.group(1), None)
            return self._json({"ok": True, "mode": mode})
        mu = re.fullmatch(r"/api/sessions/([^/]+)/undo", path)
        if mu:
            return self._undo_round(mu.group(1))
        mcmd = re.fullmatch(r"/api/sessions/([^/]+)/cmd", path)
        if mcmd:
            return self._local_command(mcmd.group(1),
                                       str(body.get("name") or ""),
                                       str(body.get("arg") or ""))
        mc = re.fullmatch(r"/api/jobs/([^/]+)/cancel", path)
        if mc:
            job = _JOBS.get(mc.group(1))
            if job is None:
                return self._json({"error": "job not found"}, 404)
            if job["status"] not in ("queued", "running"):
                return self._json({"error": "该作业已结束"}, 409)
            job["cancel"] = True
            return self._json({"ok": True})
        ma = re.fullmatch(r"/api/jobs/([^/]+)/answer", path)
        if ma:
            job = _JOBS.get(ma.group(1))
            if job is None or not job.get("pending"):
                return self._json({"error": "no pending question"}, 404)
            st = job.get("_state")
            if st is not None:
                st["answer"] = str(body.get("answer") or "")
            job["answer"] = str(body.get("answer") or "")
            ev = job.get("_ev")
            if ev:
                ev.set()
            return self._json({"ok": True})
        return self._json({"error": "not found"}, 404)

    do_PUT = do_POST
    do_PATCH = do_POST

    def do_DELETE(self):  # noqa: N802
        path = urlparse(self.path).path
        if path == "/api/sessions":
            n = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(n) or b"{}") if n else {}
            except json.JSONDecodeError:
                return self._json({"error": "bad json"}, 400)
            ids = [str(x) for x in (body.get("ids") or [])][:500]
            if not ids:
                return self._json({"error": "ids 必填（要删的会话 id 列表）"}, 400)
            deleted, failed = [], []
            for tid in ids:
                code, payload = self._try_delete(tid)
                if code == 200:
                    deleted.append(tid)
                else:
                    failed.append({"id": tid, "error": payload.get("error", "")})
            return self._json({"ok": True, "deleted": deleted, "failed": failed})
        m = re.fullmatch(r"/api/sessions/([^/]+)", path)
        if m:
            code, payload = self._try_delete(m.group(1))
            return self._json(payload, code)
        return self._json({"error": "not found"}, 404)

    def _create_session(self, body: dict) -> None:
        """新建会话：goal 可空（随对话成形）；工作目录可指定（随任务存续）。"""
        from .tools import safety as _safety
        goal = str(body.get("goal") or "").strip()
        ws_raw = str(body.get("workspace") or "").strip()
        ws_path = None
        if ws_raw:
            ws_path = Path(ws_raw)
            if not ws_path.is_absolute():
                return self._json({"error": "工作目录必须是绝对路径"}, 400)
            try:
                ws_resolved = ws_path.resolve()
            except OSError:
                return self._json({"error": "工作目录无法解析"}, 400)
            if _safety._is_fs_root(ws_resolved):
                return self._json({"error": "工作目录不能是盘根/文件系统根"}, 400)
            try:
                ws_resolved.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                return self._json({"error": f"工作目录无法创建：{e}"}, 400)
        task = task_module.Task.create(goal=goal[:500])
        if ws_path is not None:
            task.workspace = str(ws_path.resolve())
            task.save()
        return self._json({"id": task.id, "workspace": task.workspace}, 201)

    def _try_delete(self, task_id: str) -> tuple[int, dict]:
        """删除会话目录的共享判定（单个与批量共用）；返回 (HTTP 码, 响应体)。"""
        import shutil

        from .cli.session import _process_alive
        if not _ID_SAFE.match(task_id):
            return 404, {"error": "not found"}
        with _job_lock:
            busy = any(j["task_id"] == task_id
                       and j["status"] in ("queued", "running")
                       for j in _JOBS.values())
        if busy:
            return 409, {"error": "该会话有正在运行的轮，先等它结束"}
        tdir = self.tasks_root / task_id
        if not tdir.is_dir():
            return 404, {"error": "session not found"}
        lock = tdir / ".lock"
        if lock.is_file():
            try:
                pid = int(lock.read_text(encoding="utf-8").strip() or 0)
            except (OSError, ValueError):
                pid = 0
            if _process_alive(pid):
                return 409, {"error": f"会话被进程 {pid} 占用（CLI 还开着），先关闭再删"}
        shutil.rmtree(tdir, ignore_errors=True)
        with self.cache._lock:
            self.cache._cache.pop(task_id, None)
        return 200, {"ok": True}

    def _delete_session(self, task_id: str) -> None:
        """删除会话目录；有运行中作业或 CLI 持锁时拒绝。"""
        code, payload = self._try_delete(task_id)
        self._json(payload, code)

    def _start_turn(self, task_id: str, content: str) -> None:
        """追加一轮对话：三道互斥（进程内单飞 / CLI 会话锁 / 任务级去重）。"""
        if not content.strip():
            return self._json({"error": "content 必填"}, 400)
        return self._start_job(task_id, content)

    def _start_resume(self, task_id: str) -> None:
        """/c 续跑开放轮：不注入新消息（对齐 CLI \\继续）。"""
        return self._start_job(task_id, None)

    def _undo_round(self, task_id: str) -> None:
        """/undo：撤销最近一条开放轮（对齐 CLI；已闭合轮不可撤）。"""
        with _job_lock:
            busy = any(j["task_id"] == task_id
                       and j["status"] in ("queued", "running")
                       for j in _JOBS.values())
        if busy or _TURN_GATE.locked():
            return self._json({"error": "轮正在运行，先终止再撤销"}, 409)
        from .agent import MODE_MANAGED
        from .cli.prompt import _build_agent  # 懒导入：避免 cli↔serve 循环依赖
        try:
            task = task_module.Task.load(task_id)
        except (OSError, ValueError, json.JSONDecodeError):
            return self._json({"error": "session not found"}, 404)
        rounds = task.rounds or []
        if not rounds or rounds[-1].get("end_state") not in ("", "open"):
            return self._json(
                {"error": "最后一轮已闭合（有最终回答）——只撤销开放中的轮次"}, 409)
        n = len(rounds[-1].get("events") or [])
        task.rounds.pop()
        agent = _build_agent(task, mode=task.mode or MODE_MANAGED)
        agent.rounds = task.rounds
        agent._persist_rounds()
        with self.cache._lock:
            self.cache._cache.pop(task_id, None)
        return self._json({"ok": True, "removed_events": n})

    def _local_command(self, task_id: str, name: str, arg: str) -> None:
        """/ 命令的文本类输出（maint/report/bg/todo）：纯本地零模型成本。"""
        from . import ui
        if name in ("help", "h", "?", "帮助"):
            return self._json({"text": _WEB_HELP})
        try:
            task = task_module.Task.load(task_id)
        except (OSError, ValueError, json.JSONDecodeError):
            return self._json({"error": "session not found"}, 404)
        if name in ("maint", "维护", "进度"):
            text = ui.maint_view(task)
        elif name in ("report", "报告"):
            from .cli.session import _child_summaries
            text = ui.report_view(task, _child_summaries(task_id))
        elif name in ("bg", "后台"):
            from .tools.background import (check_background, list_background,
                                           stop_background)
            parts = arg.split()
            if parts and parts[0].isdigit():
                parts[0] = f"bg-{parts[0]}"        # bg 1 == bg bg-1
            if len(parts) >= 2 and parts[0].lower() == "stop":
                target = parts[1]
                if target.isdigit():
                    target = f"bg-{target}"
                text = stop_background(target)
            elif len(parts) == 1:
                text = check_background(parts[0])
            else:
                text = list_background()
        elif name in ("todo", "阶段", "计划"):
            text = "\n".join(task.todo_lines())
        else:
            return self._json({"error": f"未知命令 {name}"}, 400)
        return self._json({"text": text})

    def _start_job(self, task_id: str, content: str | None) -> None:
        data = self._load_task(task_id)
        if data is None:
            return self._json({"error": "session not found"}, 404)
        with _job_lock:
            busy = any(j["task_id"] == task_id
                       and j["status"] in ("queued", "running")
                       for j in _JOBS.values())
            if busy or _TURN_GATE.locked():
                return self._json({"error": "上一轮还在运行，稍后再发"}, 409)
            jid = f"j{next(_JOB_SEQ)}"
            _JOBS[jid] = {"job_id": jid, "task_id": task_id,
                          "status": "queued"}
        threading.Thread(target=_execute_turn,
                         args=(jid, task_id, content), daemon=True).start()
        return self._json({"job_id": jid}, 202)


def cmd_serve(args: argparse.Namespace) -> None:
    """wovra serve：起本地只读可视化服务（前端 = webui/index.html）。"""
    cache = SummaryCache(task_module.TASKS_ROOT)
    _Handler.cache = cache
    _Handler.tasks_root = task_module.TASKS_ROOT
    stop = threading.Event()
    threading.Thread(target=_warm, args=(cache, stop), daemon=True).start()
    server = ThreadingHTTPServer((args.host, args.port), _Handler)
    print(f"Wovra 可视化：http://{args.host}:{args.port}/"
          f"（只读投影；Ctrl+C 停止。数据目录：{task_module.TASKS_ROOT}）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        stop.set()
        server.server_close()
        print("已停止。")
