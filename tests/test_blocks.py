"""Block 结构化的离线测试：纯函数、零 LLM、全确定性。"""

import json

from wovra import blocks


def _event(seq: int, n: int, etype: str, tool: str = "", args: dict | None = None,
           content: str = "", tool_calls: list | None = None) -> dict:
    """构造一个最小事件：tool_call 支持 多调用（tool_calls 直传）。"""
    if etype == "tool_call":
        if tool_calls is None:
            tool_calls = [{
                "id": f"c{n}", "type": "function",
                "function": {"name": tool, "arguments": json.dumps(args or {})},
            }]
        message = {"role": "assistant", "content": "", "tool_calls": tool_calls}
    elif etype == "tool_result":
        message = {"role": "tool", "tool_call_id": f"c{n - 1}", "content": content}
    else:
        message = {"role": "user" if etype == "user" else "assistant",
                   "content": content}
    return {"id": f"R{seq}-E{n:02d}", "type": etype, "message": message,
            "truncated": "", "status": ""}


def _round(seq: int, events: list) -> dict:
    return {"seq": seq, "events": events, "end_state": "completed",
            "user_input": {"original": "", "normalized": ""}, "refined_index": {}}


# ---- 命令标签 ----------------------------------------------------------------


def test_tag_command_six_categories():
    """六类标签各就各位；安装类优先于测试（pip install pytest 是环境）。"""
    cases = {
        "pytest -q tests/": "test",
        "python -m pytest -q": "test",
        "python -m unittest discover": "test",
        "npm test": "test",
        "npm run build": "build",
        "gcc main.c -o main": "build",
        "python -m py_compile app.py": "build",
        "pip install numpy": "environment",
        "pip install pytest": "environment",  # 安装优先，不是 test
        "uv add rich": "environment",
        "npm install": "environment",
        "python -m venv .venv": "environment",
        "apt-get install curl": "environment",
        "export WOVRA_SOLO=1": "environment",
        "uvicorn app:app --port 8000": "environment",
        "python -m http.server 8000": "environment",
        "mkdir -p src/utils": "file",
        "cp a.txt b.txt": "file",
        "grep -rn TODO src/": "file",
        "python main.py": "run",
        "node server.js": "run",
        "./scripts/run.sh": "run",
        "bash setup.sh": "run",
        "git status": "other",
        "curl https://example.com": "other",
        "echo hello": "other",
    }
    for command, expected in cases.items():
        assert blocks.tag_command(command) == expected, command


# ---- 切块规则 ----------------------------------------------------------------


def test_write_is_block_cutoff():
    """规格样例：读+写 icp 一块；读 ui+写 app 一块；环境自成一块；
    测试与最终回答一块——写/改文件是块的截止。"""
    r = _round(17, [
        _event(17, 1, "user", content="调整 ICP 和 UI"),
        _event(17, 2, "tool_call", tool="read_file", args={"path": "icp.py"}),
        _event(17, 3, "tool_result", content="..."),
        _event(17, 4, "tool_call", tool="write_file", args={"path": "icp.py"}),
        _event(17, 5, "tool_result", content="ok"),
        _event(17, 6, "tool_call", tool="read_file", args={"path": "ui.py"}),
        _event(17, 7, "tool_result", content="..."),
        _event(17, 8, "tool_call", tool="edit_file",
               args={"path": "app.py", "old_text": "a", "new_text": "b"}),
        _event(17, 9, "tool_result", content="ok"),
        _event(17, 10, "tool_call", tool="run_command",
               args={"command": "pip install numpy"}),
        _event(17, 11, "tool_result", content="installed"),
        _event(17, 12, "tool_call", tool="run_command",
               args={"command": "pytest -q"}),
        _event(17, 13, "tool_result", content="1 passed"),
        _event(17, 14, "final_answer", content="完成了"),
    ])
    bs = blocks.segment_round(r)

    assert [b["id"] for b in bs] == ["R17-B1", "R17-B2", "R17-B3", "R17-B4"]
    # B1：读+写 icp.py，截止于写
    assert bs[0]["start_event"] == "R17-E01" and bs[0]["end_event"] == "R17-E05"
    assert bs[0]["wrote_files"] == ["icp.py"]
    assert bs[0]["touched_files"] == ["icp.py"]
    # B2：读 ui + 改 app（规格样例里它们同块，截止于改）
    assert bs[1]["wrote_files"] == ["app.py"]
    assert bs[1]["touched_files"] == ["ui.py", "app.py"]
    # B3：环境块自成一块
    assert bs[2]["kind"] == "environment"
    assert bs[2]["command_types"] == ["environment"]
    assert bs[2]["start_event"] == "R17-E10" and bs[2]["end_event"] == "R17-E11"
    # B4：测试 + 最终回答跟随同一工作块
    assert bs[3]["command_types"] == ["test"]
    assert bs[3]["end_event"] == "R17-E14"
    # 可持久化：无私有字段，能直接 json 落盘
    assert json.dumps(bs, ensure_ascii=False)


