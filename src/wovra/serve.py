"""wovra serve：任务现场的只读 HTTP 投影（前端可视化的接口层）。

设计契约见 docs/frontend-visualization-plan.md §3。三条铁律：
* **会话数据 GET-only**——可视化不改会话状态，任何状态变更都走 CLI 交互通道；
  唯一的写通道是"新建会话 / 追加一轮对话"，以及 **`/api/settings`**（写仓库根
  `.env`，改的是运行参数、不碰任何 task.json）；
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
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import registry as registry_module
from . import task as task_module
from . import tokens as tokens_module
from . import views as views_module
from .tools import eyes as eyes_module


def _content_text(content) -> str:
    """消息内容的纯文本投影（图片只留一行说明，不带 base64）。

    2026-09-13（眼睛）：装配尾部可能有图片 parts。前端 JSON 里塞 base64
    等于把几百万字符发给浏览器且毫无用处——只投影文字说明。
    """
    return eyes_module.content_to_text(content)

_WEBUI = Path(__file__).resolve().parents[2] / "webui" / "index.html"
_STARTED = time.strftime("%Y-%m-%d %H:%M:%S")
_ID_SAFE = re.compile(r"^[0-9A-Za-z_-]+$")
_LLM_CALL = re.compile(r"^\[(\w+)\]\s+(.*)$")
_KV = re.compile(r"(\w+)=([\d,]+(?:\.\d+)?)")
_FINISH = re.compile(r"finish=(\S+)")
_USAGE_HIT = re.compile(r"缓存命中 ([\d,]+) tok")
_USAGE_MISS = re.compile(r"未命中 ([\d,]+) tok")
# 落账行尾的**按调用方分账**段（core._by_agent_segment 写的）：
# ` by=[Main steps=12 prompt=1000 cached=900 miss=100 completion=50] [A …]`
# 数字容忍千分位（写侧不带，读侧宽容——历史/手改都不至于静默丢段）。
#
# **名字允许含空格**（2026-09-13 修）：域名是模型起的中文短语，实测
# `web 网络工具（web_fetch / web_search 实现与回归）`、`LLM 网络检索方案调研
# （外部开源方案选型）` 都带空格——原正则用 `[^\]\s]+` 取名字，于是这些段**整条
# 匹配不上被静默丢掉**：会话 20260913-151842-2628dd 的 R8 记录里明明有两段
# （Main + 调研域），投影出来只剩 Main；R7 更是整条丢空 → 页面上只剩一个汇总，
# 用户看到的"多 agent 轮只显示汇总"就是这个。
# 只在 `by=` 之后的尾巴里匹配，避免撞上轮级前缀（` round=7 steps=43 …`）。
_USAGE_BY_AGENT = re.compile(
    r"\[(.+?) steps=([\d,]+) prompt=([\d,]+) cached=([\d,]+)"
    r" miss=([\d,]+) completion=([\d,]+)\]"
)


def parse_usage_row(detail: str) -> dict | None:
    """解析轮尾 usage 落账行（steps/context/prompt/completion + 中文命中段）。

    行尾可能带**按调用方分账**段（`core._by_agent_segment`）：一次调用落一次账，
    键是那一刻的执行方。轮的总消费 = 各调用方之和（A 300K + B 400K = 700K），
    故这里把它解析成 `by_agent`（老会话的落账行没有这段 → 空 dict，不编数）。
    """
    out: dict = {}
    # **先切掉分账段再解析轮级键**（2026-09-13 修）：`by=[… prompt=167709 …]` 里
    # 也有 `prompt=`/`cached=`/`miss=`/`completion=` 这些**同名键**，而 `_KV` 是
    # 全行扫描、后写的覆盖先写的——于是轮级数字被**最后一段分账**顶掉。
    # 实测 R8：轮总本来 404,157，页面上显示成 236,448（= 最后那一段），
    # 会话级的 Σprompt 也跟着少算。
    text = detail or ""
    cut = text.find("by=")
    head = text[:cut] if cut >= 0 else text
    for k, v in _KV.findall(head):
        v = v.replace(",", "")
        out[k] = float(v) if "." in v else int(v)
    if "prompt" not in out:
        return None
    mh = _USAGE_HIT.search(head)
    mm = _USAGE_MISS.search(head)
    out["cached"] = int(mh.group(1).replace(",", "")) if mh else 0
    out["miss"] = int(mm.group(1).replace(",", "")) if mm else 0
    by: dict[str, dict] = {}
    tail = text[cut + 3:] if cut >= 0 else ""      # 只解析分账段
    for m in _USAGE_BY_AGENT.finditer(tail):
        nums = [int(v.replace(",", "")) for v in m.groups()[1:]]
        by[m.group(1)] = {
            "steps": nums[0], "prompt": nums[1], "cached": nums[2],
            "miss": nums[3], "completion": nums[4],
        }
    out["by_agent"] = by
    return out


def _blank_usage() -> dict:
    return {"steps": 0, "calls": 0, "prompt": 0, "cached": 0, "miss": 0,
            "completion": 0, "context": 0, "by_agent": {}}


def _merge_usage(agg: dict, row: dict) -> None:
    agg["steps"] += row.get("steps", 0)
    agg["calls"] += 1
    for k in _USAGE_KEYS:
        agg[k] += row.get(k, 0)
    agg["context"] = max(agg["context"], row.get("context", 0))
    for name, c in (row.get("by_agent") or {}).items():
        b = agg["by_agent"].setdefault(
            name, {"steps": 0, "prompt": 0, "cached": 0, "miss": 0, "completion": 0}
        )
        for k in ("steps", "prompt", "cached", "miss", "completion"):
            b[k] += c.get(k, 0)


def round_open_times(rounds: Iterable[dict]) -> list[tuple[int, str]]:
    """每轮的**开轮时刻**（首事件时间；没有事件的轮沿用上一轮的，便于分桶）。"""
    out: list[tuple[int, str]] = []
    last = ""
    for r in rounds:
        if not isinstance(r, dict):
            continue
        evs = r.get("events") or []
        t0 = ""
        for e in evs:
            if isinstance(e, dict) and e.get("timestamp"):
                t0 = str(e["timestamp"])
                break
        last = t0 or last
        out.append((int(r.get("seq") or 0), last))
    return out


# 只有**真的 ISO 时刻**才拿去做时间分桶：老数据/手改文件里出现过 "t3" 这类
# 占位值，字符串比较会把它们全判给最后一轮（实测把整会话的账并进一轮）。
_ISO_TIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")


def round_usage_map(data: dict) -> dict[int, dict]:
    """每轮的用量账（**按轮号精确归属**，三种口径按优先级）：

    1. **行里的轮号**（`round=n`，`core._usage_record_and_drain` 新写入）——精确；
    2. **开轮时刻分桶**（老账没有轮号时）：一条落账行属于"最后一个已开轮的轮"。
       轮按时间顺序且不重叠，故这条机械规则不会歧义，且天然支持一轮多行
       （中断写一行、续跑闭合再写一行）；
    3. **步数签名逐轮吞行**（没有时间戳可用的老数据，即历史实现）——
       保留为最后退路：它依赖 `steps_used` 与实际步数一致，一旦不符整条链错位。

    为什么换掉纯签名口径：实测 `20260912-181611-886f42` 的 R12（旧检查点轮）
    `steps_used=13` 而实际 1 个事件，签名法在 R10 多吞一行后整条链错位一格
    ——末轮 R13 一行都没分到，页面上就没有成本格。

    一轮的总消费 = 各调用方之和（`by_agent`，实记）；`context` = 轮内峰值。
    """
    rounds = [r for r in (data.get("rounds") or []) if isinstance(r, dict)]
    rows: list[dict] = []
    for h in data.get("history") or []:
        if h.get("kind") != "usage":
            continue
        row = parse_usage_row(h.get("detail", ""))
        if row:
            row["time"] = str(h.get("time") or "")   # 分桶要用（行里的时间在 history 上）
            rows.append(row)
    out: dict[int, dict] = {}
    legacy: list[dict] = []
    # ① 行里带轮号 → 精确归属
    for row in rows:
        seq = row.get("round")
        if isinstance(seq, (int, float)) and int(seq) > 0:
            _merge_usage(out.setdefault(int(seq), _blank_usage()), row)
        else:
            legacy.append(row)
    if legacy:
        starts = round_open_times(rounds)
        timed = [r for r in legacy if _ISO_TIME.match(str(r.get("time") or ""))]
        untimed = [r for r in legacy if not _ISO_TIME.match(str(r.get("time") or ""))]
        # ② 开轮时刻分桶（有时间戳就一行都不会错位）
        if timed and any(t for _seq, t in starts):
            for row in timed:
                t = str(row.get("time") or "")
                owner = 0
                for seq, t0 in starts:
                    if t0 and t0 <= t:
                        owner = seq
                if owner:
                    _merge_usage(out.setdefault(owner, _blank_usage()), row)
        else:
            untimed = timed + untimed        # 没有可用时间戳 → 全走签名
        # ③ 最后退路：步数签名逐轮吞行（老实现，保留）
        if untimed:
            it = iter(untimed)
            for r in rounds:
                seq = int(r.get("seq") or 0)
                if seq in out:
                    continue
                target = r.get("steps_used") or 0
                agg = out.setdefault(seq, _blank_usage())
                acc = 0
                while acc < target:
                    row = next(it, None)
                    if row is None:
                        break
                    acc += row.get("steps", 0)
                    _merge_usage(agg, row)
    return {k: v for k, v in out.items() if v["calls"]}


# （原 `attributed_seqs()`：用正则从 history 的 route 行里解析"渐近归属补判过的
#   轮"。已删除——渐近归属现在直接体现为派生账的「答复轮 vs 承载轮」两个字段，
#   不必再解析日志文本：材料的归属由块归属给出，答话的归属由事件流给出。）


def agent_cost_map(data: dict) -> dict[str, dict]:
    """**按调用方**的消费账（2026-09-12 用户口径：「谁花的，消费时就记录啊」）。

    来源是落账行尾的分账段（每次调用发生时即归到那一刻的执行方），故这是
    **实记**，不是事后按轮推断：一轮里主 agent 走路由那一步、接手方干剩下的，
    同轮两个 agent 各记各的（A 300K + B 400K = 轮总 700K）。

    历史会话（分账段之前落的账）没有这段 → 该轮只有总数、没有分账，
    消费方据此显示"未分账"，不编数。
    """
    out: dict[str, dict] = {}
    for agg in round_usage_map(data).values():
        for name, c in (agg.get("by_agent") or {}).items():
            b = out.setdefault(name, {
                "steps": 0, "calls": 0, "prompt": 0, "cached": 0, "miss": 0,
                "completion": 0,
            })
            b["calls"] += 1          # 一段分账 = 一行落账（一次结算）
            for k in ("steps", "prompt", "cached", "miss", "completion"):
                b[k] += c.get(k, 0)
    return out


def agent_stats(
    data: dict, main_id: str = "", registry: list | None = None
) -> list[dict]:
    """按 agent 聚合：**名下的轮（R 号）/ 步 / 消费**（全部按用户口径）。

    * **轮**：落点归属（"这轮被附加到谁的上下文"），只记新轮；已压缩段单列
      （`round_account`）。显示一律用总轮 R 号 —— `seqs` 是号清单，
      `first`/`last` 供"计数 + 首末范围"。
    * **步**：归执行它的那个 agent（同轮可分属两家），走事件流按 `route_to`
      转交点分段。
    * **消费**：实记（`agent_cost_map`，落账时就带执行方）；历史轮没有分账段
      时该 agent 的消费为 0 且 `cost_known=False`，不编数。
    * 整理/压缩/分裂开销**不在这里**：那是运行时账（`org=`/`compaction=`），
      不摊给任何 agent。
    """
    main_id = main_id or registry_module.MAIN_AGENT_ID
    rounds = [r for r in (data.get("rounds") or []) if isinstance(r, dict)]
    reg = registry if registry is not None else (data.get("registry") or [])
    domains = registry_module.latest_domains(rounds)
    ledger = views_module.agent_ledger(rounds, domains, reg)
    account = views_module.round_account(rounds, domains, reg)
    cost = agent_cost_map(data)

    per: dict[str, dict] = {}
    order: list[str] = []

    def bucket(name: str) -> dict:
        if name not in per:
            per[name] = {
                "agent": name, "id": "", "display": name,
                "rounds": 0, "seqs": [], "first": 0, "last": 0,
                "steps": 0, "handoffs": 0,
                "prompt": 0, "cached": 0, "miss": 0, "completion": 0,
                "cost_known": False, "ctx_peak": 0, "ctx_cur": 0,
                "window": 0, "share": 0.0,
            }
            order.append(name)
        return per[name]

    seen_names: set[str] = set()
    for rec in ledger.values():
        if rec["name"] in seen_names or rec["name"] == "":
            continue
        seen_names.add(rec["name"])
        b = bucket(rec["name"])
        b["id"] = rec["id"]
        b["display"] = rec["display"]
        b["rounds"] = rec["rounds"]
        b["seqs"] = list(rec["seqs"])
        b["first"] = rec["first"]
        b["last"] = rec["last"]
        b["steps"] = rec["steps"]
        b["handoffs"] = rec["handoffs"]
        b["ctx_cur"] = rec["ctx_cur"]
        b["ctx_peak"] = rec["ctx_peak"]
        b["window"] = rec["window"]
        b["share"] = rec["share"]

    for name, c in cost.items():
        b = bucket(name)
        b["cost_known"] = True
        b["prompt"] = c["prompt"]
        b["cached"] = c["cached"]
        b["miss"] = c["miss"]
        b["completion"] = c["completion"]
        b["billed_calls"] = c["calls"]
        # 分账的步数是**实记**（含空响应重试）；事件流口径只数到真的执行步。
        # 两者不同源时以实记为准（那才是真正发生的调用次数）。
        if c["steps"] > b["steps"]:
            b["steps"] = c["steps"]

    usage = round_usage_map(data)
    for r in rounds:
        agg = usage.get(r.get("seq")) or {}
        if not agg.get("calls"):
            continue
        b = bucket(_main_or_landing(r, ledger, main_id))
        b["billed_rounds"] = int(b.get("billed_rounds") or 0) + 1
        b["ctx_peak"] = max(int(b.get("ctx_peak") or 0), agg.get("context", 0))

    out = []
    for name in order:
        b = per[name]
        calls = int(b.get("billed_calls") or b.get("billed_rounds") or 0)
        b["avg_prompt"] = (int(b.get("prompt") or 0) // calls) if calls else 0
        out.append(b)
    out.sort(key=lambda x: (-int(x.get("rounds") or 0), str(x.get("agent"))))
    return out


def _main_or_landing(r: dict, ledger: dict, main_id: str) -> str:
    """该轮的落点名（用于把轮级用量挂到名下；查不到就给主 agent）。"""
    try:
        seq = int(r.get("seq") or 0)
    except (TypeError, ValueError):
        return main_id
    for rec in ledger.values():
        if seq in (rec.get("seqs") or []):
            return str(rec.get("name") or main_id)
    return main_id




# ---- C4：受控写通道的作业系统 --------------------------------------------
# 唯一的写形态 = 新建会话 / 给会话追加一轮对话。轮执行复用 CLI 的 agent
# 管线（懒导入避免 cli↔serve 循环依赖）。互斥三道：
#   1. _TURN_GATE——serve 进程内同时只跑一轮（本地单用户）；
#   2. CLI 会话锁文件——与正在运行的 chat/run 进程互斥；
#   3. 任务级 job 去重——同一会话不叠加排队。
_JOBS: dict[str, dict] = {}
_JOB_SEQ = itertools.count(1)
_TURN_GATE = threading.Lock()
# 安全模式覆盖表（会话 id → approve/auto）：切换按钮**运行中**改档时写这里，
# 闸门每次判定都先读它——运行中的 job 握着独立 Task 对象，光落盘改不到它。
_SAFETY_OVERRIDE: dict[str, str] = {}
_job_lock = threading.Lock()


def _ask_timeout() -> int:
    """问用户的等待上限（秒）——调用期读环境，配置面板改完当场生效。"""
    try:
        return max(10, int(float(os.environ.get("WOVRA_ASK_TIMEOUT", "1800"))))
    except (TypeError, ValueError):
        return 1800


def _round_seq_for(agent, content: str | None) -> int:
    """**开轮之前**先把这一轮的 seq 估出来（0 = 估不出来，不编数）。

    为什么要提前估（2026-09-13 用户实测的 bug）：前端一亮相就要把直播内容挂进
    "本轮的消息块"，而它读轮号的时机是 POST 返回后**立刻**那次 catch-up——那时
    一个分片都还没有。原先只在第一个分片到达时才报轮号，于是 round=0：前端整轮
    都找不到本轮正式块，临时块一直挂在正式块下面，同一轮的思考被复制一份，
    头部还显示成历史老轮的"无落点"。

    公式与开轮处（`agent/core.py`：`seq = len(self.rounds) + 1`）一致；`/c`
    续跑则取那个**开放轮**（没有开放轮就给 0，不瞎猜）。
    """
    try:
        rounds = list(getattr(agent, "rounds", None) or [])
    except TypeError:
        return 0
    if content is None:
        last = rounds[-1] if rounds else {}
        opened = str(last.get("end_state") or "") in ("", "open")
        try:
            return int(last.get("seq") or 0) if opened else 0
        except (TypeError, ValueError):
            return 0
    return len(rounds) + 1


def _build_turn_agent(task):
    """这一轮用的 agent：**整理异步**（与 chat 同款，见 worklog §86）。

    **为什么必须异步**（用户 2026-09-13 当场质疑："整理、分裂不是异步吗？怎么会
    卡我和 AI 对话呢？"）：整理 + 分裂是真发两次 LLM 调用的（实测一批 org 75s +
    split 31s = **106 秒**，硬上限 900s）。`close_round()` 里就会触发它：

    * `async_organization=False`（原先 serve 就是这么配的）→ 在**这一轮的作业
      线程里同步跑完**才返回。于是那 106 秒里：`job["status"]` 一直是 `running`
      （前端停在"回答中…"、发送按钮按不动）、`_TURN_GATE` 一直握着（再发一条就
      是 409「上一轮还在运行」）—— **整轮对话被维护挡住**；
    * `async_organization=True` → 批次进 `_org_queue`，后台线程跑，`close_round`
      立刻返回。维护跑完自己 `_settle_after_maintenance()` 让产物落地，而它与
      `_open_or_reuse_round` 共用 `_view_lock`，**两者不会交错**（这是 chat 模式
      一直在用的同一条路）。用户因此可以立刻说下一句。

    同步模式仍然属于**一次性进程**（`wovra run`：退出前必须把账补齐，
    `cli/main.py` 就是这么配的）——serve 是长驻进程，不该学它。
    """
    from .agent import MODE_MANAGED
    from .cli.prompt import _build_agent  # 懒导入：避免 cli↔serve 循环依赖
    return _build_agent(task, mode=task.mode or MODE_MANAGED,
                        async_organization=True)


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
        got = ev.wait(_ask_timeout())
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
        # 模式每次都**现读**：先看进程级覆盖表（切换按钮运行时写的），再退回
        # 本 job 手里那份 Task——否则"运行中切自主模式"永远不生效（实测 bug）
        mode = str(
            _SAFETY_OVERRIDE.get(str(getattr(task, "id", "")) or "")
            or getattr(task, "safety_mode", "approve") or "approve"
        )
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
        # 工作区**不在这里**动进程全局：`_build_turn_agent`（下面的
        # `_build_agent`）会把本会话的工作区绑到**本轮这个线程**上。
        # 2026-09-15 修掉的正是"在这里改全局"——serve 每轮的线程与每个
        # HTTP 请求线程并发，另一个标签页点开别的会话就会把本轮的文件世界
        # 换掉（read_file 解析到别的目录，实测报障 fd7962）。
        with _TURN_GATE:
            _acquire_session_lock(task)  # 被占用时抛 SystemExit（CLI 语义）
            try:
                agent = _build_turn_agent(task)
                # **轮号立刻报出去**（2026-09-13 用户实测的 bug）：见 `_round_seq_for`
                job["round"] = _round_seq_for(agent, content)

                def _live(chunk: dict) -> None:
                    # 轮直播流（思考/回答增量、步骤状态）。单消费者轮询读，
                    # append 原子足够；量级 = 单轮流式分片，无需封顶。
                    # 轮号以 agent 自己开的那个轮为准（上面那个是提前量的估计，
                    # 这里用权威值覆盖；正常情况两者相同）。
                    cur = getattr(agent, "current_round", None) or {}
                    if cur.get("seq"):
                        job["round"] = int(cur["seq"])
                    # **现在是谁在干**（2026-09-13 用户报："下面 agent 的思考内容
                    # 老是挂到上面主 agent 中"）：一轮里若有多名 agent（主 agent
                    # 路由 → 子 agent 接手），事件区会给**每个 agent 各建一个消息
                    # 块**，前端必须知道该把直播内容挂进哪一个。这里现取现报
                    # （`route_to` 换手后 `active_view` 就变成接手方）。
                    try:
                        chunk["ag"] = str(agent._active_view() or "")
                    except Exception:  # noqa: BLE001——报不出身份不该断直播
                        pass
                    # **步的机械锚点**（2026-09-13 修，用户报"正文留在尾部越堆越多"）：
                    # 前端的"在飞正文/思考"缓冲原先靠"事件到达就清空"收口——那是把正确性
                    # 押在**事件时序**上：事件若延迟/丢失/乱序，上一段就会留在尾部堆积。
                    # 这里报"已落事件数"：一步之内不变，**落一个事件就 +1**，前端只认这个
                    # 号——步号一变就清缓冲，不依赖任何事件是否到达。
                    try:
                        chunk["st"] = len((getattr(agent, "current_round", None) or {})
                                          .get("events") or [])
                    except Exception:  # noqa: BLE001
                        pass
                    job["live"].append(chunk)

                agent.on_progress = lambda s: _live({"k": "status", "s": s})
                job["live"] = []
                # **事件级直播**（2026-09-13，worklog §92）：每落一个事件（工具调用/
                # 工具结果/最终回答/运行时提示）就推一份扁平副本，形状与
                # `/api/sessions/{id}/rounds/{seq}` 给前端的完全一致——前端因此能用
                # **同一套"事件 → 条目"映射**按时间顺序画，工具卡不再缺席。
                # 此前直播只推 think/ans/status 文本增量，运行中看不到工具调用，
                # 表现就是"思考连一块、中间的工具块没有"。
                agent.on_event = lambda e: _live({"k": "event", "e": e})
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
                # **整理是后台的事**（§86）：`close_round()` 里已经把它排进
                # `_org_queue` 并立刻返回，这里再调一次只是兜住"上一批被推迟"
                # 的情形——异步模式下它同样**不阻塞**（排队即返回）。
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


def project_tree(workspace: str, data: dict | None = None) -> dict:
    """会话工作区的文件树（扁平相对路径列表，前端建树）。

    排除重型/生成目录（.git、node_modules、虚拟环境、构建产物…）；
    条数上限 _TREE_MAX，超出标注 truncated（大仓不拖垮页面）。

    2026-09-12 加三样（前端项目页要用）：`owner`（哪个域认领它——"文件标签"）、
    `state`（LIVE/只读/已删的历史状态，来自现场重算的 ledger）、`desc`（它的
    块摘要，没有则退回所属域的职责描述）。`tagged` = 有归属（前端把有标签的排最前）。
    """
    root = Path(workspace or "")
    if not workspace or not root.is_dir():
        return {"root": workspace, "files": [], "error": "工作目录不存在"}
    registry = list((data or {}).get("registry") or [])
    rounds = list((data or {}).get("rounds") or [])
    # 状态：现场重算 ledger（不信落盘状态）
    states: dict[str, str] = {}
    summaries: dict[str, str] = {}
    if data is not None:
        try:
            from . import blocks as blocks_module
            from . import lifecycle as lifecycle_module

            ledger = lifecycle_module.FileLedger()
            for r in rounds:
                if not isinstance(r, dict):
                    continue
                blocks = blocks_module.segment_round_by_file(r)
                ledger.update(r, blocks=blocks)
                marks = r.get("block_summaries") or {}
                for b in blocks:
                    f = str(b.get("file") or "")
                    if f and str(b.get("kind") or "") == "file" and marks.get(b["id"]):
                        summaries[f] = str(marks[b["id"]])      # 后者覆盖前者（更新）
            for path, e in ledger.entries().items():
                states[str(path)] = str(e.get("state") or "")
        except Exception:  # noqa: BLE001——树不能因为账本算不出来就整页崩
            states, summaries = {}, {}
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
            rel = str(p.relative_to(root)).replace("\\", "/")
            owner = ""
            owner_entry: dict = {}
            for e in registry:
                if isinstance(e, dict) and e.get("name"):
                    if registry_module.file_owned_by(e, rel):
                        owner = str(e.get("name"))
                        owner_entry = e
                        break
            files.append({
                "p": rel,
                "s": st.st_size,
                "owner": owner,                              # 文件标签（哪个域维护）
                "tagged": bool(owner),
                "state": states.get(rel, ""),                # live / dead / read_only
                # 一句话描述（≤30 字，agent 写的优先）→ 退回块摘要
                "desc": (str((owner_entry.get("file_notes") or {}).get(rel) or "")
                         or summaries.get(rel, ""))[:30],
            })
            if len(files) >= _TREE_MAX:
                truncated = True
                break
        if truncated:
            break
    # 有标签的排最前（同组按路径），前端就不必再排一遍
    files.sort(key=lambda x: (0 if x["tagged"] else 1, x["p"].lower()))
    return {"root": str(root), "files": files, "truncated": truncated}


def fs_list(path: str | None) -> dict:
    """目录浏览（新建会话选工作目录用）：只列子目录，只读。

    path 为空 → 列盘根（Windows）/文件系统根（POSIX）。

    **`sep` 一并报出去**（2026-09-15 修）：前端要拿它拼子目录路径。原先前端写死
    反斜杠，Linux 上点一个文件夹就请求 `/home/lkf\\foo` → 服务端判"目录不存在"
    （用户报："点文件夹时一直提示目录不存在，无法选择工作路径"）。
    """
    if not path:
        if os.name == "nt":
            roots = []
            for L in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
                if Path(f"{L}:\\").exists():
                    roots.append(f"{L}:\\")
            return {"roots": roots, "dirs": [], "path": "", "sep": os.sep}
        return {"roots": ["/"], "dirs": [], "path": "", "sep": os.sep}
    p = Path(path)
    if not p.is_absolute() or not p.is_dir():
        return {"roots": [], "dirs": [], "path": str(p), "sep": os.sep,
                "error": "目录不存在"}
    dirs = []
    try:
        for child in p.iterdir():
            try:
                if child.is_dir():
                    dirs.append(child.name)
            except OSError:
                continue
    except OSError as e:
        return {"roots": [], "dirs": [], "path": str(p), "sep": os.sep,
                "error": str(e)}
    return {"roots": [], "dirs": sorted(dirs, key=str.lower),
            "path": str(p), "sep": os.sep}


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


def tool_call_count(history: list[dict] | None) -> int:
    """工具调用次数（history 里 `tool_call` 行**逐调用**落账，含并行批次）。

    顶部六格用它替代原来的"总步数"（2026-09-15 用户口径）：一步（= 一次
    LLM 交互）可以并行调多个工具，`工具调用 / LLM 调用` 的比值就是并行度；
    另外"步"与"LLM 调用"本来就基本一致（一次交互算一步），两格重复。
    """
    return sum(1 for h in history or [] if h.get("kind") == "tool_call")


def usage_totals(history: list[dict] | None) -> dict:
    """从 history 的 llm_call 行汇总用量，并保留逐行序列（前端画图用）。

    `ttft_work_sum` / `ttft_work_n` = **干活轮**的 TTFT 有效样本（均值用）。
    为什么要单列（2026-09-15 用户报"TTFT 虚大"）：整理/分裂的批量调用输入
    8–20 万 tok，首字延迟本来就有 15–123s（实测 orgtest 一条 organization
    ttft=123.3s），和干活轮的 ~4s 平均在一起会把均值抬飞——那个数该回答
    "我这轮等多久"，不是"后台批量整理需要多久"。没有首字的调用（`nofirst=1`）
    不是延迟样本，一并不计。
    """
    totals: dict = {"calls": 0, "prompt": 0, "cached": 0, "miss": 0,
                    "completion": 0, "ttft_sum": 0.0, "finish": {}, "rows": [],
                    "ttft_work_sum": 0.0, "ttft_work_n": 0, "nofirst": 0}
    for h in history or []:
        if h.get("kind") != "llm_call":
            continue
        row = parse_llm_call(h.get("detail", ""))
        if not row:
            continue
        totals["calls"] += 1
        for k in _USAGE_KEYS:
            totals[k] += row.get(k, 0)
        if row.get("nofirst"):
            totals["nofirst"] += 1
        else:
            totals["ttft_sum"] += row.get("ttft", 0.0)
            if row.get("purpose") == "working":
                totals["ttft_work_sum"] += row.get("ttft", 0.0)
                totals["ttft_work_n"] += 1
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


# 维护硬上限 900s（`_org_maint_timeout`）+ 5 分钟余量：超过这么久还没有「结束：」
# 就认为那次维护没正常收尾（进程被杀），界面不再显示"进行中"。
_MAINT_STALE_AFTER = 900 + 300


def maint_state(data: dict) -> dict:
    """维护（整理/分裂）在不在跑、跑到哪一步、多久了、上次结果是什么。

    为什么要它（2026-09-15 用户报"没有 UI 进度提示"）：维护跑在**后台线程**里，
    轮一闭合作业就结束了——前端原来的 `/plan` 轮询以"有作业在跑"为前提，于是
    整理 + 分裂这一两分钟里页面**一点反馈都没有**（实测一批 89s：org 57s +
    split 32s，用户看到的就是"卡住了"）。

    状态全部**从盘上现取**（零新机制、零 LLM）：
    * 轮的 `org_state == "pending"` → 整理中；`split_state ∈ {running, ready,
      deferred}` → 分裂中（这两个字段本来就是维护线程写的）；
    * history 里最后一条「启动：批次 …」（其后没有「结束：」）→ 在跑，并给出
      起始时刻算已耗时；**V4 也写这同一对**（2026-09-18 补：之前 V4 不写，
      前端维护进度条在 V4 下从不亮起）；
    * 最后一条 `split_defect`/`split_stale`（旧链路）或**轮上的
      `split_state=failed`＋`split_note`**（V4 的持久来源，抗并发写丢 history 行）
      → 上次结果（**失败原因必须看得见**：以前它只落在 history 里，页面上只有
      一个小徽标）。
    """
    rounds = [r for r in (data.get("rounds") or []) if isinstance(r, dict)]
    pending = [r.get("seq") for r in rounds
               if str(r.get("org_state") or "") == "pending"]
    splitting = [r.get("seq") for r in rounds
                 if str(r.get("split_state") or "") in ("running", "ready", "deferred")]
    last_defect = ""
    # V4 分裂失败的**持久来源**（轮字段抗并发写丢行；history 只是旁证）
    for r in rounds:
        if str(r.get("split_state") or "") == "failed":
            note = str(r.get("split_note") or "").strip()
            last_defect = f"分裂失败：{note}" if note else "分裂失败（无原因记录）"
            break
    since = ""
    batch = ""
    last_end = ""
    finished_at = ""
    for h in data.get("history") or []:
        if not isinstance(h, dict):
            continue
        kind = str(h.get("kind") or "")
        detail = str(h.get("detail") or "")
        if kind == "maintenance" and detail.startswith("启动：批次"):
            since, batch, last_end, finished_at = str(h.get("time") or ""), detail, "", ""
        elif kind == "maintenance" and detail.startswith("结束："):
            # `last_end` 存**原文**（界面直接显示"结束：org=True split=True"），
            # 时间另存 `finished_at`
            last_end, finished_at, since = detail, str(h.get("time") or ""), ""
        elif kind in ("split_defect", "split_stale"):
            last_defect = detail
        elif kind == "split" and ("失败" in detail or "异常" in detail):
            last_defect = detail
    elapsed = 0
    if since:
        try:
            delta = datetime.now() - datetime.fromisoformat(since)
            elapsed = max(0, int(delta.total_seconds()))
        except ValueError:
            elapsed = 0
    # **未收尾的启动不等于"正在跑"**（2026-09-15 做 README 截图时实测到的）：维护
    # 跑在后台线程里，进程被杀（重启/崩溃）就不会写「结束：」——旧逻辑于是让界面
    # 永远显示"分裂中…已 1380s"。判据：超过维护硬上限（900s）+ 余量还没结束 →
    # 视为**上次没正常收尾**，不再显示进行中（给出说明比一个假的进度条诚实）。
    if since and elapsed > _MAINT_STALE_AFTER:
        return {"active": False, "phase": "", "since": since, "elapsed": elapsed,
                "org_pending": pending, "splitting": splitting, "batch": batch,
                "last_end": last_end or "（上次维护没有正常收尾：进程可能被重启/中断）",
                "finished_at": finished_at, "last_defect": last_defect, "stale": True}
    phase = ""
    if since:
        # V4 的"整理"阶段：结算还没落（闭合轮里还有没写段话的）也算整理中，
        # 不只是旧链路的 `org_state == "pending"`
        unsettled = [r for r in rounds
                     if str(r.get("end_state")) == "completed"
                     and str(r.get("note_state")) != "done"]
        phase = ("分裂" if splitting
                 else ("整理" if (pending or unsettled) else "收尾"))
    return {"active": bool(since), "phase": phase, "since": since,
            "elapsed": elapsed, "org_pending": pending, "splitting": splitting,
            "batch": batch, "last_end": last_end, "finished_at": finished_at,
            "last_defect": last_defect, "stale": False}


def todo_log(data: dict, limit: int = 60) -> list[dict]:
    """todo 工具调用流水（从轮事件机械派生，零新口径）。

    计划页要能看到"模型对计划做了什么"——check_item / verify_stage 这些动作
    此前只落在事件里，页面上无处可看。动作名 v2（§53：阶段/工作项）与旧名
    别名都会出现在流水里，页面的 ACT 映射表两种都认。
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


