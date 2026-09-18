"""文件权限守卫测试（工具层强制，2026-09-12 用户口径，worklog §61）。

```text
P1 自己的文件（清单内）：读/写/改/删
P2 别人的文件：只读
P4 分裂之前（注册表里只有主 agent）：主 agent 全权
P5 分裂之后：主 agent 权限与子 agent 相同
F5 新文件：谁创建谁拥有（创建成功即写进创建者的清单）
```

为什么这些用例必须有："干不干看有没有改写删权"是用户口径，而**提示词只是
纪律、模型可以不听**——只有工具层硬拒才算规则。故每个断言都打在**工具返回值**
上（而不是打在提示词文本上）。
"""

import json

import pytest

from wovra import registry as registry_module
from wovra import task as task_module
from wovra.agent import Agent
from wovra.task import Task
from wovra.tools import (
    delete_file,
    edit_file,
    glob_files,
    list_files,
    move_file,
    read_file,
    replace_lines,
    run_background,
    run_command,
    search_files,
    write_file,
)
from wovra.tools import safety as safety_module

from ._helpers import _StubLLM


def _split_task(tmp_path, monkeypatch) -> Task:
    """一个已经分裂过的任务：工具层管 src/a.py，前端管 webui/x.js。"""
    (tmp_path / "tasks").mkdir(exist_ok=True)
    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path / "tasks")
    monkeypatch.setattr("wovra.tools.safety.PROJECT_ROOT", tmp_path)
    (tmp_path / "src").mkdir(exist_ok=True)
    (tmp_path / "webui").mkdir(exist_ok=True)
    (tmp_path / "src" / "a.py").write_text("print(1)\n", encoding="utf-8")
    (tmp_path / "webui" / "x.js").write_text("// x\n", encoding="utf-8")
    (tmp_path / "legacy.md").write_text("谁都不认领\n", encoding="utf-8")
    task = Task.create(goal="权限测试")
    registry_module.merge_into(task.registry, [
        {"name": "工具层", "files": ["src/a.py"]},
        {"name": "前端", "files": ["webui/x.js"]},
    ])
    return task


def _agent(task: Task, view: str) -> Agent:
    agent = Agent(llm=_StubLLM(), tools=[write_file, edit_file, delete_file,
                                         read_file], task=task)
    agent.current_round = {"seq": 1, "events": [], "active_view": view}
    return agent


def _call(agent: Agent, name: str, args: dict) -> str:
    """跑一次工具调用并取回**工具返回值**（`_execute` 只把结果写进事件）。"""
    return str(agent._invoke_tool(name, json.dumps(args, ensure_ascii=False)))


def test_no_sub_agents_main_has_full_rights(tmp_path, monkeypatch):
    """P4：分裂之前（注册表里只有主 agent）主 agent 全权。"""
    (tmp_path / "tasks").mkdir(exist_ok=True)
    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path / "tasks")
    monkeypatch.setattr("wovra.tools.safety.PROJECT_ROOT", tmp_path)
    (tmp_path / "anything.txt").write_text("原内容\n", encoding="utf-8")
    task = Task.create(goal="未分裂")
    agent = _agent(task, registry_module.MAIN_AGENT_ID)

    out = _call(agent, "write_file", {"path": "anything.txt", "content": "改掉了"})
    assert "已覆盖" in out
    assert (tmp_path / "anything.txt").read_text(encoding="utf-8") == "改掉了"


def test_own_file_is_writable_and_others_are_read_only(tmp_path, monkeypatch):
    """P1/P2：自己的文件能写；别人的文件**只读**，写被硬拒且报错指路。"""
    task = _split_task(tmp_path, monkeypatch)
    agent = _agent(task, "工具层")

    own = _call(agent, "write_file", {"path": "src/a.py", "content": "print(2)"})
    assert "已覆盖" in own

    other = _call(agent, "write_file",
                  {"path": "webui/x.js", "content": "改别人的"})
    assert "权限拒绝" in other and "前端" in other
    assert "route_to" in other                      # 报错即指路
    assert (tmp_path / "webui" / "x.js").read_text(encoding="utf-8") == "// x\n"

    # 别人的文件**读**是允许的（P2 的另一半）
    got = _call(agent, "read_file", {"path": "webui/x.js"})
    assert "// x" in got


def test_edit_and_delete_are_gated_too(tmp_path, monkeypatch):
    """P2：改与删同样被拒（不只是写）。"""
    task = _split_task(tmp_path, monkeypatch)
    agent = _agent(task, "工具层")

    edited = _call(agent, "edit_file",
                   {"path": "webui/x.js", "old_text": "// x", "new_text": "// y"})
    assert "权限拒绝" in edited
    deleted = _call(agent, "delete_file", {"path": "webui/x.js"})
    assert "权限拒绝" in deleted
    assert (tmp_path / "webui" / "x.js").exists()