def test_round_without_writes_is_single_block():
    """全轮没有写/改 → 整轮一块（总结成一段话是整理的事）。"""
    r = _round(3, [
        _event(3, 1, "user", content="解释一下"),
        _event(3, 2, "tool_call", tool="read_file", args={"path": "a.py"}),
        _event(3, 3, "tool_result", content="..."),
        _event(3, 4, "final_answer", content="解释如下"),
    ])
    bs = blocks.segment_round(r)
    assert len(bs) == 1
    assert bs[0]["start_event"] == "R3-E01" and bs[0]["end_event"] == "R3-E04"
    assert bs[0]["wrote_files"] == []


def test_merged_user_inputs_start_new_blocks():
    """合并轮（中断续传）里第二条用户输入开新块：新输入 = 新工作脉络。"""
    r = _round(5, [
        _event(5, 1, "user", content="先做 A"),
        _event(5, 2, "tool_call", tool="write_file", args={"path": "a.py"}),
        _event(5, 3, "tool_result", content="ok"),
        _event(5, 4, "user", content="再做 B"),
        _event(5, 5, "tool_call", tool="write_file", args={"path": "b.py"}),
        _event(5, 6, "tool_result", content="ok"),
    ])
    bs = blocks.segment_round(r)
    assert len(bs) == 2
    assert bs[0]["wrote_files"] == ["a.py"]
    assert bs[1]["wrote_files"] == ["b.py"]
    assert bs[1]["start_event"] == "R5-E04"


def test_environment_commands_group_and_isolate():
    """环境命令自成环境块且连续合并；用户开口与后续工作各自成块。"""
    r = _round(9, [
        _event(9, 1, "user", content="搭环境跑测试"),
        _event(9, 2, "tool_call", tool="run_command",
               args={"command": "uv add rich"}),
        _event(9, 3, "tool_result", content="ok"),
        _event(9, 4, "tool_call", tool="run_command",
               args={"command": "export WOVRA_CACHE_RATE=30"}),
        _event(9, 5, "tool_result", content="ok"),
        _event(9, 6, "tool_call", tool="run_command",
               args={"command": "pytest -q"}),
        _event(9, 7, "tool_result", content="ok"),
    ])
    bs = blocks.segment_round(r)
    assert len(bs) == 3
    # 用户的请求单独成块（环境是另一"性质"的工作，天然隔离）
    assert bs[0]["kind"] == "work"
    assert bs[0]["start_event"] == "R9-E01" and bs[0]["end_event"] == "R9-E01"
    # 连续两条环境命令合一个环境块
    assert bs[1]["kind"] == "environment"
    assert bs[1]["command_types"] == ["environment"]
    assert bs[1]["start_event"] == "R9-E02" and bs[1]["end_event"] == "R9-E05"
    # 环境块之后接工作事件必开新块
    assert bs[2]["kind"] == "work"
    assert bs[2]["command_types"] == ["test"]
    assert bs[2]["start_event"] == "R9-E06"


def test_empty_round_yields_no_blocks():
    """没有事件的轮不产生块（防御：不入 blocks 键也不报错）。"""
    assert blocks.segment_round(_round(1, [])) == []


# ---- 渲染 --------------------------------------------------------------------