def _round_open(data: dict) -> bool:
    """末轮是不是还开着（＝"这一轮还没干完"）——"在跑"的两个事实之一。

    另一端是"服务端有活动作业"（`live_job`），由 `/api/sessions/<id>` 在拿到作业表
    之后合并进 `running`（见那里的注释）。注意**别读 `status` 字段**：V4 停用整理后
    没人写它（`is_done` 永远不来），它停在 `in_progress` 会把"在跑"永久点亮。
    """
    rounds = data.get("rounds") or []
    last = rounds[-1] if rounds else None
    if not isinstance(last, dict):
        return False
    return str(last.get("end_state") or "") in ("", "open")


def session_summary(task_id: str, data: dict) -> dict:
    """会话摘要（列表视图用；小对象，常驻缓存）。"""
    rounds = data.get("rounds") or []
    org = {"done": 0, "pending": 0, "failed": 0, "raw": 0}
    # 维护在不在跑 = 判 pending 的**上下文事实**（见 `_note_pending_for`）：
    # 没在跑时"字还没写出来"只表示"水位没到"，不是"整理中"。
    maint_running = bool(maint_state(data).get("active"))
    for r in rounds:
        # **V4 折算**（2026-09-18 实测：列表/摘要的"已整理 N · 未整理 M"原先直读
        # 轮上的 `org_state` 原字段，而 V4 折叠不写它 → 折过的轮全被数成"未整理"。
        # 与时间线的轮徽标同一套折算（`_org_state_of`），口径不再走岔。）
        state = _org_state_of(r, maint_running)
        org[state if state in _ORG_STATES else "raw"] += 1
    ts = data.get("task_state") or {}
    todo = data.get("todo") or {}
    ms = todo.get("milestone") or {}
    return {
        "id": task_id,
        "goal": data.get("goal", ""),
        "status": data.get("status", ""),
        # **"在跑"由事实派生**（2026-09-18 用户："状态不是闭合吗？咋还是跑轮"）：
        # `status` 字段唯一的写入点是整理产物带 `is_done=True`，而 **V4 已停用整理
        # 那一路** → 它永远停在 `in_progress`。前端据此判"运行中"，于是：① 顶部
        # 永远显示「● 运行中」；② 轮询那条 `if(META.status!=='in_progress') return`
        # 一直放行，每 2.5s 重画并钉底——**用户往上翻历史时被反复拽回最下面**。
        # 判据带**两个事实**：末轮未闭合（有轮开着）；有活动作业（服务端在跑）。
        # 作业那半由调用方在拿到 `live_job` 后补（见 handle 里的 `_running_now`）。
        "running": _round_open(data),
        "workspace": data.get("workspace", ""),
        "mode": data.get("mode", ""),
        "created_at": data.get("created_at", ""),
        "updated_at": data.get("updated_at", ""),
        "rounds": len(rounds),
        "steps": sum(r.get("steps_used") or 0 for r in rounds),
        "tools": tool_call_count(data.get("history")),
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
        "maint": maint_state(data),
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
    # **"重组前（整理后全量）" = 全量材料装配**（`_assemble_full_messages`）：
    # 它不看视图/归属，含**所有**轮（包括分给子 agent 的那些），这才是"重组前"
    # 的正确口径。此前取的是 `_assemble_messages()`——§41 之后主 agent 也走
    # **视图装配**，于是面板里只剩主 agent 那份料：分出去的轮（如 R7、R8）
    # 看起来"丢了"（2026-09-14 用户实测报障）。
    try:
        _pre_settle = agent._assemble_full_messages()
    except Exception:  # noqa: BLE001
        _pre_settle = None
    try:
        # 完整开场状态（promote + 渐近归属 _settle_views）——只 promote 不够：
        # 视图装配依赖各轮/块的域归属判定（505a101 起归属在 settle 里做）。
        # 不走 `_settle_after_maintenance`：那套受"有开放轮就推迟"的轮边界纪律
        # 约束（保护运行中轮的字节），而预览是**只读派生**（本函数已把
        # `_persist_rounds` 换成 no-op，连盘都不碰）——用户就是想看"若现在生效
        # 会是什么样"，故这里直接预演。
        agent._promote_org_results()
        agent._settle_views()
    except Exception:  # noqa: BLE001——预览失败不该影响任何东西
        pass
    if _v4_on():
        return _shared_views_payload(agent, task)
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
        # 主 agent 先按 **id**（`Main` 才是视图名；显示名"主agent"对不上
        # 任何归属名，会装配出"空视图"——2026-09-14 用户实测报障）
        keys = ((str(e.get("id") or ""), str(e.get("name") or ""))
                if str(e.get("id")) == "Main"
                else (str(e.get("name") or ""), str(e.get("id") or "")))
        for key in keys:
            got = agent._assemble_view_messages(str(key or ""), agent.rounds or [])
            if got is not None:
                break
        if got is None:
            continue
        try:
            tok = int(agent._estimate_messages(got))   # 与运行时同口径（防单位混用）
        except Exception:  # noqa: BLE001
            tok = sum(len(_content_text(m.get("content"))) for m in got) // 3
        msgs, total, trunc = [], 0, 0
        for m in got:
            raw = _content_text(m.get("content"))
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
                    # 新口径（§61/§63）：具体文件清单 + 历史层。前端优先渲染这两个，
                    # 只在都为空时退回 file_domains（历史数据）——§66.3 待办收口
                    "files": list(e.get("files") or []),
                    "history_files": list(e.get("history_files") or []),
                    "messages": msgs, "count": len(msgs),
                    "total_chars": total, "tokens": tok, "truncated": trunc})
    if out:
        # 主 agent 的"整理后、未按域重组"全文 = settle 之前的装配（见上）；
        full = (_pre_settle if _pre_settle is not None
                else agent._assemble_full_messages())
        alt_chars = sum(len(_content_text(m.get("content"))) for m in full)
        alt_msgs, alt_trunc = [], 0
        for m in full:
            raw = _content_text(m.get("content"))
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
            raw = _content_text(m.get("content"))
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
            # 该会话的模型上下文窗口（各视图共用）——投影要"占窗口多少"当分母，
            # 未运行过的条目注册表里 window=0（没有观测），分母得从这里给
            "window": int(agent._agent_window()),
            "note": "在内存中模拟产物生效所得（不改动会话）；真实生效发生在轮闭合或开新轮时"}


