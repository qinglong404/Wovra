"""A/D/E 组缓存法医（2026-09-09）：逐调用生成时长重建 + TTL 拟合。

用法：uv run python scripts/d_e_forensic.py
数据源：tasks/20260908-201257-dccb41（D）/ 20260908-231729-6d526e（E）。
结论备忘：D 的 miss 模式与 TTL≈60s 干净拟合（误差 6%）；E 的偏离来自
22 次 >60s 生成（批量+深思考的 pacing 副作用）——TTL=60 模型解释
1.09M/1.89M，残余 ~800K 集中在 >180s 生成之后，疑似长流触发驱逐，
机制不透明（cache_ttl_probe.py 待跑）。
"""
import json
import re
from datetime import datetime


def calls_of(path):
    d = json.load(open(path))
    events = [e for r in d["rounds"] for e in r["events"]]
    events.sort(key=lambda e: e["timestamp"])

    def is_response(e):
        return e["type"] in ("tool_call", "final_answer", "assistant")

    def chars(e):
        m = e["message"]
        c = len(str(m.get("content") or ""))
        for tc in m.get("tool_calls") or []:
            c += len(str((tc.get("function") or {}).get("arguments") or ""))
        return c

    out, prev_t, prev_cum, cum = [], None, 0, 0
    for e in events:
        cum += chars(e)
        if is_response(e):
            ts = datetime.fromisoformat(e["timestamp"])
            gen = (ts - prev_t).total_seconds() if prev_t else None
            out.append({"gen": gen, "prefix": cum, "append": cum - prev_cum})
            prev_t, prev_cum = ts, cum
    return out


def usage_miss(path):
    d = json.load(open(path))
    total = 0
    for h in d["history"]:
        if h["kind"] == "usage":
            m = re.search(r"未命中 ([\d,]+) tok", h["detail"])
            if m:
                total += int(m.group(1).replace(",", ""))
    return total


def report(label, path, actual_miss):
    calls = calls_of(path)
    ptok = [c["prefix"] / 2.9 for c in calls]
    atok = [c["append"] / 2.9 for c in calls]
    fits = []
    for ttl in range(15, 601, 15):
        miss, prev = 0, 0
        for i, c in enumerate(calls):
            miss += ptok[i] if (prev and prev > ttl) else atok[i]
            prev = c["gen"] or 0
        fits.append((abs(miss - actual_miss) / actual_miss, ttl, miss))
    err, ttl, miss = min(fits)
    m60, prev = 0, 0
    for i, c in enumerate(calls):
        m60 += ptok[i] if (prev and prev > 60) else atok[i]
        prev = c["gen"] or 0
    durs = sorted(c["gen"] for c in calls if c["gen"])
    print(f"[{label}] 调用 {len(calls)} · 生成中位 {durs[len(durs)//2]:.0f}s "
          f"p90 {durs[int(len(durs)*0.9)]:.0f}s max {durs[-1]:.0f}s")
    print(f"  实际 miss {actual_miss:,} · 追加下限 {sum(atok[1:]):,.0f} · "
          f"TTL=60 模型 {m60:,.0f} · 最优拟合 TTL={ttl}s（误差 {err:.0%}）")
    print(f"  >60s 生成 {sum(1 for c in calls if c['gen'] and c['gen'] > 60)} 次 · "
          f">180s {sum(1 for c in calls if c['gen'] and c['gen'] > 180)} 次")


if __name__ == "__main__":
    report("A", "tasks/20260906-161916-f5594a/task.json", usage_miss("tasks/20260906-161916-f5594a/task.json"))
    report("D", "tasks/20260908-201257-dccb41/task.json", usage_miss("tasks/20260908-201257-dccb41/task.json"))
    report("E", "tasks/20260908-231729-6d526e/task.json", 1_888_304)