def test_block_digest_routes_not_reproduces():
    """块摘要 = 路由式信息（动作+对象），不复制正文——机制二输入。"""
    import json as _json

    events = [
        _event(9, 1, "user", content="加测试和 agent 演示包"),
        _event(9, 2, "tool_call", tool="write_file",
               args={"path": "js/agent-pack.js", "content": "长内容" * 500}),
        _event(9, 3, "tool_result", content="ok"),
        _event(9, 4, "tool_call", tool="run_command", args={"command": "node tests/run.js"}),
        _event(9, 5, "tool_result", content="5 套件通过"),
        {"id": "R9-E06", "type": "final_answer",
         "message": {"role": "assistant", "content": "全部完成并验证"}},
    ]
    r = _round(9, events)
    bs = blocks.segment_round(r)
    digest = "\n\n".join(blocks.block_digest(r, b) for b in bs)

    assert "R9-B1" in digest and "write_file → js/agent-pack.js" in digest
    assert "[run] node tests/run.js" in digest
    assert "最终回答头" in digest
    assert "长内容" * 500 not in digest  # 正文不复制


def test_render_round_shows_commands_and_writes():
    """人读视图：块行带事件区间/文件/标签，命令原文缩进可核对。

    切块语义顺带验证（v3 主线，2026-09-11 起 render_round 缺省用
    segment_round_by_file）：验证类命令被吸收进被验证的文件块，不另起块。
    """
    r = _round(2, [
        _event(2, 1, "user", content="修一下"),
        _event(2, 2, "tool_call", tool="edit_file",
               args={"path": "app.py", "old_text": "a", "new_text": "b"}),
        _event(2, 3, "tool_result", content="ok"),
        _event(2, 4, "tool_call", tool="run_command",
               args={"command": "pytest -q tests/"}),
        _event(2, 5, "tool_result", content="1 passed"),
    ])
    out = blocks.render_round(r)
    assert "R2 · 5 事件 · 1 块" in out
    assert "写: app.py" in out
    assert "[test]" in out
    assert "▸ [test] pytest -q tests/" in out
    assert "✎ edit_file(app.py)" in out


# ---- v3：按文件聚合的块切分（用户格式规格，零 LLM） -------------------------


def test_segment_by_file_basic_layout():
    """一轮 → 只有文件块：一个文件的所有交互聚合一块；非文件工具与
    助手结论并入当前活动块（最近的文件块）；用户输入不进块。"""
    r = _round(7, [
        _event(7, 1, "user", content="加个黑夜"),
        _event(7, 2, "tool_call", tool="write_file",
               args={"path": "world.js", "content": "x" * 100}),
        _event(7, 3, "tool_result", content="ok"),
        _event(7, 4, "tool_call", tool="read_file", args={"path": "world.js"}),
        _event(7, 5, "tool_result", content="line1"),
        _event(7, 6, "tool_call", tool="edit_file",
               args={"path": "renderer.js", "old_text": "a", "new_text": "b"}),
        _event(7, 7, "tool_result", content="ok"),
        _event(7, 8, "tool_call", tool="run_command",
               args={"command": "pytest -q tests/"}),
        _event(7, 9, "tool_result", content="1 passed"),
        _event(7, 10, "final_answer", content="完成"),
    ])
    bs = blocks.segment_round_by_file(r)
    assert [(b["kind"], b.get("file", "")) for b in bs] == [
        ("file", "world.js"),
        ("file", "renderer.js"),
    ]
    # 同一文件的读+写聚合在同一块，ops 保留时序
    world = bs[0]
    assert world["ops"] == [
        {"e": "R7-E02", "op": "write", "c": "c2"},
        {"e": "R7-E04", "op": "read", "c": "c4"},
    ]
    assert world["events"] == ["R7-E02", "R7-E03", "R7-E04", "R7-E05"]
    # 测试命令与最终回答并入最后一个活动块（renderer.js）
    assert bs[1]["events"] == [
        "R7-E06", "R7-E07", "R7-E08", "R7-E09", "R7-E10",
    ]
    assert bs[1]["command_types"] == ["test"]