# 重组后视图体量的缓存：键 = task.json 的**完整路径**（不是 task_id——不同
# TASKS_ROOT 下同名会话会撞），值 = ((mtime_ns, size), 投影)。
# 为什么必须缓存：这个投影要真装配每个视图（实测 1.8–3.1s），而前端每切一次
# 会话就会来拉一次。文件没变就该秒回——否则请求在算的时候被浏览器中止连接，
# `_json` 写回时抛 `ConnectionAbortedError` 刷屏（2026-09-13 用户实测）。
_VIEW_SIZES_CACHE: dict[str, tuple[tuple[int, int], dict]] = {}
_VIEW_SIZES_LOCK = threading.Lock()


def _v4_on() -> bool:
    """V4 开关（取消重组）。serve 只用来决定**投影口径**，不参与装配。"""
    from .agent.support import v4_enabled

    return v4_enabled()


def _shared_views_payload(agent, task) -> dict:
    """V4 的"各 agent 看到什么"：**同一份共享历史** ＋ 各 agent 的职责与文件。

    V4 取消了重组（`assembly._assemble_messages_impl` 里视图分化那条被
    `and not v4_enabled()` 关掉），所有 agent 装配的是同一份字节——所以
    "每个子 agent 各看到什么"这个问题本身已经不存在了。这里如实给：
    一份真实装配（就是运行时那份）＋ 各条的职责/文件归属（那是真正按 agent 分的
    东西），不再算那套没人会走的按域视图（2026-09-17 用户："现在不是没有重组
    上下文这个概念了"）。

    **fork 之后另说**（2026-09-17）：一旦有 agent 被激活过（`forked_at > 0`），
    各家上下文就**不再是同一份**（公共快照 ＋ 自己的职责，之后自己干的轮全量、
    别人干的轮只进一段话）——这时给**逐 agent** 的行，数字取注册表里的按视图观察。
    """
    forked = [e for e in (task.registry or [])
              if isinstance(e, dict) and int(e.get("forked_at") or 0) > 0]
    if forked:
        rows = []
        for e in (task.registry or []):
            if not isinstance(e, dict):
                continue
            rows.append({
                "id": str(e.get("id") or ""), "name": str(e.get("name") or ""),
                "is_main": str(e.get("id") or "") == "Main", "shared": False,
                "count": 0, "total_chars": 0,
                "tokens": int(e.get("ctx_cur") or 0),
                "forked_at": int(e.get("forked_at") or 0),
            })
        return {
            "agents": rows, "shared": False, "forked": True,
            "window": int(agent._agent_window()),
            "note": "各 agent 已有自己的上下文（首次激活＝fork：公共上下文快照 ＋ 自己的职责，"
                    "之后自己干的轮全量、别人干的轮只进一段话）；数字是各自最近一次装配的体量",
        }
    msgs_src = agent._assemble_messages()
    msgs, total, trunc = [], 0, 0
    for m in msgs_src:
        raw = _content_text(m.get("content"))
        total += len(raw)
        body = raw
        if len(raw) > _DUMP_CAP:
            body = raw[:_DUMP_CAP] + chr(10) + f"…（本条截断，原 {len(raw):,} 字符）"
            trunc += 1
        msgs.append({"role": m.get("role"), "content": body, "len": len(raw),
                     "tool_calls": len(m.get("tool_calls") or [])})
    try:
        tok = int(agent._estimate_messages(msgs_src))
    except Exception:  # noqa: BLE001
        tok = total // 3
    duties = []
    for e in (task.registry or []):
        if not isinstance(e, dict):
            continue
        duties.append({
            "id": str(e.get("id") or ""), "name": str(e.get("name") or ""),
            "status": str(e.get("status") or ""),
            "files": [str(f) for f in (e.get("files") or [])],
            "history_files": [str(f) for f in (e.get("history_files") or [])],
            "description": str(e.get("description") or ""),
        })
    return {
        "agents": [{
            "id": "Main", "name": "主agent", "is_main": True, "shared": True,
            "count": len(msgs), "total_chars": total, "tokens": tok,
            "truncated": trunc, "messages": msgs,
        }],
        "shared": True,
        "duties": duties,
        "window": int(agent._agent_window()),
        "note": "V4 取消重组：所有 agent 共用同一份共享历史（段落档 ＋ 近期原文），"
                "这里给的就是运行时真正会发出去的那一份；下面列的是各 agent 的"
                "职责与文件归属，不是各自的上下文副本",
    }


