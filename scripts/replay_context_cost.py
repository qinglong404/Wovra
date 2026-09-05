"""回放对照：同一轨迹下 managed / baseline 的历史携带成本。

把一条已落盘的会话轨迹（task.json 的 rounds）在每个轮闭合点，用两种
模式分别装配"下一次请求会看到的上下文"（不含新轮自己的事件——轮内
工作集两种模式等价，比较时会互相抵消），逐点配对估算 token。

模型不参与、非确定性归零：这是"机制自身开销"的最干净口径。
对含 full 字段的旧数据（旧版安全截断的产物），baseline 侧按其运行时
行为还原完整内容后再装配，保证两种模式看到同一份事实。

用法：uv run python scripts/replay_context_cost.py <task_id> [task_id ...]
"""

import sys
from types import SimpleNamespace

from wovra.agent import Agent
from wovra.task import Task


def _restore_full(rounds: list[dict]) -> list[dict]:
    """回放前的轨迹清洗：

    * 旧数据的安全截断把大结果裁剪进 message、原文放 full 字段；
      baseline 的运行时行为是还原完整内容——回放时保持同一口径。
    * 剥离 compacted 标记——那是会话结束态的产物（旧水位口径在
      末轮触发过一次压缩），回放要的是"每个边界当时的真实体量"，
      且按修订后的水位口径本轨迹根本不会触发压缩。
    """
    out = []
    for r in rounds:
        events = []
        for e in r.get("events", []):
            if e.get("full"):
                e = {**e, "message": {**e["message"], "content": e["full"]}}
            events.append(e)
        out.append({**r, "events": events, "compacted": False})
    return out


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        return

    for task_id in sys.argv[1:]:
        task = Task.load(task_id)
        # 回放不需要补跑未完成的整理（pending 会触发惰性补跑调用 LLM），
        # 清掉标记即可——仅内存对象，task.save() 不会被调用，磁盘不受影响
        for r in task.rounds:
            if r.get("org_state") == "pending":
                r["org_state"] = ""
        rounds = task.rounds
        print(f"\n=== 轨迹 {task_id}（{len(rounds)} 轮）===")
        print(f"{'边界':<10} {'managed':>10} {'baseline':>10} {'B/A':>6}   本轮工作集(两边等价)")

        managed = Agent(llm=SimpleNamespace(), tools=[], task=task, context_mode="managed")
        baseline = Agent(llm=SimpleNamespace(), tools=[], task=task, context_mode="baseline")
        baseline_rounds = _restore_full(rounds)

        total_m = total_b = 0
        for k in range(len(rounds) + 1):
            for agent, rs in ((managed, rounds[:k]), (baseline, baseline_rounds[:k])):
                agent.rounds = rs
                agent.current_round = None
                agent.messages = []
            m = managed._estimate_messages(managed._assemble_messages())
            b = baseline._estimate_messages(baseline._assemble_messages())
            # 边界 k 之后新开的是第 k+1 轮；其轮内工作集两边等价
            work = (managed._estimate_messages(
                [e["message"] for e in rounds[k]["events"]]) if k < len(rounds) else 0)
            ratio = f"{b / m:.2f}" if m else "-"
            label = "会话开始" if k == 0 else f"R{k} 闭合后"
            print(f"{label:<10} {m:>10,} {b:>10,} {ratio:>6}   {work:,}")
            total_m += m
            total_b += b

        print(f"{'边界合计':<10} {total_m:>10,} {total_b:>10,} {total_b / total_m:>6.2f}"
              "   （每轮入口各计一次；轮内每步都会重复携带同一份历史）")
        print("注：managed 含降档索引/文件地图/任务状态；baseline 为无压缩全量回放"
              "（compaction 触发点另见水位口径，本表按原始回放计）。")


if __name__ == "__main__":
    main()