def test_segment_by_file_interleaved_same_file():
    """同一文件的操作被其他工具隔开：仍聚合为同一文件块。"""
    r = _round(3, [
        _event(3, 1, "user", content="修"),
        _event(3, 2, "tool_call", tool="write_file",
               args={"path": "a.js", "content": "x"}),
        _event(3, 3, "tool_result", content="ok"),
        _event(3, 4, "tool_call", tool="run_command",
               args={"command": "node a.js"}),
        _event(3, 5, "tool_result", content="ok"),
        _event(3, 6, "tool_call", tool="edit_file",
               args={"path": "a.js", "old_text": "x", "new_text": "y"}),
        _event(3, 7, "tool_result", content="ok"),
        _event(3, 8, "final_answer", content="done"),
    ])
    bs = blocks.segment_round_by_file(r)
    assert len(bs) == 1 and bs[0]["file"] == "a.js"
    assert bs[0]["ops"] == [
        {"e": "R3-E02", "op": "write", "c": "c2"},
        {"e": "R3-E06", "op": "edit", "c": "c6"},
    ]
    assert bs[0]["events"] == [
        "R3-E02", "R3-E03", "R3-E04", "R3-E05", "R3-E06", "R3-E07", "R3-E08",
    ]


def test_segment_by_file_environment_and_chat_only():
    """环境命令独立成环境块（本轮合并为一块）；纯聊天轮 → 保底 1 块。"""
    r = _round(4, [
        _event(4, 1, "user", content="装依赖"),
        _event(4, 2, "tool_call", tool="run_command",
               args={"command": "pip install numpy"}),
        _event(4, 3, "tool_result", content="ok"),
        _event(4, 4, "final_answer", content="好了"),
    ])
    bs = blocks.segment_round_by_file(r)
    assert [(b["kind"], b.get("command_types")) for b in bs] == [
        ("environment", ["environment"]),
    ]
    r2 = _round(5, [
        _event(5, 1, "user", content="你好"),
        _event(5, 2, "final_answer", content="你好呀"),
    ])
    bs2 = blocks.segment_round_by_file(r2)
    assert [b["kind"] for b in bs2] == ["fallback"]
    assert bs2[0]["events"] == ["R5-E01", "R5-E02"]  # 纯聊天轮保底块含全部事件


def test_segment_by_file_delete_op():
    """delete_file 计入文件块的 op 序列。"""
    r = _round(6, [
        _event(6, 1, "user", content="删掉旧版"),
        _event(6, 2, "tool_call", tool="delete_file", args={"path": "old.js"}),
        _event(6, 3, "tool_result", content="已归档删除"),
        _event(6, 4, "final_answer", content="删了"),
    ])
    bs = blocks.segment_round_by_file(r)
    old = [b for b in bs if b.get("file") == "old.js"][0]
    assert old["kind"] == "file"
    assert old["ops"] == [{"e": "R6-E02", "op": "delete", "c": "c2"}]


def test_segment_by_file_failed_call_falls_back():
    """参数截断的文件操作（解析不出 path）不产生空文件块；整轮无文件/
    环境交互时保底 1 块（R28 三次 write_file 参数被掐断的实证场景）。"""
    r = _round(8, [
        _event(8, 1, "user", content="写"),
        _event(8, 2, "tool_call", tool="write_file",
               args={"content": "…被截断的 JSON…"}),
        _event(8, 3, "tool_result", content="工具参数不是合法 JSON"),
        _event(8, 4, "final_answer", content="失败"),
    ])
    bs = blocks.segment_round_by_file(r)
    assert len(bs) == 1 and bs[0]["kind"] == "fallback"
    assert all(not b.get("file") for b in bs)


def test_segment_by_file_parallel_batch_dedup():
    """并行批次的同一事件只计入归属块一次（不按调用数翻倍）。"""
    r = _round(9, [
        _event(9, 1, "user", content="并行"),
        _event(9, 2, "tool_call", tool_calls=[
            {"id": "c2a", "type": "function",
             "function": {"name": "run_command",
                          "arguments": json.dumps({"command": "pytest"})}},
            {"id": "c2b", "type": "function",
             "function": {"name": "run_command",
                          "arguments": json.dumps({"command": "node a.js"})}},
        ]),
        _event(9, 3, "tool_result", content="ok"),
        _event(9, 4, "tool_result", content="ok"),
        _event(9, 5, "final_answer", content="ok"),
    ])
    bs = blocks.segment_round_by_file(r)
    assert len(bs) == 1 and bs[0]["kind"] == "fallback"
    assert bs[0]["events"] == ["R9-E01", "R9-E02", "R9-E03", "R9-E04", "R9-E05"]