def view_sizes(task_id: str) -> dict | None:
    """各 agent **重组后视图体量**（走 pending_views 的物化，剥掉消息正文）。

    顶部状态栏用。**登记完就能算，不必等下一轮对话**（2026-09-13 用户口径：
    "我重组上下文，给每个 agent 分了多少内容？不是分完 agent 就知道了吗？
    非得新消息干嘛？"）：视图字节本来就从 `rounds + registry` 确定性派生，
    这里在内存里把注册表每个条目装配一遍，数出来的就是"它现在会看到多少"。

    **按 task.json 的 (mtime_ns, size) 缓存**：这个投影要真装配每个视图，
    实测 1.8–3.1s（随 agent 数涨）。从前没有缓存，前端每切一次会话就重算一次，
    算到一半被浏览器中止连接 → `_json` 写回时 `ConnectionAbortedError` 刷屏
    （用户 2026-09-13 实测：几十段 traceback）。文件没变就不该重算。

    口径如实交代，不许含糊成实测：
    * `tokens` = 与运行时**同一把尺子**（`tokens.estimate`，装了 tiktoken 就是
      官方分词器），故与注册表里的 `ctx_cur`（实测观测）**可直接比较**；
    * `basis` = 用的是哪把尺子（`tiktoken:cl100k_base` / `heuristic`）；
    * `window` = 该会话的模型上下文窗口（`WOVRA_CONTEXT_LIMIT`）——未运行过的
      条目注册表里 `window=0`（没有观测），但"占窗口多少"这个投影是要分母的。
    """
    tf = task_module.TASKS_ROOT / str(task_id) / "task.json"
    key = str(tf)
    try:
        st = tf.stat()
        sig = (st.st_mtime_ns, st.st_size)
    except OSError:
        return None
    with _VIEW_SIZES_LOCK:
        hit = _VIEW_SIZES_CACHE.get(key)
    if hit is not None and hit[0] == sig:
        return hit[1]
    got = pending_views(task_id)
    if got is None:
        return None
    # 签名**算完之后**再取一次：`Task.load` 的块迁移可能把文件回写一遍
    # （幂等的既有行为），拿算之前那次的签名去存，缓存会永远命中不了
    # ——实测踩过（第二次调用照样重算）。
    try:
        st = tf.stat()
        sig = (st.st_mtime_ns, st.st_size)
    except OSError:
        sig = sig
    basis = tokens_module.caliber()
    try:
        from .agent.support import context_limit as _env_window
        window = int(got.get("window") or _env_window())
    except Exception:  # noqa: BLE001——拿不到就留 0，前端按"无分母"渲染
        window = int(got.get("window") or 0)
    shared = bool(got.get("shared"))
    # `forked` 与 `shared=False` 是两回事：fork 之后各家**各有各的**上下文（给的是
    # 逐 agent 的实测体量），旧链路才是"按域重组"。前端据这两个标志分别渲染。
    forked = bool(got.get("forked"))
    out = {"agents": [{k: a.get(k) for k in
                       ("id", "name", "is_main", "count", "total_chars",
                        "tokens", "alt_count", "alt_chars",
                        "alt_tokens")}
                      for a in got.get("agents") or []],
           "window": window, "basis": basis,
           "pending": True,
           "shared": shared, "forked": forked,
           "note": (got.get("note") if (shared or forked) else
                    "零 LLM 机械投影：在内存里按当前注册表 + 轮材料装配各视图"
                    "所得（会话一个字节都不改）")}
    with _VIEW_SIZES_LOCK:
        if len(_VIEW_SIZES_CACHE) > 16:      # 兜住条目数（会话数级别，不会涨）
            _VIEW_SIZES_CACHE.clear()
        _VIEW_SIZES_CACHE[key] = (sig, out)
    return out


