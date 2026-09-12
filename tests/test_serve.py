"""wovra serve 的契约测试：派生函数 + HTTP 冒烟（本机回环，零模型）。"""

import json
import threading
import urllib.request
from pathlib import Path

import pytest

from wovra import serve
from wovra import task as task_module


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
    assert s["last_round"] == {"seq": 2, "events": 0, "end_state": "open"}


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


def test_round_detail_after_filter():
    """C3 实时跟随便签：after=R{n}-E{m} 只回该事件之后的部分。"""
    data = _fake_task()
    data["rounds"][0]["events"] = [
        {"id": f"R1-E0{i}", "type": "tool_call", "status": "",
         "message": {"role": "assistant", "content": f"第{i}步"}}
        for i in range(1, 5)
    ]
    d = serve.round_detail(data, 1, after="R1-E02")
    assert [e["id"] for e in d["events"]] == ["R1-E03", "R1-E04"]
    assert serve.round_detail(data, 1, after="R1-E04")["events"] == []


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
    code, body = _get(server + "/api/sessions")
    assert code == 200 and body["sessions"][0]["id"] == "s1"
    code, body = _get(server + "/api/sessions/s1")
    assert code == 200 and body["round_list"][0]["seq"] == 1
    code, body = _get(server + "/api/sessions/s1/rounds/1")
    assert code == 200 and body["events"][0]["id"] == "R1-E01"
    code, body = _get(server + "/api/sessions/nope")
    assert code == 404
    # 写通道收窄为两个具名端点（新建会话 / turn），其余 POST 一律 404
    code, body = _get(server + "/api/sessions/s1", method="POST")
    assert code == 404
    code, body = _get(server + "/api/sessions/../../etc", method="GET")
    assert code == 404  # 路径穿越不出去（id 白名单）