def test_segment_by_file_ghost_and_escape_blocks():
    """读全失败的文件块保留并分类：FileNotFoundError→幽灵（文件从未存在）、
    路径越界→越界（安全拦截）、通用工具出错→不作分类（不用管）。"""
    r = _round(10, [
        _event(10, 1, "user", content="测试读"),
        _event(10, 2, "tool_call", tool="read_file", args={"path": "nope.txt"}),
        _event(10, 3, "tool_result",
               content="工具执行出错: FileNotFoundError(2, 'No such file or directory')"),
        _event(10, 4, "tool_call", tool="read_file",
               args={"path": "../outside.py"}),
        _event(10, 5, "tool_result",
               content="工具执行出错: ValueError('路径越界，只允许访问项目目录内的文件: ../outside.py')"),
        _event(10, 6, "tool_call", tool="read_file", args={"path": "ok.txt"}),
        _event(10, 7, "tool_result", content="ok.txt 内容"),
        _event(10, 8, "tool_call", tool="read_file", args={"path": "boom.txt"}),
        _event(10, 9, "tool_result", content="工具执行出错: 编码错误"),
        _event(10, 10, "final_answer", content="完成"),
    ])
    bs = blocks.segment_round_by_file(r)
    nope = [b for b in bs if b.get("file") == "nope.txt"][0]
    assert nope.get("fail_tags") == ["幽灵"]
    esc = [b for b in bs if b.get("file") == "../outside.py"][0]
    assert esc.get("fail_tags") == ["越界"]
    ok = [b for b in bs if b.get("file") == "ok.txt"][0]
    assert not ok.get("fail_tags")
    boom = [b for b in bs if b.get("file") == "boom.txt"][0]
    assert not boom.get("fail_tags")  # 通用工具出错：不用管，不作分类



def test_segment_by_file_read_success_is_not_ghost():
    """回归（2026-09-11 机制评审）：读成功、正文含"不存在"字面量 → 不标幽灵。

    旧判定是全文子串匹配（`"不存在" in content`），读本项目
    src/wovra/tools/files.py 这类文件时正文里就有 `文件不存在: {path}`
    文案，于是整块被打成【(DEAD)：幽灵】。该标签是整理指令的输入，会让
    组织器把 live 文件当成从未存在的幽灵。修法 = 只看首行。
    """
    r = _round(11, [
        _event(11, 1, "user", content="读源码"),
        _event(11, 2, "tool_call", tool="read_file",
               args={"path": "src/wovra/tools/files.py"}),
        _event(11, 3, "tool_result",
               content="src/wovra/tools/files.py（共 704 行，以下为第 1-200 行）\n"
                       '    return f"文件不存在: {path}（解析为 {target}）。"'),
        _event(11, 4, "final_answer", content="读完"),
    ])
    blk = [b for b in blocks.segment_round_by_file(r)
           if b.get("file") == "src/wovra/tools/files.py"][0]
    assert not blk.get("fail_tags"), "读成功的块不该被打成幽灵"
    # 标签行给出 LIVE 与"只读"，而不是 DEAD/幽灵
    label = blocks.label_line(blk, {}, "live")
    assert label.startswith("【src/wovra/tools/files.py(live)】")
    assert "幽灵" not in label

    # 对照：真失败仍必须分类（不能因为改口径就漏判）
    assert blocks._op_failure(
        {"op": "read", "c": "c1"},
        {"c1": "文件不存在: nope.txt（解析为 /x）"},
    ) == "幽灵"
    assert blocks._op_failure(
        {"op": "read", "c": "c2"},
        {"c2": "工具执行出错: ValueError('路径越界，只允许访问项目目录内的文件: ../x')"},
    ) == "越界"
    assert blocks._op_failure(
        {"op": "read", "c": "c3"}, {"c3": "工具执行出错: 编码错误"},
    ) is None  # 通用工具出错：不作分类