def _event_agents(r: dict, main_id: str) -> list[str]:
    """轮内逐事件的 agent 归属——**复用 views.event_owners 的同一套判据**。

    口径（2026-09-12 用户拍板 + `route_to` 实现）：每轮恒由主 agent 起手并路由
    原话；换手点是 `route_to` 的**工具调用事件之后**（调用者执行了那次调用）；
    历史数据里的 `route_explicit` 只管老轮，不影响本规则。

    单点实现的理由：步数分段（`views.round_step_segments`）与对话页的事件标签
    必须说同一件事，否则会出现"这一步算 A 的步，但气泡挂在 B 名下"。
    """
    owners = views_module.event_owners(r, {views_module.MAIN_AGENT_ID: main_id})
    return [main_id if o == views_module.MAIN_AGENT_ID else o for o in owners]


def _live_file_paths(rounds: list[dict]) -> list[str]:
    """机械重算"被写过且还在"的文件（与维护侧 `_live_files` 同口径，零 LLM）。

    前端用它给结构树标**漏项**：活性文件必须落在某个节点里；只读文件不在
    此列（用户口径 2026-09-14：只读不是分裂对象，压缩后要用会重新读）。
    """
    from . import blocks as blocks_module
    from . import lifecycle as lifecycle_module
    ledger = lifecycle_module.FileLedger()
    for r in rounds or []:
        ledger.update(r, blocks=blocks_module.segment_round_by_file(r))
    return sorted(
        str(p) for p, e in ledger.entries().items()
        if e.get("state") != lifecycle_module.STATE_DEAD
        and int(e.get("write_count") or 0) > 0
    )