def test_new_file_is_claimed_by_creator(tmp_path, monkeypatch):
    """F5：新文件放行，且**立即**归属创建者（写进它的清单）。"""
    task = _split_task(tmp_path, monkeypatch)
    agent = _agent(task, "工具层")

    out = _call(agent, "write_file",
                {"path": "src/brand_new.py", "content": "x = 1"})
    assert "已创建" in out
    entry = agent._registry_entry_for("工具层")
    assert "src/brand_new.py" in registry_module.entry_files(entry)
    # 归属变更留痕（独立 kind，不混进 file_change 状态流水）
    assert any(e["kind"] == "ownership" and "brand_new" in e["detail"]
               for e in task.history)


def test_main_agent_is_equal_to_sub_agents_after_split(tmp_path, monkeypatch):
    """P5：分裂之后主 agent 权限**与子 agent 相同**（不能改别人的文件）。"""
    task = _split_task(tmp_path, monkeypatch)
    agent = _agent(task, registry_module.MAIN_AGENT_ID)

    denied = _call(agent, "write_file",
                   {"path": "src/a.py", "content": "主 agent 想改工具层的文件"})
    assert "权限拒绝" in denied and "工具层" in denied
    # 但它照样能新建文件（它就是"写从来没有的新文件"的那个角色）
    ok = _call(agent, "write_file", {"path": "notes/new.md", "content": "新东西"})
    assert "已创建" in ok


def test_existing_unclaimed_file_is_refused_loudly(tmp_path, monkeypatch):
    """F4/P3：**不存在"未认领文件"**——真出现就是缺陷，拒绝并叫人，不静默吸收。"""
    task = _split_task(tmp_path, monkeypatch)
    agent = _agent(task, "工具层")

    out = _call(agent, "write_file", {"path": "legacy.md", "content": "偷偷改掉"})
    assert "权限拒绝" in out and "没有任何域认领" in out
    assert "缺陷" in out and "用户" in out
    assert (tmp_path / "legacy.md").read_text(encoding="utf-8") == "谁都不认领\n"


# ---- 禁写区：tasks/ 只读（2026-09-15 用户拍板："tasks 这个文件夹不允许修改，只可以读"）