def test_segment_by_file_user_supplement_block():
    """被打断后补充的用户输入 → 用户块（时间序就位）；首条用户输入仍
    由轮头承载。"""
    r = _round(11, [
        _event(11, 1, "user", content="写个功能"),
        _event(11, 2, "tool_call", tool="write_file",
               args={"path": "a.js", "content": "x"}),
        _event(11, 3, "tool_result", content="ok"),
        _event(11, 4, "user", content="等等，加个约束：不要用全局变量"),
        _event(11, 5, "tool_call", tool="edit_file",
               args={"path": "a.js", "old_text": "x", "new_text": "y"}),
        _event(11, 6, "tool_result", content="ok"),
        _event(11, 7, "final_answer", content="done"),
    ])
    bs = blocks.segment_round_by_file(r)
    kinds = [b["kind"] for b in bs]
    # 同一文件聚合为一块（含用户补充前后的两次操作），用户补充独立成块
    assert kinds == ["file", "user"]
    assert bs[1]["events"] == ["R11-E04"]
    a_blocks = [b for b in bs if b.get("file") == "a.js"]
    assert len(a_blocks) == 1
    assert a_blocks[0]["ops"] == [
        {"e": "R11-E02", "op": "write", "c": "c2"},
        {"e": "R11-E05", "op": "edit", "c": "c5"},
    ]



def test_segment_by_file_block_level_tools():
    """工具事件确定性吸收进具体文件块：谁吸收谁打 has_tools（块级精确，
    不整轮打标）。"""
    r = _round(12, [
        _event(12, 1, "user", content="干活"),
        _event(12, 2, "tool_call", tool="write_file",
               args={"path": "a.js", "content": "x"}),
        _event(12, 3, "tool_result", content="ok"),
        _event(12, 4, "tool_call", tool="run_command",
               args={"command": "pytest -q tests/"}),
        _event(12, 5, "tool_result", content="1 passed"),
        _event(12, 6, "tool_call", tool="write_file",
               args={"path": "b.js", "content": "y"}),
        _event(12, 7, "tool_result", content="ok"),
        _event(12, 8, "final_answer", content="done"),
    ])
    bs = blocks.segment_round_by_file(r)
    a = [b for b in bs if b.get("file") == "a.js"][0]
    b = [b for b in bs if b.get("file") == "b.js"][0]
    assert a.get("has_tools") is True   # pytest 吸收进 a.js 块
    assert not b.get("has_tools")       # b.js 只写了，没吸收工具


def test_segment_by_file_verify_vs_research_tools():
    """工具归属 v2：验证类（test/build/run）吸收进文件块并打「工具」；
    调研类（file 标签命令 / web_search / list_files）吸收但不打标——
    文件块的「工具」只代表有验证/执行类工具。"""
    r = _round(13, [
        _event(13, 1, "user", content="干活"),
        _event(13, 2, "tool_call", tool="write_file",
               args={"path": "a.js", "content": "x"}),
        _event(13, 3, "tool_result", content="ok"),
        _event(13, 4, "tool_call", tool="run_command",
               args={"command": "grep -rn TODO src/"}),   # 调研类
        _event(13, 5, "tool_result", content="无结果"),
        _event(13, 6, "tool_call", tool="web_search", args={"query": "x"}),
        _event(13, 7, "tool_result", content="结果"),
        _event(13, 8, "tool_call", tool="run_command",
               args={"command": "pytest -q tests/"}),     # 验证类
        _event(13, 9, "tool_result", content="1 passed"),
        _event(13, 10, "final_answer", content="done"),
    ])
    bs = blocks.segment_round_by_file(r)
    a = [b for b in bs if b.get("file") == "a.js"][0]
    assert a.get("has_tools") is True  # pytest 验证类 → 打标
    # 只保留验证类：grep/web_search 吸收进块但不触发打标（无法单独断言，
    # 但验证类存在即说明调研类没把它冲掉——事件都在块里）
    assert all(e in a["events"] for e in
               ["R13-E04", "R13-E05", "R13-E06", "R13-E07", "R13-E08", "R13-E09"])