def _split_meta(r: dict, live_files: list[str] | None = None) -> dict | None:
    """分裂结果（已落实的轮字段 + 尚未落实的 pending_org）——机械透出。

    2026-09-14 口径：产物是**结构树**（节点 + files + 保底块归宿），
    供前端渲染核对；模型侧不再有"可分裂/不可分裂"判定字段（老会话可能
    仍带 split_assessment，原样透出）。`live_files` 是机械事实，前端拿它
    对结构树标漏项。
    """
    po = r.get("pending_org") or {}
    sa = r.get("split_assessment") or po.get("split_assessment")
    dm = r.get("domains") or po.get("domains")
    un = r.get("unassigned") or po.get("unassigned")
    if not (sa or dm or un):
        return None
    if isinstance(un, dict):
        un_ids = [str(x) for x in (un.get("block_ids") or [])]
    elif isinstance(un, (list, tuple)):
        un_ids = [str(x) for x in un]      # 老形态：直接是块 ID 列表
    else:
        un_ids = []
    return {
        "assessment": sa if isinstance(sa, dict) else {},
        "domains": [{
            "name": d.get("name"),
            "description": d.get("description") or "",
            "parent": d.get("parent") or "",
            "main_agent": bool(d.get("main_agent")),
            "file": d.get("file") or "",
            "files": d.get("files") or [],
            "file_domains": d.get("file_domains") or [],
            "history_files": d.get("history_files") or [],
            "chat_block_ids": d.get("chat_block_ids") or [],
            "user_block_ids": d.get("user_block_ids") or [],
            "goal": d.get("goal") or "",
        } for d in (dm or []) if isinstance(d, dict)],
        "unassigned": un_ids,
        "live_files": list(live_files or []),
        "pending": bool(po.get("domains") or po.get("split_assessment")),
    }


def _note_pending_for(r: dict, running: bool = False) -> bool:
    """这一轮是不是"**正在整理**"（V4 口径）：已闭合、没折、这一段话**正在写**。

    `note_state` 明说了：`done`（成了）／`failed`（失败留痕，原文继续顶着）／
    `running`（写的过程中）。**空**是歧义的——它同时表示两件相反的事：

    * "后台正在写这一段话"（维护跑起来了，这一轮排在批里还没轮到）；
    * "水位没到，压根没开始整理"（这是常态，绝大多数轮都是这个）。

    所以必须带上服务端的**上下文事实** `running`（维护在不在跑）才能判：
    `running=True` 时空着＝正在整理；`running=False` 时空着＝还没到水位，不是"整理中"。

    **2026-09-18 修**：此前只看"空且非 failed"就算 pending，于是**任何水位没到的会话里，
    每个已闭合轮都被显示成「整理中」**（用户实测："R1、R2 都显示整理中，现在不是没有触发
    水位吗？"）。同一个假信号还骗着前端的维护观察器一直武装（见 worklog §197）。
    """
    if str(r.get("end_state")) != "completed":
        return False
    if bool(r.get("folded")):
        return False
    state = str(r.get("note_state") or "")
    if state in ("done", "failed"):
        return False
    if state == "running":
        return True          # 明写着在写：不必再靠上下文推
    return bool(running)


def _org_state_of(r: dict, running: bool = False) -> str:
    """一轮的整理状态（**唯一折算点**：时间线徽标、会话摘要、列表都走这里）。

    * 旧链路自己写的 `org_state` 照旧优先（它的整理是另一条路）；
    * V4：折了 → **done**（它在上文里就是一段话）；维护在跑且这一段话还没写出来 →
      **pending（整理中）**；其余 → **raw**（原文全量保留，水位还没到）。

    参数 `running` = 服务端的维护在不在跑（`maint_state(data)["active"]`）——判 pending
    必须带上它，否则"还没到水位"会被误报成"整理中"（见 `_note_pending_for`）。

    "折叠"这个词退出用户界面（2026-09-18 用户："以后统一叫折叠为整理，和前端统一"），
    代码内部仍叫 `folded`——它是机械标志，不是用户词汇。
    """
    legacy = str(r.get("org_state") or "")
    if legacy:
        return legacy
    if not _v4_on():
        return "raw"
    if r.get("folded"):
        return "done"
    return "pending" if _note_pending_for(r, running) else "raw"


def _round_meta(r: dict, usage: dict | None = None,
                plan: dict | None = None,
                live_files: list[str] | None = None,
                maint_running: bool = False) -> dict:
    """轮元数据（不含 events 原文——16MB 级会话事件按需单轮取）。

    `stage` = 该轮属于哪个阶段（0 = **分裂前**，i≥1 = 第 i 次分裂生效之后）——
    显示按阶段分，且分裂前的轮**不归任何 agent**（§58）。

    `maint_running` = 服务端的维护在不在跑（`maint_state(data)["active"]`）——判
    "整理中"必须带上它，否则"水位没到"会被误报成"整理中"（见 `_note_pending_for`）。
    """
    evs = r.get("events") or []
    return {
        "seq": r.get("seq"),
        "user_input": (r.get("user_input") or {}).get("original", ""),
        "end_state": r.get("end_state"),
        # 整理状态：折算规则见 `_org_state_of`（**唯一折算点**）
        "org_state": _org_state_of(r, maint_running),
        # 分裂状态（2026-09-15）：running/ready/deferred/stale/rejected/failed/done——
        # 维护只写 history 时会被并发写覆盖（见 worklog §106），落到轮上才可见、可查。
        "split_state": r.get("split_state") or "",
        # 分裂失败的原因（落轮字段，抗并发写丢 history 行）
        "split_note": r.get("split_note") or "",
        "org_generation": r.get("org_generation", 1),
        "steps_used": r.get("steps_used"),
        "active_view": r.get("active_view") or "",
        # V4：每轮一段话（note）＝ 给人看的叙事；folded = 该轮已折叠成段落进装配。
        # `note_segments` = 分裂后一轮多 agent 时的分段产物（每家一段，按事件顺序）。
        "note": r.get("note") or {},
        "note_segments": r.get("note_segments") or [],
        # "这一轮在等用户拍什么板"（V4 §3.7 连续性）：没产出就没有这个字段
        "awaiting_user": (str((r.get("note") or {}).get("awaiting_user") or "")
                          or "；".join(
                              str(s.get("awaiting_user") or "")
                              for s in (r.get("note_segments") or [])
                              if str(s.get("awaiting_user") or "")
                          )),
        "note_state": r.get("note_state") or "",
        "folded": bool(r.get("folded")),
        "stage": views_module.stage_index(r, plan or {}),
        # 该轮是否已被整理压缩覆盖（只作整理状态的展示，不再决定轮账归属）
        "compressed": (str(r.get("org_state") or "") == "done" or bool(r.get("folded"))),
        "route_hops": r.get("route_hops", 0),   # 轮内转交次数（>0 = 主 agent 路由过）
        # 会合：本轮的参与者队列（join_with 排的队）——多参与者轮要看得见
        "participants": [str((p or {}).get("agent") or "")
                         for p in (r.get("participants") or [])],
        "events": len(evs),
        "t0": (evs[0].get("timestamp") or "") if evs else "",
        "t1": (evs[-1].get("timestamp") or "") if evs else "",
        "usage": usage or {},
        "split": _split_meta(r, live_files),
        "blocks": r.get("blocks") or [],
    }


