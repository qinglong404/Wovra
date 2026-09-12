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
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import task as task_module

_WEBUI = Path(__file__).resolve().parents[2] / "webui" / "index.html"
_ID_SAFE = re.compile(r"^[0-9A-Za-z_-]+$")
_LLM_CALL = re.compile(r"^\[(\w+)\]\s+(.*)$")
_KV = re.compile(r"(\w+)=([\d,]+(?:\.\d+)?)")
_FINISH = re.compile(r"finish=(\S+)")
_ORG_STATES = ("done", "pending", "failed")

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


def _execute_turn(job_id: str, task_id: str, content: str) -> None:
    """轮执行线程：CLI 同款管线（会话锁 → agent → run → 补整理）。"""
    job = _JOBS[job_id]
    from .agent import MODE_MANAGED
    from .cli.prompt import _build_agent  # 懒导入：避免 cli↔serve 循环依赖
    from .cli.session import _acquire_session_lock, _release_session_lock
    try:
        job["status"] = "running"
        task = task_module.Task.load(task_id)
        with _TURN_GATE:
            _acquire_session_lock(task)  # 被占用时抛 SystemExit（CLI 语义）
            try:
                agent = _build_agent(task, mode=task.mode or MODE_MANAGED,
                                     async_organization=False)
                answer = agent.run(content)
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



_USAGE_KEYS = ("prompt", "cached", "miss", "completion")


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


def _round_meta(r: dict) -> dict:
    """轮元数据（不含 events 原文——16MB 级会话事件按需单轮取）。"""
    return {
        "seq": r.get("seq"),
        "user_input": (r.get("user_input") or {}).get("original", ""),
        "end_state": r.get("end_state"),
        "org_state": r.get("org_state"),
        "org_generation": r.get("org_generation", 1),
        "steps_used": r.get("steps_used"),
        "events": len(r.get("events") or []),
        "blocks": r.get("blocks") or [],
    }


def session_meta(task_id: str, data: dict) -> dict:
    """会话元数据（详情页用：摘要 + 账本 + 计划 + 注册表 + 轮元数据）。"""
    meta = session_summary(task_id, data)
    meta["task_state"] = data.get("task_state") or {}
    meta["todo"] = data.get("todo") or {}
    meta["registry"] = data.get("registry") or []
    meta["round_list"] = [_round_meta(r) for r in data.get("rounds") or []]
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

        events = []
        for e in r.get("events") or []:
            msg = e.get("message") or {}
            if after_n is not None and _num(e.get("id")) <= after_n:
                continue
            events.append({
                "id": e.get("id"), "type": e.get("type"),
                "status": e.get("status", ""), "role": msg.get("role"),
                "content": msg.get("content", ""),
                "tool_calls": msg.get("tool_calls"),
                "tool_call_id": msg.get("tool_call_id"),
            })
        return {"seq": seq,
                "user_input": (r.get("user_input") or {}).get("original", ""),
                "events": events, "blocks": r.get("blocks") or []}
    return None


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
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
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
        if path == "/api/sessions":
            self.cache.scan()  # 增量：mtime 没变的会话直接跳过（廉价）
            items = self.cache.snapshot()
            return self._json({"scanning": self.cache.scanning,
                               "sessions": items})
        m = re.fullmatch(r"/api/sessions/([^/]+)", path)
        if m:
            data = self._load_task(m.group(1))
            if data is None:
                return self._json({"error": "session not found"}, 404)
            return self._json(session_meta(m.group(1), data))
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
                               for k in ("status", "answer", "error")})
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
            goal = str(body.get("goal") or "").strip()
            if not goal:
                return self._json({"error": "goal 必填"}, 400)
            task = task_module.Task.create(goal=goal[:500])
            return self._json({"id": task.id}, 201)
        m = re.fullmatch(r"/api/sessions/([^/]+)/turn", path)
        if m:
            return self._start_turn(m.group(1), str(body.get("content") or ""))
        return self._json({"error": "not found"}, 404)

    do_PUT = do_POST
    do_DELETE = do_POST
    do_PATCH = do_POST

    def _start_turn(self, task_id: str, content: str) -> None:
        """追加一轮对话：三道互斥（进程内单飞 / CLI 会话锁 / 任务级去重）。"""
        if not content.strip():
            return self._json({"error": "content 必填"}, 400)
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
