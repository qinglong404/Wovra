"""取证：复刻 _split_fixture 场景，看 _split_view_watermarks 为何返回空。"""
import json
import sys
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

import tempfile  # noqa: E402
from wovra import task as task_module  # noqa: E402
from wovra import views as views_module  # noqa: E402
from wovra.agent import Agent  # noqa: E402
from wovra.task import Task  # noqa: E402

tmp = Path(tempfile.mkdtemp(prefix="dbg-wm-"))
task_module.TASKS_ROOT = tmp
task = Task.create(goal="目标")
task.rounds = [{
    "seq": 1,
    "user_input": {"original": "第一轮", "normalized": "澄清：第一轮"},
    "events": [
        {"id": "R1-E01", "type": "user", "status": "", "truncated": "第一轮",
         "message": {"role": "user", "content": "第一轮"}},
        {"id": "R1-E02", "type": "final_answer", "status": "", "truncated": "答案",
         "message": {"role": "assistant", "content": "答案"}},
    ],
    "refined_index": {"R1-E01": "第一轮（精修）", "R1-E02": "答案（精修）"},
    "end_state": "completed",
    "org_state": "",
}]
agent = Agent(llm=type("L", (), {"model": "stub"})(), tools=[], task=task,
              org_watermark=0, org_grace_rounds=0, org_cooldown_rounds=0)
task.rounds[0]["domains"] = [
    {"name": "工具层", "description": "边界守卫",
     "file_domains": ["src/wovra/tools/"]},
]
print("self.rounds is task.rounds:", agent.rounds is task.rounds)
print("latest_domains:", views_module.latest_domains(agent.rounds))
try:
    marks = views_module.view_watermarks(
        agent.rounds, task.get_state(),
        domains=views_module.latest_domains(agent.rounds),
        registry=task.registry, watermark=agent._org_watermark,
    )
    print("marks ok:", marks)
except Exception:
    traceback.print_exc()
print("硬数据行:", agent._split_view_watermarks())