def _live_ctx(registry: list | None) -> list[dict]:
    """注册表里的**上下文观测**（每 agent 一行）——供 `/plan` 每 tick 现读。

    只给观测字段（`ctx_cur`/`ctx_peak`/`window`）：运行时的 `_touch_view_context`
    在**每次装配**（一轮内每步装配一次）就地更新注册表条目，`_persist_rounds`
    随后把它写盘；前端每 tick 拉到就能重画顶部上下文窗口，不必等轮闭合。
    """
    out = []
    for e in registry or []:
        if not isinstance(e, dict):
            continue
        out.append({
            "id": str(e.get("id") or ""), "name": str(e.get("name") or ""),
            "ctx_cur": int(e.get("ctx_cur") or 0),
            "ctx_peak": int(e.get("ctx_peak") or 0),
            "window": int(e.get("window") or 0),
        })
    return out


def session_meta(task_id: str, data: dict) -> dict:
    """会话元数据（详情页用：摘要 + 账本 + 计划 + 注册表 + 轮元数据）。"""
    meta = session_summary(task_id, data)
    meta["task_state"] = data.get("task_state") or {}
    meta["todo"] = data.get("todo") or {}
    # 对齐/传话线程（像聊天软件）：谁交给谁、内容是什么（前端「线程」面板用）
    meta["chat"] = data.get("chat") or []
    meta["registry"] = data.get("registry") or []
    # 跨条目归属互斥体检（2026-09-13，worklog §78）：**只报不改**——旧会话的
    # 数据原样保留（用户口径），但一个文件同时挂在两个 agent 名下这件事不该
    # 静默躺在页面上。新分裂由 promote 的落点预演拦住，不会再长出这种条目。
    meta["registry_defects"] = registry_module.registry_defects(meta["registry"])
    # **公共文件**（2026-09-18 用户口径）：结构树没认领的活性文件——谁都能先动手，
    # 第一次写/改的那个 agent 得所有权。页面上要让这件事看得见（不然"这个文件归谁"
    # 在注册表里查不到，看起来像漏了）。
    meta["public_files"] = [str(p) for p in (data.get("public_files") or [])]
    # 窗口语义纠正（2026-09-12）：旧数据把整理水位 100K 存进了 registry
    # window——投影层按真实窗口展示；落盘值随下一轮活动由 core 自愈。
    # **未观测的条目不要补默认窗口**（2026-09-13，worklog §78）：`window=0`
    # 是"这个 agent 一次都没跑过"的事实，补成 1M 会在页面上演成
    # `0/1M（0.00%）`——看起来像"窗口空着没用"，其实是"没有观测"。前端据此
    # 显示「未运行（无观测）」。
    from .agent.support import context_limit as _env_window
    for e in meta["registry"] or []:
        if isinstance(e, dict) and int(e.get("window") or 0) == 100_000:
            e["window"] = _env_window()
    # per-agent 账**派生后补进条目**（2026-09-12 用户拍板：账本不落盘）——
    # 前端照旧读 `rounds`/`steps`/`handoffs`，但那些值现在是现场算的：
    # `rounds` = 名下轮数（落点归属），`seqs` 是**总轮 R 号**清单（显示与展开
    # 共用同一套号），消费按调用方实记（`by=` 段）。
    rounds = data.get("rounds") or []
    ledger = views_module.agent_ledger(
        rounds, registry_module.latest_domains(rounds), data.get("registry") or []
    )
    cost = agent_cost_map(data)
    meta["round_account"] = views_module.round_account(
        rounds, registry_module.latest_domains(rounds), data.get("registry") or []
    )
    for e in meta["registry"] or []:
        if not isinstance(e, dict):
            continue
        rec = ledger.get(str(e.get("name") or "")) or ledger.get(str(e.get("id") or ""))
        if rec is None:
            continue
        e["rounds"] = rec["rounds"]
        e["seqs"] = list(rec["seqs"])
        e["first"] = rec["first"]
        e["last"] = rec["last"]
        e["steps"] = rec["steps"]
        e["handoffs"] = rec["handoffs"]
        c = cost.get(str(e.get("name") or "")) or {}
        if c:
            e["prompt"] = c.get("prompt", 0)
            e["cached"] = c.get("cached", 0)
            e["miss"] = c.get("miss", 0)
            e["completion"] = c.get("completion", 0)
            e["cost_known"] = True
        if not e.get("ctx_peak"):
            e["ctx_peak"] = rec["ctx_peak"]
        e["share"] = rec["share"] if rec["window"] else 0.0
    usage = round_usage_map(data)
    plan = views_module.stage_plan(rounds)
    live_files = _live_file_paths(rounds)
    meta["round_list"] = [_round_meta(r, usage.get(r.get("seq")), plan, live_files,
                                      maint_running=bool(
                                          (meta.get("maint") or {}).get("active")))
                          for r in rounds]
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


def _v4_view_bytes(agent, task, view: str) -> tuple[list[dict] | None, str]:
    """V4 下"这个 agent 会看到的那一份"（取消重组后，按域视图不再存在）。

    `forked_at > 0` 的给**它的 fork 基线**（自己干的轮全量、别人干的轮一段话）；
    其余（未激活 / 主 agent）给**共享历史**——两者都是运行时真正发出去的那一份。
    返回 `(messages, 说明)`；materialize 失败返 `(None, "")`。
    """
    try:
        base = agent._fork_baseline(view, task.rounds or [])
    except Exception:  # noqa: BLE001——诊断端点不该因单条材料出错而整体失败
        return None, ""
    if base is None:
        return agent._assemble_messages(), (
            f"V4 取消重组：{view} 尚未激活（零成本待命），与所有 agent 共用同一份"
            "共享历史——以下就是运行时真正发出去的那一份")
    return base, (f"{view} 已 fork：自己干的轮全量原文、别人干的轮只进一段话"
                  "（内容可丢、存在性不可丢）——以下是它真正会看到的那一份")


