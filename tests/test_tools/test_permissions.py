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

from wovra import registry as registry_module
from wovra import task as task_module
from wovra.agent import Agent
from wovra.task import Task
from wovra.tools import delete_file, edit_file, read_file, write_file

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
