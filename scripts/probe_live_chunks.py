"""取正在跑的 serve 的**真实直播分片流**（常驻诊断仪器）。

为什么需要它：直播区的很多 bug（尾部留旧正文、跨步累积、工具卡不出现）都取决于
**服务端到底推了什么、什么顺序**——光看前端代码推不出来。这个脚本直接问在跑的
serve 要分片，把"看得到的事实"打出来（≤20 行）。

用法：
    uv run --no-sync python scripts/probe_live_chunks.py             # 自动找在跑的会话
    uv run --no-sync python scripts/probe_live_chunks.py 8600 SID    # 指定端口/会话
"""
from __future__ import annotations

import json
import pathlib
import sys
import urllib.request

PORTS = (8600, 8602, 8601)


def get(port: int, path: str):
    with urllib.request.urlopen(  # noqa: S310 —— 只连本机
        f"http://127.0.0.1:{port}{path}", timeout=5
    ) as r:
        return json.loads(r.read().decode("utf-8"))


def find(port: int) -> list[tuple[str, str]]:
    """[(session_id, job_id)]：正在跑的会话。"""
    try:
        sessions = get(port, "/api/sessions")
    except Exception as exc:  # noqa: BLE001
        print(f"  端口 {port} 不可用：{type(exc).__name__}")
        return []
    out = []
    for s in sessions if isinstance(sessions, list) else sessions.get("sessions", []):
        sid = s.get("id") or s.get("session") or ""
        if not sid:
            continue
        try:
            meta = get(port, f"/api/sessions/{sid}")
        except Exception:  # noqa: BLE001
            continue
        job = meta.get("live_job")
        if job:
            out.append((sid, job))
    return out


def report(port: int, sid: str, job: str) -> None:
    d = get(port, f"/api/jobs/{job}/live?after=0")
    chunks = d.get("chunks") or []
    kinds: dict[str, int] = {}
    for c in chunks:
        kinds[c.get("k", "?")] = kinds.get(c.get("k", "?"), 0) + 1
    print(f"端口 {port}  会话 {sid}  作业 {job}  状态 {d.get('status')} 轮 {d.get('round')}")
    print(f"  分片 {len(chunks)} 个：{kinds}")
    # 逐步回放：哪些分片是"文字"，哪些是"事件"，事件之间累积了多少文字
    step: list[str] = []
    tails: list[str] = []
    for c in chunks:
        k = c.get("k")
        if k == "ans":
            step.append(str(c.get("s") or ""))
        elif k == "think":
            step.append(f"<think:{len(str(c.get('s') or ''))}>")
        elif k == "event":
            e = c.get("e") or {}
            tails.append(f"{e.get('type')}({len(str(e.get('content') or ''))}字)")
            step = []                       # 事件到达 → 前端会在这一步清空在飞缓冲
        elif k == "status":
            step = []
    print(f"  事件序列（前 12）：{tails[:12]}")
    print(f"  最后 6 个分片：{[ (c.get('k'), len(str(c.get('s') or c.get('e') or ''))) for c in chunks[-6:] ]}")
    if step:
        print(f"  ⚠ 末尾还有 {len(step)} 个未收口的文字分片（合计 {sum(len(x) for x in step)} 字）")


def main() -> int:
    import time
    wait = "--wait" in sys.argv
    dump = "--dump" in sys.argv
    deadline = time.time() + (120 if wait else 0)
    picked = 0
    while True:
        for port in PORTS:
            for sid, job in find(port):
                if dump:
                    d = get(port, f"/api/jobs/{job}/live?after=0")
                    out = pathlib.Path(f"output/live-chunks-{port}-{job}.json")
                    out.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
                    print(f"  已抓 {len(d.get('chunks') or [])} 个分片 → {out}")
                report(port, sid, job)
                picked += 1
        if picked or time.time() > deadline:
            break
        print("  等一个正在跑的轮…")
        time.sleep(5)
    if not picked:
        print("没找到正在跑的轮（所有 serve 都没有 live_job）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
