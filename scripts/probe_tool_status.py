"""工具结果成败判定取证（常驻仪器，零 LLM）：旧口径误判 vs 新权威口径。

背景（2026-09-14 用户报障："工具消息块对成功/失败的判断太草率了，老是判断做，
修一下"）：同一份"成没成"的事实，代码里**四处各自猜文本**，口径互不相同——

  * `tools/safety.FAILURE_MARKERS`（**全文子串**，已于本日删除）→ truncate /
    ui / task 三处共用；正文里出现"工具执行出错"/"命令执行失败（"即判失败，
    于是**成功的 read_file 读到本项目源码必然中招**。
  * `webui/index.html::resultStatus`（**开头锚定 5 个词 + 全文 exit_code**）→
    "工具执行出错:"、"截图失败："、"抓取失败:" 全落到"成功"分支。
  * `lifecycle.op_failed` / `blocks/segment._op_failure`（首行前缀）——各自维护
    一份表，覆盖不全（二进制读取算"读成功"）。

现状：判定统一在 `wovra.tools.status.classify`（首行 + 结构化 exit_code +
三分类 ok/error/deny），四个消费者全部委托。

本脚本扫 tasks/*/task.json 的全部 tool_result 事件，输出：
  ① **旧全文子串口径**（内联冻结复刻，便于对照）与新口径的分歧条数；
  ② **旧前端口径**与新口径的分歧条数；
  ③ 分歧按**首行形态**归类（修复清单的直接依据）；
  ④ 假阳性样本（成功的源码读取被当成失败）。

用法：`uv run --no-sync python scripts/probe_tool_status.py`。只打结论（≤20 行）。
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from wovra.tools import result_status  # noqa: E402

TASKS = Path(__file__).resolve().parents[1] / "tasks"

# 旧口径的**冻结复刻**（原 tools/safety.FAILURE_MARKERS，2026-09-14 删除）：
# 留着是为了让"改前 vs 改后"的对照永远可复算，而不是靠记忆里的数字。
LEGACY_FULLTEXT_MARKERS = (
    "工具执行出错",
    "未知工具",
    "合法 JSON",
    "已拒绝执行危险命令",
    "命令执行失败（",
)


def legacy_python(content: str) -> str:
    """旧 Python 口径（truncate/ui/task 共用，全文子串）。"""
    return "error" if any(m in content for m in LEGACY_FULLTEXT_MARKERS) else "ok"


def legacy_frontend(content: str) -> str:
    """旧前端口径（resultStatus：开头锚定 + 全文 exit_code）。"""
    s = (content or "").strip()
    if s.startswith("已拒绝执行"):
        return "ok"  # 前端另有 deny 分支，这里折算成"非 error"便于比较
    m = re.search(r"exit_code=(-?\d+)", s)
    if m:
        return "ok" if m.group(1) == "0" else "error"
    if re.match(r"^(错误|失败|路径越界|文件不存在|Error|Traceback)", s):
        return "error"
    return "ok"


def main() -> None:
    events: list[tuple[str, str, str]] = []
    sessions = 0
    for path in sorted(TASKS.glob("*/task.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        sessions += 1
        for rnd in data.get("rounds") or []:
            for ev in rnd.get("events") or []:
                if ev.get("type") != "tool_result":
                    continue
                msg = ev.get("message") or {}
                events.append((path.parent.name, ev.get("id", ""),
                               str(msg.get("content") or "")))
    print(f"扫描 {len(events)} 条 tool_result（{sessions} 个会话）")

    rows = []
    for task, eid, content in events:
        head = (content or "").split("\n", 1)[0][:24]
        rows.append((head, legacy_python(content), legacy_frontend(content),
                     result_status(content), content))

    def count(a_idx: int, b_idx: int) -> int:
        return sum(1 for r in rows if (r[a_idx] == "error") != (r[b_idx] == "error"))

    old_vs_new = count(1, 3)
    fe_vs_new = count(2, 3)
    # 假阳性 = 旧口径判"失败"、新口径判"成功"（真误判）。
    # 旧口径判失败而新口径判"拒绝"的**不算误判**——那是口径细化（拒绝与出错分开），
    # 两者都属于"没成"。探针把它们分开数，避免把改进记成问题。
    fp_py = sum(1 for r in rows if r[1] == "error" and r[3] == "ok")
    deny_py = sum(1 for r in rows if r[1] == "error" and r[3] == "deny")
    fn_py = sum(1 for r in rows if r[1] != "error" and r[3] != "ok")
    fp_fe = sum(1 for r in rows if r[2] == "error" and r[3] == "ok")
    deny_fe = sum(1 for r in rows if r[2] == "error" and r[3] == "deny")
    fn_fe = sum(1 for r in rows if r[2] != "error" and r[3] != "ok")
    print(f"① 旧全文子串口径 vs 新权威：{old_vs_new} 条不一致"
          f"（假阳性 {fp_py}；拆成拒绝 {deny_py}；假阴性 {fn_py}）")
    print(f"② 旧前端口径 vs 新权威：{fe_vs_new} 条不一致"
          f"（假阳性 {fp_fe}；拆成拒绝 {deny_fe}；假阴性 {fn_fe}）")

    print("③ 分歧最多的首行形态（旧口径 → 新权威）：")
    forms = Counter()
    for head, old, fe, new, _ in rows:
        if (old == "error") != (new == "error"):
            forms[(head, "旧Python全文")] += 1
        if (fe == "error") != (new == "error"):
            forms[(head, "旧前端开头")] += 1
    for (head, kind), n in forms.most_common(8):
        print(f"   {n:4d}  [{kind}] 首行={head!r}")

    fp = [(t, e, c) for (t, e, c), r in zip(events, rows)
          if r[1] == "error" and r[3] == "ok"]
    print(f"④ 假阳性（成功被判失败）样本 {len(fp)} 条：")
    for t, e, c in fp[:3]:
        print(f"   {t} {e} :: " + " ".join(c.split())[:70])


if __name__ == "__main__":
    main()
