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
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from . import task as task_module

_WEBUI = Path(__file__).resolve().parents[2] / "webui" / "index.html"
_ID_SAFE = re.compile(r"^[0-9A-Za-z_-]+$")
_LLM_CALL = re.compile(r"^\[(\w+)\]\s+(.*)$")
_KV = re.compile(r"(\w+)=([\d,]+(?:\.\d+)?)")
_FINISH = re.compile(r"finish=(\S+)")
_ORG_STATES = ("done", "pending", "failed")

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
        "org": org,
        "escalations": len(ts.get("escalations") or []),
        "experiments": len(ts.get("experiments") or []),
        "todo_milestone": ms.get("goal"),
        "todo_steps_left": sum(1 for s in todo.get("steps") or []
                               if not s.get("done")),
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


def round_detail(data: dict, seq: int) -> dict | None:
    """单轮完整事件与块（expand 语义：块/事件原文按需取）。"""
    for r in data.get("rounds") or []:
        if r.get("seq") != seq:
            continue
        events = []
        for e in r.get("events") or []:
            msg = e.get("message") or {}
            events.append({
                "id": e.get("id"), "type": e.get("type"),
                "status": e.get("status", ""), "role": msg.get("role"),
                "content": msg.get("content", ""),
                "tool_calls": msg.get("tool_calls"),
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

    def _html(self) -> None:
        try:
            body = _WEBUI.read_bytes()
        except OSError:
            self._json({"error": "webui/index.html 缺失"}, 500)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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
            detail = round_detail(data, int(m.group(2))) if data else None
            if detail is None:
                return self._json({"error": "round not found"}, 404)
            return self._json(detail)
        return self._json({"error": "not found"}, 404)

    def do_POST(self):  # noqa: N802
        self._json({"error": "read-only（可视化不做任何写操作）"}, 405)

    do_PUT = do_POST
    do_DELETE = do_POST
    do_PATCH = do_POST


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
