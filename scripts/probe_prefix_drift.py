"""前缀分叉定位仪：同一轮相邻两步的装配，前缀在第几个字节分叉、分叉点长什么样。

为什么要它：上下文缓存是**离散前缀命中**——中途有一个字节变了，从那一点往后
全部作废。命中率只是症状，**分叉点**才是病因。本仪器把同一轮的第 k 步与第 k+1
步各装配一次（同一份 rounds/registry/state，纯函数派生），算最长公共前缀，打印
分叉点前后的原文，直接看出"谁在中间改写了历史字节"。

判读：
* `前缀保留率` = 公共前缀 / 第 k 步的体量。理想是 **100%**（新的一步只应在尾部
  追加），低于 100% 的那些字节就是白花的钱；
* 分叉点落在哪一段（`<<role>>` 标记）就说明那段被重写了。落在"当前轮的事件"
  里正常（当前轮本来就在长）；落在**更早的历史**里就是违反前缀纪律。

用法：
  uv run --no-sync python scripts/probe_prefix_drift.py <task_id> <round_seq> [步…]
  uv run --no-sync python scripts/probe_prefix_drift.py 20260911-190158-c34bc5 12 3 4
（只读：先把会话复制到临时 TASKS_ROOT 再加载，不碰原数据。）
"""

from __future__ import annotations

import re
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

TASKS = Path(__file__).resolve().parents[1] / "tasks"
WINDOW = 110


class _Stub:
    """替身模型：装配不需要它，只要有个能构造的 llm。"""

    model = "stub"

    def chat(self, messages, tools=None, stream=False, **kwargs):  # pragma: no cover
        raise RuntimeError("本仪器不调用模型")


def _blob(messages: list[dict]) -> str:
    parts: list[str] = []
    for m in messages:
        parts.append(f"<<{m.get('role')}>>")
        content = m.get("content")
        if content:
            parts.append(str(content))
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function") or {}
            parts.append(f"[call {fn.get('name')}] {fn.get('arguments')}")
    return "\n".join(parts)


def _lcp(a: str, b: str) -> int:
    lo, hi = 0, min(len(a), len(b))
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if a[:mid] == b[:mid]:
            lo = mid
        else:
            hi = mid - 1
    return lo


def assemble(task_id: str, seq: int, step: int) -> str:
    from wovra import task as task_module
    from wovra.agent import Agent

    src = TASKS / task_id
    tmp = Path(tempfile.mkdtemp(prefix="wovra-prefix-"))
    shutil.copytree(src, tmp / task_id)
    task_module.TASKS_ROOT = tmp
    task = task_module.Task.load(task_id)
    target = next((r for r in task.rounds if int(r.get("seq") or 0) == seq), None)
    if target is None:
        raise SystemExit(f"没有 R{seq}")
    r = dict(target)
    events = list(r.get("events") or [])[:step]
    r["events"] = events
    agent = Agent(llm=_Stub(), tools=[], task=task)
    agent.turn_count = seq
    agent.current_round = r
    agent.messages = [e.get("message") or {} for e in events]
    return _blob(agent._assemble_messages())


def _where(blob: str, pos: int) -> str:
    head = blob[:pos]
    marks = [m for m in re.finditer(r"<<(\w+)>>", head)]
    idx = len(marks)
    role = marks[-1].group(1) if marks else "?"
    return f"第 {idx} 段（<<{role}>> 内）"


def main() -> None:
    if len(sys.argv) < 3:
        print(__doc__)
        return
    task_id, seq = sys.argv[1], int(sys.argv[2])
    steps = [int(x) for x in sys.argv[3:]] or [2, 3, 4]
    print(f"任务 {task_id}　轮 R{seq}　对比步骤 {steps}")
    for step in steps:
        try:
            a = assemble(task_id, seq, step)
            b = assemble(task_id, seq, step + 1)
        except Exception as exc:  # noqa: BLE001——仪器：任何构造失败都直接报
            print(f"  步 {step}→{step + 1}：装配失败（{exc!r}）")
            continue
        common = _lcp(a, b)
        keep = common / len(a) * 100 if a else 0
        print(f"\n  步 {step}→{step + 1}　体量 {len(a):,} → {len(b):,} 字符")
        print(f"    公共前缀 {common:,} 字符　前缀保留率 {keep:.1f}%"
              f"　分叉后新发 {len(b) - common:,} 字符"
              f"{'（应为 0：整段历史都被改写）' if common == 0 else ''}")
        print(f"    分叉点：{_where(a, common)}")
        print(f"    分叉前：…{a[max(0, common - WINDOW):common]}".replace("\n", "⏎"))
        print(f"    分叉后(a)：{a[common:common + WINDOW]}…".replace("\n", "⏎"))
        print(f"    分叉后(b)：{b[common:common + WINDOW]}…".replace("\n", "⏎"))


if __name__ == "__main__":
    main()