def test_http_turn_job_flow(server, monkeypatch):
    """C4 写通道：POST turn → 作业排队 → 执行线程 → done。

    _execute_turn 被 monkeypatch（不真跑 agent）——测的是作业协议。
    """
    import time as _time
    seen = {}

    def fake_execute(job_id, task_id, content):
        seen["job"] = (job_id, task_id, content)
        _time.sleep(0.2)
        serve._JOBS[job_id]["status"] = "running"
        serve._JOBS[job_id]["status"] = "done"
        serve._JOBS[job_id]["answer"] = "完成"

    monkeypatch.setattr(serve, "_execute_turn", fake_execute)
    body = json.dumps({"content": "继续推进"}).encode("utf-8")
    req = urllib.request.Request(server + "/api/sessions/s1/turn",
                                 data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:
        assert r.status == 202
        job_id = json.loads(r.read())["job_id"]
    for _ in range(40):
        code, j = _get(server + f"/api/jobs/{job_id}")
        if j["status"] == "done":
            break
        _time.sleep(0.1)
    assert j["status"] == "done" and j["answer"] == "完成"
    assert seen["job"][1] == "s1" and seen["job"][2] == "继续推进"


def test_http_turn_rejects_empty_and_busy(server, monkeypatch):
    body = json.dumps({"content": "  "}).encode("utf-8")
    req = urllib.request.Request(server + "/api/sessions/s1/turn",
                                 data=body, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            code = r.status
    except urllib.error.HTTPError as e:
        code = e.code
    assert code == 400  # 空内容

    serve._JOBS["busy1"] = {"job_id": "busy1", "task_id": "s1",
                            "status": "running"}
    try:
        req = urllib.request.Request(server + "/api/sessions/s1/turn",
                                     data=json.dumps({"content": "x"}).encode(),
                                     method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                code = r.status
        except urllib.error.HTTPError as e:
            code = e.code
            assert code == 409  # 上一轮还在跑
    finally:
        serve._JOBS.pop("busy1", None)


def test_http_serves_index(server):
    with urllib.request.urlopen(server + "/", timeout=5) as r:
        html = r.read().decode("utf-8")
    assert r.status == 200 and "WOVRA" in html


def test_http_view_endpoint(tmp_path, monkeypatch):
    """上下文视图端点：物化某 agent 的装配（检查分裂/重组用）。"""
    import urllib.error
    import urllib.parse

    tasks = tmp_path / "tasks"
    (tasks / "s1").mkdir(parents=True)
    data = _fake_task()
    data["registry"] = [{"id": "A", "name": "主agent", "status": "active"}]
    data["rounds"][0]["domains"] = [
        {"name": "工具层", "description": "工具实现",
         "file_domains": ["src/wovra/tools/"], "block_ids": ["R1-B1"]}]
    (tasks / "s1" / "task.json").write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(task_module, "TASKS_ROOT", tasks)

    class _H(serve._Handler):
        pass

    _H.cache = serve.SummaryCache(tasks)
    _H.tasks_root = tasks
    httpd = serve.ThreadingHTTPServer(("127.0.0.1", 0), _H)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    try:
        url = base + "/api/sessions/s1/views/" + urllib.parse.quote("工具层")
        with urllib.request.urlopen(url, timeout=10) as r:
            assert r.status == 200
            body = json.loads(r.read().decode("utf-8"))
        assert body["available"] is True
        assert body["messages"], "视图物化应至少产出系统/职责表段"
        assert body["total_chars"] > 0
        # 不存在的会话 → 404
        try:
            urllib.request.urlopen(base + "/api/sessions/nope/views/x",
                                   timeout=5)
            raise AssertionError("应 404")
        except urllib.error.HTTPError as e:
            assert e.code == 404
    finally:
        httpd.shutdown()


def test_http_ask_bridge_flow(server, monkeypatch, tmp_path):
    """C4.5 交互桥：轮内 ask_user/敏感确认路由到网页——POST answer 放行。

    _build_agent 替身为假 agent：run() 里调用被桥接的 ask_user 与
    _ask_yes_no，验证 pending 发布 → answer 回填 → 轮完成的全链路。
    """
    import time as _time

    from wovra.cli import prompt as cli_prompt
    from wovra.tools import safety as safety_mod

    tasks = tmp_path / "tasks3"
    (tasks / "s1").mkdir(parents=True)
    (tasks / "s1" / "task.json").write_text(
        json.dumps(_fake_task(), ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(task_module, "TASKS_ROOT", tasks)

    captured = {}

    class FakeAgent:
        def run(self, content, **kw):
            captured["ask"] = cli_prompt.ask_user("选哪种模式？", "A|B", False)
            captured["confirm"] = safety_mod._ask_yes_no("允许安装依赖？")
            return "已按选择执行"

        def organize_backlog(self):
            pass

    def fake_build(task, mode="managed", **kw):
        return FakeAgent()

    monkeypatch.setattr(cli_prompt, "_build_agent", fake_build)

    body = json.dumps({"content": "继续"}).encode("utf-8")
    req = urllib.request.Request(server + "/api/sessions/s1/turn",
                                 data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:
        assert r.status == 202
        job_id = json.loads(r.read())["job_id"]

    # 等待 pending 问题出现（ask 先到）
    pending = None
    for _ in range(60):
        code, j = _get(server + f"/api/jobs/{job_id}")
        if j.get("pending"):
            pending = j["pending"]
            break
        _time.sleep(0.05)
    assert pending and pending["type"] == "ask"
    assert pending["question"] == "选哪种模式？"
    assert pending["choices"] == ["A", "B"]

    # 回答选项 A → 桥接展开后进入 confirm
    req = urllib.request.Request(server + f"/api/jobs/{job_id}/answer",
                                 data=json.dumps({"answer": "A"}).encode(),
                                 method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:
        assert r.status == 200

    # 等待 confirm 出现
    pending2 = None
    for _ in range(60):
        code, j = _get(server + f"/api/jobs/{job_id}")
        if j.get("pending"):
            pending2 = j["pending"]
            break
        _time.sleep(0.05)
    assert pending2 and pending2["type"] == "confirm"

    # 回答 y → 放行 → 轮完成
    req = urllib.request.Request(server + f"/api/jobs/{job_id}/answer",
                                 data=json.dumps({"answer": "y"}).encode(),
                                 method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:
        assert r.status == 200

    for _ in range(60):
        code, j = _get(server + f"/api/jobs/{job_id}")
        if j["status"] == "done":
            break
        _time.sleep(0.05)
    assert j["status"] == "done"
    assert captured["ask"] == "用户的回答: A"        # 字母 → 选项原文展开
    assert captured["confirm"] is True               # y → 放行
    serve._JOBS.pop(job_id, None)
