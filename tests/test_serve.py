"""wovra serve 的契约测试：派生函数 + HTTP 冒烟（本机回环，零模型）。"""

import json
import threading
import urllib.request
from pathlib import Path

import pytest

from wovra import serve


def test_parse_llm_call_full_row():
    row = serve.parse_llm_call(
        "[working] prompt=5,226 cached=0 miss=5,226 completion=185 "
        "ttft=2.0s dur=2.7s finish=tool_calls"
    )
    assert row["purpose"] == "working"
    assert row["prompt"] == 5226 and row["cached"] == 0 and row["miss"] == 5226
    assert row["completion"] == 185
    assert row["ttft"] == 2.0 and row["dur"] == 2.7
    assert row["finish"] == "tool_calls"


def test_parse_llm_call_rejects_other_kinds():
    assert serve.parse_llm_call("[managed] steps=60 context=40,262") is None
    assert serve.parse_llm_call("") is None


def test_usage_totals_aggregates():
    history = [
        {"kind": "llm_call", "time": "t1",
         "detail": "[working] prompt=100 cached=90 miss=10 completion=5 ttft=1.0s finish=stop"},
        {"kind": "llm_call", "time": "t2",
         "detail": "[working] prompt=200 cached=100 miss=100 completion=8 ttft=2.0s finish=tool_calls"},
        {"kind": "usage", "detail": "不是 llm_call 行，跳过"},
    ]
    totals = serve.usage_totals(history)
    assert totals["calls"] == 2
    assert totals["prompt"] == 300 and totals["cached"] == 190 and totals["miss"] == 110
    assert totals["ttft_sum"] == 3.0
    assert totals["finish"] == {"stop": 1, "tool_calls": 1}
    assert len(totals["rows"]) == 2


def _fake_task() -> dict:
    return {
        "id": "s1", "goal": "测试目标", "status": "in_progress",
        "workspace": "C:/x/proj", "mode": "managed",
        "created_at": "2026-09-12T10:00:00", "updated_at": "2026-09-12T11:00:00",
        "task_state": {"escalations": ["等拍板"], "experiments": []},
        "todo": {"milestone": {"goal": "大步一"}, "steps": [{"done": False}]},
        "registry": [{"id": "A", "name": "主agent", "status": "active", "inbox": []}],
        "history": [{"kind": "llm_call", "time": "t1",
                     "detail": "[working] prompt=100 cached=90 miss=10 completion=5 "
                               "ttft=1.0s finish=stop"}],
        "rounds": [
            {"seq": 1, "user_input": {"original": "干活"}, "end_state": "completed",
             "org_state": "done", "org_generation": 2, "steps_used": 7,
             "events": [{"id": "R1-E01", "type": "user", "status": "",
                         "message": {"role": "user", "content": "干活"}}],
             "blocks": [{"id": "R1-B1", "file": "a.py", "kind": "file",
                         "events": ["R1-E01"]}]},
            {"seq": 2, "user_input": {"original": "继续"}, "end_state": "open",
             "org_state": "", "events": []},
        ],
    }


def test_session_summary_derives_org_and_usage():
    s = serve.session_summary("s1", _fake_task())
    assert s["goal"] == "测试目标" and s["rounds"] == 2
    assert s["org"] == {"done": 1, "pending": 0, "failed": 0, "raw": 1}
    assert s["escalations"] == 1 and s["todo_milestone"] == "大步一"
    assert s["usage"]["calls"] == 1 and s["usage"]["prompt"] == 100


def test_session_meta_strips_events_keeps_blocks():
    meta = serve.session_meta("s1", _fake_task())
    assert meta["round_list"][0]["events"] == 1          # 事件只留计数
    assert meta["round_list"][0]["blocks"][0]["id"] == "R1-B1"
    assert meta["registry"][0]["id"] == "A"
    assert "escalations" in meta["task_state"]


def test_round_detail_flattens_events():
    d = serve.round_detail(_fake_task(), 1)
    assert d["user_input"] == "干活"
    assert d["events"][0] == {"id": "R1-E01", "type": "user", "status": "",
                              "role": "user", "content": "干活",
                              "tool_calls": None, "tool_call_id": None}
    assert d["blocks"][0]["file"] == "a.py"
    assert serve.round_detail(_fake_task(), 99) is None


# ---- HTTP 冒烟（本机回环） ----------------------------------------------------


@pytest.fixture()
def server(tmp_path):
    tasks = tmp_path / "tasks"
    (tasks / "s1").mkdir(parents=True)
    (tasks / "s1" / "task.json").write_text(
        json.dumps(_fake_task(), ensure_ascii=False), encoding="utf-8")

    class _H(serve._Handler):
        pass

    _H.cache = serve.SummaryCache(tasks)
    _H.tasks_root = tasks
    httpd = serve.ThreadingHTTPServer(("127.0.0.1", 0), _H)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{port}"
    httpd.shutdown()


def _get(url, method="GET"):
    req = urllib.request.Request(url, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def test_http_sessions_endpoint(server):
    _H_cache_warm = None
    code, body = _get(server + "/api/sessions")
    assert code == 200 and body["sessions"][0]["id"] == "s1"
    code, body = _get(server + "/api/sessions/s1")
    assert code == 200 and body["round_list"][0]["seq"] == 1
    code, body = _get(server + "/api/sessions/s1/rounds/1")
    assert code == 200 and body["events"][0]["id"] == "R1-E01"
    code, body = _get(server + "/api/sessions/nope")
    assert code == 404
    code, body = _get(server + "/api/sessions/s1", method="POST")
    assert code == 405  # 只读：写操作一律 405
    code, body = _get(server + "/api/sessions/../../etc", method="GET")
    assert code == 404  # 路径穿越不出去（id 白名单）


def test_http_serves_index(server):
    with urllib.request.urlopen(server + "/", timeout=5) as r:
        html = r.read().decode("utf-8")
    assert r.status == 200 and "WOVRA" in html
