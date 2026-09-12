"""缓存命中审计：**哪些 miss 是浪费的**（只读，零 LLM）。

为什么要算"该 miss 多少"：单看命中率会把好账看成坏账——一次调用读了新的
工具结果（几 KB 新增内容），命中率就掉到 90% 以下，但**那份新增内容本来
就要按未命中计费**，一点没浪费。

口径：

```text
理想 miss（新内容）  = max(0, prompt_i - prompt_{i-1})   # 上一条 prompt 的字节
                                                           应当全部命中
浪费（defect）       = miss_i - 理想 miss                 # > 0 才是前缀被破坏
```

defect 的三种来源（本脚本按时间与体量自动归类）：

* **冷启动**：会话第一条调用（没有可复用的前缀）；
* **空闲过期**：两次调用间隔很久（服务端上下文缓存 TTL 到期），整条前缀作废；
* **前缀重建**：prompt 比上一条明显变小/变形（整理生效、视图切换、回合内
  转交到另一个 agent）——AGENTS.md §2 只允许整理破坏前缀，其余都该是 0。

用法：`uv run --no-sync python scripts/probe_cache_hits.py [task_id]`
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from pathlib import Path

TASKS = Path(__file__).resolve().parents[1] / "tasks"
KV = re.compile(r"(\w+)=([\d,]+(?:\.\d+)?)")
IDLE_SECONDS = 600          # 超过这个间隔算"空闲过期"候选
DEFECT_FLOOR = 2000         # 小于这个数的差额当噪声（分块对齐/字段微调）
SHOW = 3


def _num(detail: str, key: str) -> int:
    m = re.search(rf"{key}=([\d,]+)", detail or "")
    return int(m.group(1).replace(",", "")) if m else 0


def _ts(text: str):
    try:
        return datetime.fromisoformat(str(text))
    except (TypeError, ValueError):
        return None


def calls(data: dict) -> list[dict]:
    out: list[dict] = []
    for h in data.get("history") or []:
        if h.get("kind") != "llm_call":
            continue
        detail = str(h.get("detail") or "")
        prompt = _num(detail, "prompt")
        cached = _num(detail, "cached")
        if not prompt:
            continue
        out.append({
            "time": str(h.get("time") or ""),
            "purpose": detail.split("]")[0].strip("[").strip(),
            "prompt": prompt,
            "cached": cached,
            "miss": max(0, prompt - cached),
        })
    return out


def classify(rows: list[dict]) -> None:
    prev = None
    prev_t = None
    for row in rows:
        t = _ts(row["time"])
        ideal = max(0, row["prompt"] - prev["prompt"]) if prev else row["prompt"]
        row["ideal"] = ideal
        row["defect"] = max(0, row["miss"] - ideal)
        row["idle"] = round((t - prev_t).total_seconds()) if (t and prev_t) else 0
        if prev is None:
            row["kind"] = "冷启动"
        elif row["defect"] <= DEFECT_FLOOR:
            row["kind"] = "纯新增内容"
        elif row["idle"] >= IDLE_SECONDS:
            row["kind"] = "空闲过期"
        elif row["prompt"] < prev["prompt"] * 0.6:
            row["kind"] = "前缀重建"
        else:
            row["kind"] = "其他"
        prev, prev_t = row, t


def audit(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = calls(data)
    classify(rows)
    total_prompt = sum(r["prompt"] for r in rows)
    total_cached = sum(r["cached"] for r in rows)
    defects = [r for r in rows if r["defect"] > DEFECT_FLOOR]
    kinds: dict[str, list[int]] = {}
    for r in defects:
        k = kinds.setdefault(r["kind"], [0, 0])
        k[0] += 1
        k[1] += r["defect"]
    purposes: dict[str, list[int]] = {}
    for r in rows:
        p = purposes.setdefault(r["purpose"] or "?", [0, 0, 0])
        p[0] += 1
        p[1] += r["prompt"]
        p[2] += r["cached"]
    return {
        "id": data.get("id") or path.parent.name,
        "calls": len(rows),
        "prompt": total_prompt,
        "cached": total_cached,
        "defects": sorted(defects, key=lambda r: -r["defect"]),
        "kinds": kinds,
        "purposes": purposes,
    }


def main() -> None:
    if len(sys.argv) > 1:
        paths = [TASKS / sys.argv[1] / "task.json"]
    else:
        paths = sorted(TASKS.glob("*/task.json"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
    audits = [audit(p) for p in paths if p.exists()]
    audits = [a for a in audits if a["calls"]]
    if not audits:
        print("没有带调用账的会话。")
        return
    for a in audits[:SHOW]:
        rate = a["cached"] / a["prompt"] * 100 if a["prompt"] else 0
        waste = sum(v[1] for v in a["kinds"].values())
        print(f"\n[{a['id']}] {a['calls']} 次调用　Σprompt {a['prompt']:,}　"
              f"命中 {rate:.1f}%　浪费（前缀被破坏）{waste:,} tok"
              f"（{waste / a['prompt'] * 100 if a['prompt'] else 0:.2f}% of Σprompt）")
        for purpose, (n, p, c) in sorted(a["purposes"].items(), key=lambda x: -x[1][1]):
            print(f"    {purpose:<13} {n:>3} 次　Σprompt {p:>10,}　"
                  f"命中 {c / p * 100 if p else 0:5.1f}%")
        if a["kinds"]:
            print("    浪费归类：" + "、".join(
                f"{k} {n} 次 / {v:,} tok" for k, (n, v) in
                sorted(a["kinds"].items(), key=lambda x: -x[1][1])))
        for r in a["defects"][:3]:
            idle = f"　距上次 {r['idle'] // 60} 分钟" if r["idle"] >= 60 else ""
            print(f"    ⚠ {r['time']} {r['kind']:<6} prompt {r['prompt']:,}　"
                  f"miss {r['miss']:,}（该 miss {r['ideal']:,}）　"
                  f"浪费 {r['defect']:,}{idle}")
    if len(audits) > SHOW:
        print(f"\n（仅展示最近 {SHOW} 个会话，共 {len(audits)} 个）")
    print("\n口径：理想 miss = 本轮新增内容（上一条 prompt 应当全命中）；"
          "浪费 = miss − 理想 miss。")


if __name__ == "__main__":
    main()
