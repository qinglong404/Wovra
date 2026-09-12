"""wovra serve 的契约测试：派生函数 + HTTP 冒烟（本机回环，零模型）。"""

import json
import sys
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
                               "ttft=1.0s finish=stop"},
                    {"kind": "usage", "time": "t2",
                     "detail": "[managed] steps=5 context=50,000 prompt=45,000 "
                               "completion=1,200 缓存命中 43,000 tok（95.6%） "
                               "未命中 2,000 tok（4.4%）"},
                    {"kind": "usage", "time": "t3",
                     "detail": "[managed] steps=2 context=60,000 prompt=30,000 "
                               "completion=800 缓存命中 29,000 tok 未命中 1,000 tok"},
                    {"kind": "usage", "time": "t4",
                     "detail": "[managed] steps=2 context=70,000 prompt=20,000 "
                               "completion=300 缓存命中 19,000 tok 未命中 1,000 tok"}],
        "rounds": [
            {"seq": 1, "user_input": {"original": "干活"}, "end_state": "completed",
             "org_state": "done", "org_generation": 2, "steps_used": 7,
             "events": [{"id": "R1-E01", "type": "user", "status": "",
                         "timestamp": "2026-09-12T10:59:14",
                         "thinking": "推理全文",
                         "message": {"role": "user", "content": "干活"}}],
             "blocks": [{"id": "R1-B1", "file": "a.py", "kind": "file",
                         "events": ["R1-E01"]}]},
            {"seq": 2, "user_input": {"original": "继续"}, "end_state": "open",
             "org_state": "", "steps_used": 2, "events": []},
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
    assert meta["round_list"][0]["t0"] == "2026-09-12T10:59:14"  # 首末事件时间
    assert meta["round_list"][0]["t1"] == "2026-09-12T10:59:14"
    assert meta["round_list"][1]["t0"] == ""             # 无事件轮不瞎编
    assert meta["round_list"][0]["blocks"][0]["id"] == "R1-B1"
    assert meta["registry"][0]["id"] == "A"
    assert "escalations" in meta["task_state"]


def test_http_context_dump(tmp_path, monkeypatch):
    """上下文对照：raw = 全量原文；view = 当前装配视图。"""
    import json as _json
    from dataclasses import asdict as _asdict
    from wovra.task import Task
    tasks = tmp_path / "tasks3"
    (tasks / "c1").mkdir(parents=True)
    task = Task(id="c1", goal="读文档", workspace=str(tmp_path))
    task.rounds = [{
        "seq": 1, "user_input": {"original": "读一下"},
        "events": [
            {"id": "R1-E01", "type": "user", "truncated": "读一下",
             "message": {"role": "user", "content": "读一下"}},
            {"id": "R1-E02", "type": "final_answer", "truncated": "读完了",
             "message": {"role": "assistant", "content": "读完了，结论是 X"}},
        ],
        "end_state": "completed", "org_state": "done",
    }]
    (tasks / "c1" / "task.json").write_text(
        _json.dumps(_asdict(task), ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(task_module, "TASKS_ROOT", tasks)
    raw = serve.context_dump("c1", "raw")
    assert raw["available"] and raw["mode"] == "raw"
    assert raw["messages"][0]["content"] == "读一下"
    assert "未经整理" in raw["note"]
    view = serve.context_dump("c1", "view")
    assert view["available"] and view["mode"] == "view"
    assert view["count"] >= 1 and view["total_chars"] > 0
    assert serve.context_dump("ghost", "raw") is None


def test_round_meta_exposes_split_result():
    """分裂结果透出：已落实字段 + 未落实 pending_org 都要能看见。"""
    data = _fake_task()
    data["rounds"][0]["pending_org"] = {
        "domains": [{"name": "工具层", "description": "守卫与审批",
                     "file_domains": ["src/wovra/tools"]}],
        "split_assessment": {"splittable": True, "reason": "两条独立工作线"},
        "unassigned": ["R1-B9"],
    }
    meta = serve.session_meta("s1", data)
    sp = meta["round_list"][0]["split"]
    assert sp["assessment"]["splittable"] is True
    assert sp["domains"][0]["name"] == "工具层"
    assert sp["unassigned"] == 1 and sp["pending"] is True
    assert meta["round_list"][1]["split"] is None      # 无分裂数据的轮不编造


def test_pending_views_preview_is_readonly(tmp_path, monkeypatch):
    """待生效预览：内存模拟生效能物化域视图，且**不改动会话文件**。"""
    import json as _json
    from dataclasses import asdict as _asdict
    from wovra.task import Task
    tasks = tmp_path / "tasks4"
    (tasks / "p1").mkdir(parents=True)
    task = Task(id="p1", goal="g", workspace=str(tmp_path))
    task.rounds = [{
        "seq": 1, "user_input": {"original": "干活"},
        "events": [{"id": "R1-E01", "type": "user", "truncated": "干活",
                    "message": {"role": "user", "content": "干活"}}],
        "end_state": "completed", "org_state": "done",
        "pending_org": {
            "domains": [{"name": "域甲", "description": "d",
                         "file_domains": ["a.py"]}],
            "split_assessment": {"splittable": True, "reason": "r"},
        },
    }]
    tf = tasks / "p1" / "task.json"
    tf.write_text(_json.dumps(_asdict(task), ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(task_module, "TASKS_ROOT", tasks)
    out = serve.pending_views("p1")
    assert out is not None and "agents" in out
    # 预览绝不"生效"：产物仍留在暂存区、域未写进轮、注册表未长条目
    # （Task.load 自身的块迁移可能重排文件格式，那是幂等的既有行为）
    after = _json.loads(tf.read_text(encoding="utf-8"))
    assert after["rounds"][0].get("pending_org")
    assert not after["rounds"][0].get("domains")
    assert after["registry"] == []          # 注册表未被写入新域（夹具本就是空的）
    assert serve.pending_views("ghost") is None


def test_todo_log_derives_calls():
    """计划页流水：从轮事件抽 todo 调用（动作/文本/结果配对）。"""
    data = _fake_task()
    data["rounds"][0]["events"] += [
        {"id": "R1-E02", "type": "tool_call", "timestamp": "2026-09-12T10:59:20",
         "message": {"role": "assistant", "content": "", "tool_calls": [
             {"id": "t1", "type": "function", "function": {
                 "name": "todo", "arguments": '{"action": "push", "text": "写单测"}'}}]}},
        {"id": "R1-E03", "type": "tool_result", "timestamp": "2026-09-12T10:59:21",
         "message": {"role": "tool", "tool_call_id": "t1", "content": "已加入小步"}},
    ]
    log = serve.todo_log(data)
    assert len(log) == 1
    assert log[0]["action"] == "push" and log[0]["text"] == "写单测"
    assert log[0]["result"] == "已加入小步" and log[0]["seq"] == 1


def test_session_meta_corrects_legacy_window():
    """旧数据把水位 100K 存成 registry window——投影层即时校正为真实窗口。"""
    data = _fake_task()
    data["registry"][0]["window"] = 100_000
    meta = serve.session_meta("s1", data)
    assert meta["registry"][0]["window"] == 1_000_000


def test_round_detail_flattens_events():
    d = serve.round_detail(_fake_task(), 1)
    assert d["user_input"] == "干活"
    assert d["events"][0] == {"id": "R1-E01", "type": "user",
                              "agent": "Main",     # 轮内逐事件归属（route_to 后换手）
                              "time": "2026-09-12T10:59:14",
                              "thinking": "推理全文", "status": "",
                              "role": "user", "content": "干活",
                              "tool_calls": None, "tool_call_id": None}
    assert d["blocks"][0]["file"] == "a.py"
    assert serve.round_detail(_fake_task(), 99) is None


def test_event_agents_handover_after_route_result():
    """route_to 在工具批次跑完后换手：调用与结果归路由方，其后归接手方。"""
    r = {"seq": 1, "active_view": "A", "events": [
        {"id": "R1-E01", "type": "user",
         "message": {"role": "user", "content": "干活"}},
        {"id": "R1-E02", "type": "tool_call",
         "message": {"role": "assistant", "content": "", "tool_calls": [
             {"id": "c1", "function": {"name": "route_to",
                                       "arguments": '{"agent": "A"}'}}]}},
        {"id": "R1-E03", "type": "tool_result",
         "message": {"role": "tool", "tool_call_id": "c1", "content": "已转交"}},
        {"id": "R1-E04", "type": "tool_call",
         "message": {"role": "assistant", "content": "", "tool_calls": [
             {"id": "c2", "function": {"name": "read_file", "arguments": "{}"}}]}},
    ]}
    assert serve._event_agents(r, "Main") == ["Main", "Main", "Main", "A"]


def test_round_usage_map_steps_signature():
    """轮级用量：usage 行按 steps 签名归属（轮1=7步吃两行，轮2=2步吃一行）。"""
    m = serve.round_usage_map(_fake_task())
    assert m[1]["calls"] == 2 and m[1]["prompt"] == 75000
    assert m[1]["cached"] == 72000 and m[1]["miss"] == 3000
    assert m[1]["completion"] == 2000 and m[1]["context"] == 60000
    assert m[1]["steps"] == 7                       # 结算覆盖步数
    assert m[2] == {"steps": 2, "calls": 1, "prompt": 20000, "cached": 19000,
                    "miss": 1000, "completion": 300, "context": 70000}


def test_session_meta_round_list_has_usage():
    meta = serve.session_meta("s1", _fake_task())
    assert meta["round_list"][0]["usage"]["prompt"] == 75000
    assert meta["round_list"][1]["usage"]["calls"] == 1
    # agent_stats 与轮级账同源：无 active_view 的轮归主 agent（哨兵 ID）
    a = meta["agent_stats"][0]
    assert a["agent"] == "Main" and a["prompt"] == 95000 and a["billed_rounds"] == 2


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


def _get(url, method="GET", body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {})
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


def _server_on(tmp_tasks: Path):
    class _H(serve._Handler):
        pass

    _H.cache = serve.SummaryCache(tmp_tasks)
    _H.tasks_root = tmp_tasks
    httpd = serve.ThreadingHTTPServer(("127.0.0.1", 0), _H)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{port}"


def test_http_create_with_workspace(tmp_path, monkeypatch):
    """新建会话选工作目录：记录到 task.workspace、目录自动创建。"""
    tasks = tmp_path / "tasks"
    tasks.mkdir(parents=True)
    monkeypatch.setattr(task_module, "TASKS_ROOT", tasks)
    httpd, base = _server_on(tasks)
    try:
        ws = tmp_path / "my-project"
        req = urllib.request.Request(
            base + "/api/sessions",
            data=json.dumps({"workspace": str(ws)}).encode("utf-8"),
            method="POST", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            assert r.status == 201
            created = json.loads(r.read())
        assert created["workspace"] == str(ws)
        assert ws.is_dir()
        # 落盘验证：task.workspace 已持久化
        on_disk = json.loads((tasks / created["id"] / "task.json")
                             .read_text(encoding="utf-8"))
        assert on_disk["workspace"] == str(ws)
    finally:
        httpd.shutdown()


def test_http_create_rejects_fs_root(tmp_path, monkeypatch):
    tasks = tmp_path / "tasks"
    tasks.mkdir(parents=True)
    monkeypatch.setattr(task_module, "TASKS_ROOT", tasks)
    httpd, base = _server_on(tasks)
    try:
        drive_root = Path(sys.executable).drive + "\\"
        req = urllib.request.Request(
            base + "/api/sessions",
            data=json.dumps({"workspace": drive_root}).encode("utf-8"),
            method="POST", headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                code = r.status
        except urllib.error.HTTPError as e:
            code = e.code
        assert code == 400          # 盘根不可为工作区
    finally:
        httpd.shutdown()


def test_http_delete_session(tmp_path, monkeypatch):
    """删除会话：目录移除 + 摘要缓存清除。"""
    tasks = tmp_path / "tasks"
    (tasks / "s1").mkdir(parents=True)
    (tasks / "s1" / "task.json").write_text(
        json.dumps(_fake_task(), ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(task_module, "TASKS_ROOT", tasks)
    httpd, base = _server_on(tasks)
    try:
        req = urllib.request.Request(base + "/api/sessions/s1",
                                     method="DELETE")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                assert r.status == 200
        except urllib.error.HTTPError as e:
            raise AssertionError(f"删除失败: {e.code}")
        assert not (tasks / "s1").exists()
        # 再 GET → 404
        try:
            with urllib.request.urlopen(base + "/api/sessions/s1", timeout=5) as r:
                raise AssertionError("应 404")
        except urllib.error.HTTPError as e:
            assert e.code == 404
    finally:
        httpd.shutdown()


def test_http_batch_delete(server, tmp_path):
    """批量删除：逐个复用单删判定；失败项逐条报告，不拖累其余。"""
    for sid in ("s2", "s3"):
        d = tmp_path / "tasks" / sid
        d.mkdir()
        (d / "task.json").write_text("{}", encoding="utf-8")
    code, body = _get(server + "/api/sessions", method="DELETE",
                      body={"ids": ["s1", "s2", "ghost"]})
    assert code == 200 and body["deleted"] == ["s1", "s2"]
    assert body["failed"] == [{"id": "ghost", "error": "session not found"}]
    assert not (tmp_path / "tasks" / "s1").exists()
    assert not (tmp_path / "tasks" / "s2").exists()
    assert (tmp_path / "tasks" / "s3").exists()   # 未列入的不动
    code, body = _get(server + "/api/sessions", method="DELETE", body={"ids": []})
    assert code == 400


def test_http_undo_and_local_cmd(server):
    """/undo 只撤开放轮；/cmd 文本命令（todo/help）走任务数据。"""
    code, body = _get(server + "/api/sessions/s1/undo", method="POST", body={})
    assert code == 200 and body["removed_events"] == 0   # s1 最后一轮是开放轮
    code, body = _get(server + "/api/sessions/s1/undo", method="POST", body={})
    assert code == 409                                    # 剩下的是闭合轮
    code, body = _get(server + "/api/sessions/s1/cmd", method="POST",
                      body={"name": "todo"})
    assert code == 200 and isinstance(body["text"], str)
    code, body = _get(server + "/api/sessions/s1/cmd", method="POST",
                      body={"name": "help"})
    assert code == 200 and "/c" in body["text"]
    code, body = _get(server + "/api/sessions/s1/cmd", method="POST",
                      body={"name": "xyz"})
    assert code == 400


def test_project_tree_prunes_and_lists(tmp_path):
    """项目树：列文件、剪掉 .git/node_modules 等重型目录、路径相对化。"""
    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    (root / "src" / "a.py").write_text("x", encoding="utf-8")
    (root / "README.md").write_text("y", encoding="utf-8")
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("z", encoding="utf-8")
    (root / "node_modules").mkdir()
    (root / "node_modules" / "big.js").write_text("q", encoding="utf-8")
    out = serve.project_tree(str(root))
    paths = [f["p"] for f in out["files"]]
    assert paths == ["README.md", "src/a.py"]          # 排序 + 相对路径
    assert out["truncated"] is False
    assert serve.project_tree(str(tmp_path / "nope"))["error"]


def test_http_tree_endpoint(server, tmp_path):
    """GET /tree 用会话 workspace 出树；无 workspace 给空表 + 报因。"""
    code, body = _get(server + "/api/sessions/s1/tree")
    assert code == 200 and "files" in body
    assert body["error"]                               # s1 的 workspace 不存在
    code, body = _get(server + "/api/sessions/ghost/tree")
    assert code == 404


def test_split_choices_fullwidth_separators():
    """选项分割：全角分号/竖线也要拆（实测模型写成一整句的情况）。"""
    from wovra.tools.interaction import _split_choices
    got = _split_choices("只追加 worklog，先不提交；追加 worklog 并提交推送；暂不落账，继续留在工作区")
    assert len(got) == 3
    assert got[0].startswith("只追加 worklog")
    assert "暂不落账" in got[2]
    assert _split_choices("甲｜乙") == ["甲", "乙"]
    assert _split_choices(["A. 一", "B、二"]) == ["一", "二"]


def test_confirm_tag_and_safety_endpoint(server, tmp_path, monkeypatch):
    """确认标签归并（同类判据）+ 安全模式端点 + 会话字段持久化。"""
    from wovra.serve import confirm_tag
    assert confirm_tag("命令包含敏感操作（命中 `\\bgit\\s+(commit|tag)\\b`）") \
        == "cmd:\\bgit\\s+(commit|tag)\\b"
    assert confirm_tag("确认删除文件 D:/x/a.txt？（删除前自动归档）") == "del:D:/x/a.txt"
    assert confirm_tag("随便问问").startswith("q:")

    # 真实 Task（可持久化新字段）挂到 server 的 tasks_root 下
    import json as _json
    from dataclasses import asdict as _asdict
    from wovra.task import Task
    tasks = tmp_path / "tasks2"
    tasks.mkdir()
    t = Task(id="sf1", goal="g")
    (tasks / "sf1").mkdir()
    (tasks / "sf1" / "task.json").write_text(
        _json.dumps(_asdict(t), ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(task_module, "TASKS_ROOT", tasks)
    code, body = _get(server + "/api/sessions/sf1/safety", method="POST",
                      body={"mode": "auto"})
    assert code == 200 and body["mode"] == "auto"
    saved = _json.loads((tasks / "sf1" / "task.json").read_text(encoding="utf-8"))
    assert saved["safety_mode"] == "auto"
    code, body = _get(server + "/api/sessions/sf1/safety", method="POST",
                      body={"mode": "nonsense"})
    assert code == 400


def test_http_sse_stream(server):
    """SSE 端点：分片 data 行 + 终态 done 事件，写完即断。"""
    serve._JOBS["js"] = {"job_id": "js", "task_id": "s1", "status": "done",
                         "live": [{"k": "think", "s": "想"}]}
    try:
        req = urllib.request.Request(server + "/api/jobs/js/stream")
        with urllib.request.urlopen(req, timeout=5) as r:
            body = r.read().decode("utf-8")
            assert r.headers.get("Content-Type", "").startswith(
                "text/event-stream")
        assert '"k": "think"' in body and "data: " in body
        assert "event: done" in body and '"status": "done"' in body
    finally:
        serve._JOBS.pop("js", None)


def test_http_cancel_and_resume(server, monkeypatch):
    """⏹ 终止：置 cancel 标记；/c 续跑：入队 content=None 的作业。"""
    import time as _time
    seen = {}

    def fake_execute(job_id, task_id, content):
        seen[task_id] = content
        serve._JOBS[job_id]["status"] = "running"
        serve._JOBS[job_id]["status"] = "done"

    monkeypatch.setattr(serve, "_execute_turn", fake_execute)
    code, body = _get(server + "/api/sessions/s1/resume", method="POST", body={})
    assert code == 202 and body["job_id"]
    _time.sleep(0.3)
    assert seen.get("s1") is None            # 续跑不注入消息
    # cancel：作业在跑才可终止
    serve._JOBS["jc"] = {"job_id": "jc", "task_id": "s1", "status": "running"}
    try:
        code, body = _get(server + "/api/jobs/jc/cancel", method="POST", body={})
        assert code == 200 and body["ok"] is True
        assert serve._JOBS["jc"]["cancel"] is True
        serve._JOBS["jc"]["status"] = "done"
        code, body = _get(server + "/api/jobs/jc/cancel", method="POST", body={})
        assert code == 409
    finally:
        serve._JOBS.pop("jc", None)


def test_http_shutdown(server):
    """⏻ 关停端点：空闲时先应答后退路；监听关闭后连接被拒。"""
    import time as _time
    code, body = _get(server + "/api/shutdown", method="POST")
    assert code == 200 and body["ok"] is True
    _time.sleep(0.8)   # 等 0.3s 延迟的关停计时器生效
    try:
        urllib.request.urlopen(server + "/api/sessions", timeout=2)
        raise AssertionError("服务应已停止")
    except (urllib.error.URLError, TimeoutError, OSError):
        pass   # 连接拒绝/超时 = 监听已关闭（进程在真实部署随主线程退出）


def test_http_live_stream_and_live_job(server):
    """轮直播：job.live 增量按 after 取；会话元数据带运行中作业 id。"""
    serve._JOBS["jt"] = {"task_id": "s1", "status": "running",
                         "pending": {"type": "confirm", "question": "跑 git tag？"},
                         "live": [{"k": "think", "s": "想"},
                                  {"k": "ans", "s": "答"}]}
    try:
        code, body = _get(server + "/api/jobs/jt/live?after=0")
        assert code == 200 and body["status"] == "running"
        assert len(body["chunks"]) == 2 and body["next"] == 2
        assert body["pending"]["type"] == "confirm"   # 审批栏由轮询驱动
        code, body = _get(server + "/api/jobs/jt/live?after=1")
        assert body["chunks"] == [{"k": "ans", "s": "答"}] and body["next"] == 2
        code, body = _get(server + "/api/jobs/ghost/live")
        assert code == 404
        code, body = _get(server + "/api/sessions/s1")
        assert body["live_job"] == "jt"      # 页面刷新后据此重挂直播
    finally:
        serve._JOBS.pop("jt", None)
    code, body = _get(server + "/api/sessions/s1")
    assert body["live_job"] is None
