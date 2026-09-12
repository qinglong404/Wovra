"""一次性取证：为什么 _split_view_watermarks 在测试夹具下返回空（用完保留，不删）。"""
import json
import sys
import traceback
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from wovra import views as v  # noqa: E402
from wovra.agent import Agent  # noqa: E402
from wovra.task import Task  # noqa: E402

task = Task.create(goal="g")
task.rounds = [{
    "seq": 1,
    "user_input": {"original": "第一轮", "normalized": ""},
    "events": [
        {"id": "R1-E01", "type": "user",
         "message": {"role": "user", "content": "第一轮"}},
        {"id": "R1-E02", "type": "final_answer",
         "message": {"role": "assistant", "content": "答案"}},
    ],
    "end_state": "completed", "org_state": "",
    "domains": [{"name": "工具层", "description": "d",
                 "file_domains": ["src/wovra/tools/"]}],
}]
agent = Agent(llm=SimpleNamespace(model="stub"), tools=[], task=task)
print("latest_domains:", v.latest_domains(agent.rounds))
try:
    marks = v.view_watermarks(
        agent.rounds, task.get_state(),
        domains=v.latest_domains(agent.rounds), registry=task.registry,
        watermark=1000,
    )
    print("marks:", marks)
except Exception:
    traceback.print_exc()
print("agent 内的硬数据行:", agent._split_view_watermarks())