def view_messages(task_id: str, view: str) -> dict | None:
    """物化某个 agent 的上下文（= 路由到它时模型所见，确定性派生）。

    用于人工检查分裂后的上下文是否正确。view 先按域名再按 id 尝试。
    """
    from .agent import MODE_MANAGED
    from .cli.prompt import _build_agent  # 懒导入：避免 cli↔serve 循环依赖
    try:
        task = task_module.Task.load(task_id)
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    # view 先按域名、再按注册表 id 解析（2026-09-14：此前传 id 会静默
    # 装配出"空视图"——名字对不上节点名，看起来像"这个 agent 没材料"）
    names = {str(d.get("name") or "")
             for d in views_module.latest_domains(task.rounds or [])}
    if view not in names:
        for entry in (task.registry or []):
            if str(entry.get("id") or "") == view and entry.get("name") in names:
                view = str(entry["name"])
                break
    agent = _build_agent(task, mode=task.mode or MODE_MANAGED)
    agent.current_round = None
    if _v4_on():
        msgs, note = _v4_view_bytes(agent, task, view)
        if msgs is None:
            return {"view": view, "available": False, "messages": [],
                    "total_chars": 0, "note": "材料装不出来（见 history 里的留痕）"}
        return {"view": view, "available": True, "note": note,
                "messages": [{"role": m.get("role"),
                              "content": m.get("content", "")} for m in msgs],
                "total_chars": sum(len(m.get("content", "")) for m in msgs)}
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
        if _v4_on():
            # V4 取消按域重组：`_assemble_view_messages` 那条路运行时不再走，
            # 拿它当"这个 agent 看到什么"会给出没人会看到的一份。
            got, note = _v4_view_bytes(agent, task, view)
            if got is None:
                got = agent._assemble_messages()
                note = "材料装不出来，以下是共享历史（主装配）"
            msgs = got
        else:
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
        body = _content_text(m.get("content"))
        total += len(body)
        if len(body) > _DUMP_CAP:
            body = (body[:_DUMP_CAP]
                    + chr(10) + f"…（本条截断，原 {len(body):,} 字符）")
            truncated += 1
        out.append({"role": m.get("role"), "content": body,
                    "len": len(body),
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


# **客户端已走**这一族异常：浏览器刷新 / 切会话 / 关标签页时中止连接，
# Windows 上表现为 `ConnectionAbortedError: [WinError 10053]`。它是**常态**，
# 不是服务端故障——但 `BaseHTTPRequestHandler` 默认把它连 traceback 一起打到
# stderr，实测刷了几十段，看着像服务崩了（用户 2026-09-13 贴了一大片来问）。
# 凡是"对端没了"就闭嘴，真错误照旧打。
_CLIENT_GONE = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)


class _Server(ThreadingHTTPServer):
    """多线程 HTTP 服务 + **安静的客户端断开**（见 `_CLIENT_GONE`）。"""

    daemon_threads = True

    def handle_error(self, request, client_address) -> None:
        error = sys.exc_info()[1]
        if isinstance(error, _CLIENT_GONE):
            return                      # 对端已走：不是错误，别刷屏
        super().handle_error(request, client_address)


class _Handler(BaseHTTPRequestHandler):
    cache: SummaryCache = None  # type: ignore[assignment]
    tasks_root: Path = None  # type: ignore[assignment]

    def log_message(self, fmt, *args):  # 静默访问日志（默认 stdout 刷屏）
        pass

    # ---- 响应辅助 ----
    def _json(self, payload, code: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except _CLIENT_GONE:
            return                      # 客户端在装配/写回期间走了（刷新、切页）

    def _bytes(self, body: bytes, ctype: str) -> None:
        # no-store 一律带：前端热更新靠浏览器每次拿到最新 index.html
        try:
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except _CLIENT_GONE:
            return

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
        if path == "/api/settings":
            from . import settings as settings_module
            return self._json(settings_module.describe())
        if path == "/api/providers":
            from . import providers as providers_module
            return self._json(providers_module.describe())
        mpm = re.fullmatch(r"/api/providers/([^/]+)/models", path)
        if mpm:
            from . import providers as providers_module
            return self._json(providers_module.list_models(unquote(mpm.group(1))))
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
            # **"在跑" = 有活动作业 或 末轮未闭合**（两个事实任一成立）。作业这一半
            # 在这里才拿得到（`session_meta` 不知道作业表），故在拿到后合并。
            meta["running"] = bool(meta.get("running") or meta["live_job"])
            meta["safety_mode"] = str(data.get("safety_mode") or "approve")
            meta["approved_tags"] = list(data.get("approved_tags") or [])
            # 会话级模型选择（聊天页两个下拉读它；空 = 用渠道商当前项）
            meta["provider"] = str(data.get("provider") or "")
            meta["model"] = str(data.get("model") or "")
            meta["reasoning"] = str(data.get("reasoning") or "")
            from .agent.support import org_watermark
            meta["org_watermark"] = org_watermark()   # 整理水位（账本产出条件）
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
        # **计划页的轻量轮询口**（2026-09-12 用户要求："只有调用 todo 就同步更新到
        # 那边"）：只回计划账本本身，几十字节级——前端在跑轮时每 tick 拉一次，
        # 一变化就把「当前进行中的阶段 / 验收通过」重画，不必等一轮结束。
        mplan = re.fullmatch(r"/api/sessions/([^/]+)/plan", path)
        if mplan:
            data = self._load_task(mplan.group(1))
            if data is None:
                return self._json({"error": "session not found"}, 404)
            rounds = data.get("rounds") or []
            usage = usage_totals(data.get("history"))
            return self._json({
                "todo": data.get("todo") or {},
                "todo_log": todo_log(data),
                # **还在整理的轮**（2026-09-13）：整理/分裂改成异步之后（§86），
                # 轮结束不代表维护结束——前端靠这个轻量字段知道"什么时候可以
                # 刷新看新注册表"，不必反复拉整份 session_meta。
                "org_pending": [r.get("seq") for r in rounds
                                if str(r.get("org_state") or "") == "pending"],
                # **顶部六格的按步现算值**（2026-09-15 用户口径："按步更新，不要
                # 再按轮更新"）：与 `session_meta` 的 usage/rounds/steps 同源同口径
                # （`usage_totals(history)` + `Σsteps_used`），但**每 tick 现读**——
                # 跑轮期间账本行逐调用落盘，前端拉到就能重画，不必等轮闭合。
                "usage": {k: usage.get(k, 0) for k in (
                    "calls", "prompt", "cached", "miss", "completion", "ttft_sum",
                    # TTFT 均值只认干活轮样本（维护批量调用输入巨大，混进来会虚高）
                    "ttft_work_sum", "ttft_work_n", "nofirst")},
                "rounds": len(rounds),
                "steps": sum(int(r.get("steps_used") or 0) for r in rounds),
                "tools": tool_call_count(data.get("history")),
                # **各 agent 的上下文观测**（2026-09-17：顶部上下文窗口按步更新）：
                # `ctx_cur` 是**每次装配**（一轮内每步都装一次）的体量，随步落盘——
                # 这里每 tick 现读，前端拉到就重画，不必等轮闭合。口径与
                # `session_meta` 的 registry 同源（同一份 task.json 的同一个字段）。
                "ctx": _live_ctx(data.get("registry")),
                # 维护进度（整理/分裂在后台跑，轮闭合作业就没了——界面靠它
                # 显示"整理中…/分裂中…（已 Ns）"，见 `maint_state`）
                "maint": maint_state(data),
            })
        mtree = re.fullmatch(r"/api/sessions/([^/]+)/tree", path)
        if mtree:
            data = self._load_task(mtree.group(1))
            if data is None:
                return self._json({"error": "session not found"}, 404)
            return self._json(project_tree(str(data.get("workspace") or ""), data))
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
                                         "pending", "round")})
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
                               "round": job.get("round") or 0,
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
            # `?after=N`：从第 N 个分片起推（刷新后的重新挂载先做一次
            # 单次 catch-up 拉全量，再从这里接上，**不重复重放**——从 0 重放
            # 几百个分片会把页面刷爆，而且重放里的状态分片会把思考缓冲清空，
            # 表现为"刷新后卡住 + 思考内容消失"，实测 2026-09-13）
            try:
                _q = parse_qs(urlparse(self.path).query)
                last = max(0, int((_q.get("after") or ["0"])[0] or 0))
            except (TypeError, ValueError):
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
        if path == "/api/settings":
            from . import settings as settings_module
            values = body.get("values")
            if not isinstance(values, dict):
                return self._json({"error": "values 必填（参数表）"}, 400)
            result = settings_module.apply({str(k): str(v)
                                            for k, v in values.items()})
            result["settings"] = settings_module.describe()
            return self._json(result, 200 if result["ok"] else 400)
        if path == "/api/providers":
            from . import providers as providers_module
            action = str(body.get("action") or "upsert")
            if action == "test":
                return self._json(providers_module.test_provider(
                    str(body.get("id") or ""), str(body.get("model") or "")))
            if action == "remove":
                providers_module.remove(str(body.get("id") or ""))
            elif action == "current":
                providers_module.set_current(str(body.get("id") or ""))
            else:
                provider = body.get("provider")
                if not isinstance(provider, dict):
                    return self._json({"error": "provider 必填（一条渠道商）"}, 400)
                providers_module.upsert({**provider,
                                         "current": bool(body.get("current"))})
            return self._json({**providers_module.describe(), "ok": True})
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
        m = re.fullmatch(r"/api/sessions/([^/]+)/attach", path)
        if m:
            return self._save_attachment(m.group(1), body)
        m = re.fullmatch(r"/api/sessions/([^/]+)/turn", path)
        if m:
            return self._start_turn(m.group(1), str(body.get("content") or ""))
        mr = re.fullmatch(r"/api/sessions/([^/]+)/resume", path)
        if mr:
            return self._start_resume(mr.group(1))
        mmo = re.fullmatch(r"/api/sessions/([^/]+)/model", path)
        if mmo:
            # 会话级模型选择（2026-09-18 用户口径："聊天页面有模型选择以及思考强度选择"）。
            # 只改本会话的三个字段，落盘后**下一轮**生效（每轮重建 agent 时读它）。
            try:
                task = task_module.Task.load(mmo.group(1))
            except (OSError, ValueError, json.JSONDecodeError):
                return self._json({"error": "session not found"}, 404)
            if "provider" in body:
                task.provider = str(body.get("provider") or "")
            if "model" in body:
                task.model = str(body.get("model") or "")
            if "reasoning" in body:
                level = str(body.get("reasoning") or "")
                if level and level not in ("off", "auto", "high"):
                    return self._json({"error": "reasoning 只能是 off/auto/high"}, 400)
                task.reasoning = level
            task.save()
            with self.cache._lock:
                self.cache._cache.pop(mmo.group(1), None)
            return self._json({"ok": True, "provider": task.provider,
                               "model": task.model, "reasoning": task.reasoning})
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
            # **正在跑的那一轮也要立刻换挡**（2026-09-13 用户报的 bug：任务进行中
            # 切到自主模式不生效）。运行中的 job 手里握着**自己那份 Task 对象**，
            # 上面那次 save 只落盘、改不到它——于是闸门继续按老模式拦。
            # 用进程级覆盖表把它接上：闸门每次判定都先看这张表。
            _SAFETY_OVERRIDE[msf.group(1)] = mode
            for job in list(_JOBS.values()):
                if getattr(job.get("task"), "id", "") == msf.group(1):
                    try:
                        job["task"].safety_mode = mode
                    except Exception:  # noqa: BLE001
                        pass
                    job["live"].append({
                        "k": "status",
                        "s": "已切到自主运行：敏感操作自动放行" if mode == "auto"
                             else "已切回审批模式：敏感操作先问你",
                    })
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

    def _save_attachment(self, task_id: str, body: dict) -> None:
        """用户粘贴/上传的附件落盘到工作区 `attachments/`，返回引用行。

        原文不进 task.json（会话存档已 MB 级）：前端拿到 `marker` 后拼进用户
        消息，内容由装配期展开注入。落盘目录必须在工作区内——工具与装配
        都按工作区相对路径找它。
        """
        try:
            task = task_module.Task.load(task_id)
        except (OSError, ValueError, json.JSONDecodeError):
            return self._json({"error": "session not found"}, 404)
        workspace = str(task.workspace or "")
        if not workspace:
            return self._json(
                {"error": "本会话没有工作目录，无法接收附件（先设工作目录）"}, 400)
        from . import attachments as attachments_module
        from .tools import safety as _safety
        _safety.bind_workspace(workspace)
        name = str(body.get("name") or "pasted.txt")
        try:
            if body.get("data_url"):
                rel = attachments_module.save_data_url(str(body["data_url"]), name)
            elif body.get("text") is not None:
                rel = attachments_module.save_text(str(body["text"]), name)
            else:
                return self._json({"error": "text 或 data_url 必填"}, 400)
        except (OSError, ValueError) as error:
            return self._json({"error": f"附件落盘失败：{error}"}, 500)
        return self._json({"ok": True, "path": rel,
                           "marker": attachments_module.marker(rel)})

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
        from .tools import safety as _safety
        if name in ("help", "h", "?", "帮助"):
            return self._json({"text": _WEB_HELP})
        try:
            task = task_module.Task.load(task_id)
        except (OSError, ValueError, json.JSONDecodeError):
            return self._json({"error": "session not found"}, 404)
        # 命令在本请求线程里跑：bg 那几个工具要按**本会话的工作区**找日志/
        # 起进程，故先绑到本线程（其余分支只读 task 数据，绑了也无害）。
        if task.workspace:
            _safety.bind_workspace(task.workspace)
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
    server = _Server((args.host, args.port), _Handler)
    print(f"Wovra 可视化：http://{args.host}:{args.port}/"
          f"（只读投影；Ctrl+C 停止。数据目录：{task_module.TASKS_ROOT}）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        stop.set()
        server.server_close()
        print("已停止。")