def test_segment_by_file_research_only_no_tool_tag():
    """只有调研类工具（无验证类）：文件块不打「工具」。"""
    r = _round(14, [
        _event(14, 1, "user", content="看看"),
        _event(14, 2, "tool_call", tool="write_file",
               args={"path": "b.js", "content": "y"}),
        _event(14, 3, "tool_result", content="ok"),
        _event(14, 4, "tool_call", tool="run_command",
               args={"command": "ls -la src/"}),          # 调研类
        _event(14, 5, "tool_result", content="..."),
        _event(14, 6, "final_answer", content="done"),
    ])
    bs = blocks.segment_round_by_file(r)
    b = [b for b in bs if b.get("file") == "b.js"][0]
    assert not b.get("has_tools")


def test_segment_by_file_chat_supplement_merges_into_fallback():
    """纯聊天轮（只有保底块）的中途用户补充并入保底块，不单独成块——
    需求（轮头用户输入）先于结论；用户块只在有文件/环境交互的轮出现。"""
    r = _round(15, [
        _event(15, 1, "user", content="问"),
        _event(15, 2, "tool_call", tool="web_search", args={"query": "x"}),
        _event(15, 3, "tool_result", content="结果"),
        _event(15, 4, "user", content="等等，补充一下"),
        _event(15, 5, "final_answer", content="回答"),
    ])
    bs = blocks.segment_round_by_file(r)
    assert len(bs) == 1 and bs[0]["kind"] == "fallback"
    assert bs[0]["events"] == [
        "R15-E01", "R15-E02", "R15-E03", "R15-E04", "R15-E05",
    ]  # 全部事件（含补充），轮头渲染时并入用户输入


def test_symlink_security_messages_classify_correctly():
    """安全加固后的新返回文案，在块层分类正确（端到端锁）。

    三件事一起验：
    * 读界外链接被拦 → 越界（不是幽灵）；
    * 删悬空链接成功 → 正常删除块，**不**打幽灵（文案改动前会误判）；
    * 普通写入不受影响。
    """
    from wovra.blocks import label_line, segment_round_by_file

    def ev(i, etype, msg):
        return {"id": f"E{i}", "type": etype, "message": msg}

    round_ = {
        "seq": 1,
        "round_user_input": "链接边界",
        "events": [
            ev(1, "user_input", {"content": "链接边界"}),
            ev(2, "tool_call", {"tool_calls": [{
                "id": "c1", "function": {"name": "read_file",
                                         "arguments": '{"path": "link_escape.md"}'}}]}),
            ev(3, "tool_result", {"tool_call_id": "c1", "content":
                "工具执行出错: ValueError('路径越界，只允许访问项目目录内的文件: "
                "link_escape.md（该路径是指向工作区之外的符号链接；允许的根目录: /tmp/ws）')"}),
            ev(4, "tool_call", {"tool_calls": [{
                "id": "c2", "function": {"name": "delete_file",
                                         "arguments": '{"path": "dangling.txt"}'}}]}),
            ev(5, "tool_result", {"tool_call_id": "c2", "content":
                "已删除符号链接 dangling.txt → /tmp/x（该链接原本已失效）"}),
            ev(6, "tool_call", {"tool_calls": [{
                "id": "c3", "function": {"name": "write_file",
                                         "arguments": '{"path": "real.py", "content": "x=1"}'}}]}),
            ev(7, "tool_result", {"tool_call_id": "c3", "content": "已创建 real.py（3 字符）"}),
            ev(8, "assistant", {"content": "完成"}),
        ],
    }
    segments = {b.get("file"): b for b in segment_round_by_file(round_) if b.get("file")}

    assert segments["link_escape.md"].get("fail_tags") == ["越界"]
    assert "fail_tags" not in segments["dangling.txt"], "删悬空链接是成功，不该被标幽灵"
    assert label_line(segments["dangling.txt"], {}, "live").startswith("【dangling.txt(live)】")