def _readonly_env(tmp_path, monkeypatch) -> Task:
    """工作区根下带 tasks/（= 任务库）的环境：会话记录可读、不可写。"""
    (tmp_path / "tasks" / "s1").mkdir(parents=True, exist_ok=True)
    (tmp_path / "tasks" / "s1" / "task.json").write_text(
        '{"id": "s1", "goal": "别动我"}', encoding="utf-8")
    (tmp_path / "src").mkdir(exist_ok=True)
    (tmp_path / "src" / "a.py").write_text("print(1)\n", encoding="utf-8")
    monkeypatch.setattr(task_module, "TASKS_ROOT", tmp_path / "tasks")
    monkeypatch.setattr(safety_module, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(safety_module, "_audit", lambda detail: None)
    monkeypatch.setattr(safety_module, "_ask_yes_no", lambda question: True)
    return Task.create(goal="禁写区测试")


def test_readonly_zone_covers_workspace_tasks_and_task_store(tmp_path, monkeypatch):
    """禁写区 = 工作区下的 tasks/ + 任务库根；两者都按解析后的绝对路径判。"""
    _readonly_env(tmp_path, monkeypatch)
    roots = safety_module.protected_readonly_roots()
    assert (tmp_path / "tasks").resolve() in roots
    assert safety_module.readonly_hit(tmp_path / "tasks" / "s1" / "task.json")
    assert safety_module.readonly_hit(tmp_path / "tasks" / "s1") is not None
    # 反向对照：普通目录不在禁写区
    assert safety_module.readonly_hit(tmp_path / "src" / "a.py") is None


def _full_agent(task: Task) -> Agent:
    """装齐写类工具的 agent（`_agent` 只注册四个，禁写区要逐条通道都试）。"""
    agent = Agent(llm=_StubLLM(),
                  tools=[write_file, edit_file, delete_file, read_file,
                         replace_lines, move_file], task=task)
    agent.current_round = {"seq": 1, "events": [],
                           "active_view": registry_module.MAIN_AGENT_ID}
    return agent


def test_readonly_zone_refuses_every_file_write(tmp_path, monkeypatch):
    """写/改/替换/删/移 —— 每条通道都拒，且会话记录**一个字节不变**。"""
    task = _readonly_env(tmp_path, monkeypatch)
    agent = _full_agent(task)
    record = tmp_path / "tasks" / "s1" / "task.json"
    before = record.read_text(encoding="utf-8")

    for name, args in (
        ("write_file", {"path": "tasks/s1/task.json", "content": "{}",
                        "force": True}),
        ("write_file", {"path": "tasks/s1/new.md", "content": "偷偷加个文件"}),
        ("edit_file", {"path": "tasks/s1/task.json", "old_text": "别动我",
                       "new_text": "动了"}),
        ("replace_lines", {"path": "tasks/s1/task.json", "start_line": 1,
                           "end_line": 1, "new_content": "{}"}),
        ("delete_file", {"path": "tasks/s1/task.json"}),
        ("move_file", {"path": "tasks/s1/task.json", "new_path": "out.json"}),
        ("move_file", {"path": "src/a.py", "new_path": "tasks/a.py"}),
    ):
        out = _call(agent, name, args)
        assert "禁写区" in out, (name, args, out)

    assert record.read_text(encoding="utf-8") == before     # 内容未变
    assert not (tmp_path / "tasks" / "s1" / "new.md").exists()
    assert not (tmp_path / "out.json").exists()
    assert not (tmp_path / "tasks" / "a.py").exists()
    assert (tmp_path / "src" / "a.py").exists()


def test_readonly_zone_still_allows_reading(tmp_path, monkeypatch):
    """反向对照：禁写区**放读**——复盘会话数据是正常需求。"""
    task = _readonly_env(tmp_path, monkeypatch)
    agent = _agent(task, registry_module.MAIN_AGENT_ID)

    assert "别动我" in read_file("tasks/s1/task.json")
    assert "s1" in "".join(list_files("tasks"))
    assert "task.json" in glob_files("*", directory="tasks/s1")
    assert "别动我" in search_files("别动我", directory="tasks")


def test_readonly_zone_shell_write_is_refused_but_read_allowed(tmp_path, monkeypatch):
    """shell 通道：写意图 → 拒（且**不可授权**）；只读命令照放。"""
    _readonly_env(tmp_path, monkeypatch)

    for command in (
        "echo x > tasks/s1/task.json",
        "echo x >> tasks/s1/task.json",
        "rm tasks/s1/task.json",
        "cp src/a.py tasks/a.py",
        "mv tasks/s1/task.json /tmp/x",
        "sed -i s/别动我/动了/ tasks/s1/task.json",
        "tee tasks/a.txt",
        "git restore tasks/s1/task.json",
        "git checkout -- tasks/",
        'python3 -c "open(\'tasks/s1/task.json\',\'w\').write(\'{}\')"',
    ):
        out = run_command(command)
        assert "禁写区" in out, (command, out)
        # 后台通道同一条判定
        assert "禁写区" in run_background(command), command

    # 只读命令不受影响（含"读会话数据"的三种常见写法）
    assert "禁写区" not in run_command("grep -rn 别动我 tasks/")
    assert "禁写区" not in run_command("cat tasks/s1/task.json")
    assert "禁写区" not in run_command("ls tasks")
    assert "禁写区" not in run_command(
        "python3 -c \"print(open('tasks/s1/task.json').read())\"")
    # 引号文本里提到 tasks/ 不算写意图（commit message 里常有这种字样）
    assert "禁写区" not in run_command('echo "别 rm tasks/x" > note.txt')


def test_readonly_zone_cannot_be_authorized(tmp_path, monkeypatch):
    """禁写区**没有**放行通道：越界授权对它无效（与越界访问刻意不同）。"""
    _readonly_env(tmp_path, monkeypatch)
    monkeypatch.setattr(safety_module, "_request_path_authorization",
                        lambda *args, **kwargs: True)      # 一律"授权"
    assert "禁写区" in run_command("echo x > tasks/s1/task.json")
    out = _call(_agent(_readonly_env(tmp_path, monkeypatch),
                       registry_module.MAIN_AGENT_ID),
                "write_file",
                {"path": "tasks/s1/task.json", "content": "{}", "force": True})
    assert "禁写区" in out and "不可授权" in out


def test_readonly_env_extra_dirs_from_env_var(tmp_path, monkeypatch):
    """`WOVRA_READONLY_DIRS` 可追加自定义只读目录（os.pathsep 分隔）。"""
    task = _readonly_env(tmp_path, monkeypatch)
    extra = tmp_path / "secrets"
    extra.mkdir()
    monkeypatch.setenv("WOVRA_READONLY_DIRS", str(extra))

    out = _call(_agent(task, registry_module.MAIN_AGENT_ID), "write_file",
                {"path": "secrets/x.md", "content": "写不进去"})
    assert "禁写区" in out and str(extra) in out
    # 取消该变量后同一条写恢复（证明是环境变量在起作用，不是别的原因）
    monkeypatch.delenv("WOVRA_READONLY_DIRS")
    ok = _call(_agent(task, registry_module.MAIN_AGENT_ID), "write_file",
               {"path": "secrets/x.md", "content": "这下能写"})
    assert "禁写区" not in ok


def test_public_files_are_writable_and_first_writer_owns(tmp_path, monkeypatch):
    """P6：**公共文件**谁都能先动手；第一次写/改它的那个 agent 获得所有权。

    用户口径（2026-09-18）："如果活性文件没有归类，将其放到公共文件中，所有 agent
    都有其所有操作权，但后面第一次操作写/改的 agent 获得其所有权，被归宿后后面
    agent 就只能读了。"——所以断言分两段：**先写成功**（谁都不撞拒绝），
    **写完之后变成它的私有文件**（另一个 agent 再写就撞"别人的文件只读"）。
    """
    task = _split_task(tmp_path, monkeypatch)
    (tmp_path / "notes.md").write_text("公共笔记\n", encoding="utf-8")
    task.public_files = ["notes.md"]

    # ① 甲（工具层）先写：放行，且归它
    a = _agent(task, "工具层")
    out = _call(a, "write_file", {"path": "notes.md", "content": "甲写的"})
    assert "已覆盖" in out, out
    assert "notes.md" not in (task.public_files or []), "写完要从公共区摘牌"
    assert "notes.md" in [str(f) for f in task.registry[1].get("files") or []]
    assert any("公共文件" in str(e.get("detail")) for e in task.history
               if e.get("kind") == "ownership")

    # ② 乙（前端）再写：现在它是甲的私有文件 → 撞 P2
    b = _agent(task, "前端")
    out2 = _call(b, "write_file", {"path": "notes.md", "content": "乙也要改"})
    assert "权限拒绝" in out2 and "工具层" in out2
    assert (tmp_path / "notes.md").read_text(encoding="utf-8") == "甲写的"

    # ③ 乙**读**公共文件（还没被人拿下时）仍有全权——另起一个公共文件验证
    task.public_files = ["docs/"]
    (tmp_path / "docs").mkdir(exist_ok=True)
    (tmp_path / "docs" / "spec.md").write_text("规格\n", encoding="utf-8")
    assert "权限拒绝" not in _call(b, "read_file", {"path": "docs/spec.md"})
    # **改**也算"第一次动手"（不只写）——edit_file 成功后同样认领
    assert "已修改" in _call(b, "edit_file",
                            {"path": "docs/spec.md", "old_text": "规格",
                             "new_text": "规格 v2"})
    assert any(str(f) == "docs/spec.md"
               for f in task.registry[2].get("files") or []), "改公共文件也认领"


def test_workspace_not_bound_when_session_dir_is_unusable(tmp_path, monkeypatch):
    """会话的工作目录不可用 → **不许悄悄退回启动目录**（2026-09-18 用户报障）。

    用户原话："我工作路径下是空的，其为什么说当前工作路径是 wovra，且不需要我授权
    就去读取修改了。"

    链条（三个事实叠起来）：`workspace_root()` 是"线程绑定优先、进程默认兜底"；
    进程默认 = serve 的**启动目录**；越界检查只拦"解析后落在界外"的路径。于是绑定
    一旦静默跳过，相对路径 `docs/x.md` 解析成 `<启动目录>/docs/x.md`——**恰好在界内**，
    既读得到运行器自己的仓库，又压根不问授权。
    """
    from wovra.cli.prompt import _build_agent
    from wovra.tools import safety as safety_module

    monkeypatch.setattr("wovra.tools.safety.PROJECT_ROOT", tmp_path)
    task = Task.create(goal="工作区不可用")
    task.workspace = str(tmp_path / "并不存在" / "这个目录")

    _build_agent(task)

    assert not safety_module.is_bound(), "目录不可用就不该绑（更不能绑到启动目录）"
    assert str(safety_module.workspace_root()) == str(tmp_path), "应回退到进程默认供只读端点用"
    # 工具层：相对路径落在启动目录下——**这正是要防的**
    resolved = safety_module._safe_path_lexical("docs/x.md")
    assert str(resolved).startswith(str(tmp_path))
    # 会话上留了标记，起轮那道闸门据此拒绝开工
    assert task.__dict__.get("_workspace_bind_failed")


def test_workspace_binds_when_dir_is_usable(tmp_path, monkeypatch):
    """目录可用时正常绑定，且 `is_bound()` 如实为真。"""
    from wovra.cli.prompt import _build_agent
    from wovra.tools import safety as safety_module

    safety_module.unbind_workspace()
    task = Task.create(goal="正常")
    task.workspace = str(tmp_path)

    _build_agent(task)

    assert safety_module.is_bound()
    assert safety_module.workspace_root() == tmp_path.resolve()
    assert not task.__dict__.get("_workspace_bind_failed")
