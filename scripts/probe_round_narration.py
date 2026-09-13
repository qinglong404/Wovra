"""看某个会话最近一轮的**事件内容**：过程发言是否跨步重复（0-LLM 取证，只读）。

用来判定"尾部正文越堆越多"到底是**前端没清**还是**模型真的重复说了同一段**。
用法：`uv run --no-sync python scripts/probe_round_narration.py [端口] [会话前缀]`
"""
from __future__ import annotations

import json
import sys
import urllib.request

PORTS = (8600, 8602)


def get(port: int, path: str):
    with urllib.request.urlopen(  # noqa: S310 —— 只连本机
        f"http://127.0.0.1:{port}{path}", timeout=10
    ) as r:
        return json.loads(r.read().decode("utf-8"))


def main() -> int:
    want_prefix = sys.argv[2] if len(sys.argv) > 2 else ""
    ports = [int(sys.argv[1])] if len(sys.argv) > 1 else list(PORTS)
    for port in ports:
        try:
            sessions = get(port, "/api/sessions")
        except Exception:  # noqa: BLE001
            continue
        rows = sessions if isinstance(sessions, list) else sessions.get("sessions", [])
        rows = sorted(rows, key=lambda s: str(s.get("updated") or s.get("mtime") or ""),
                      reverse=True)
        for s in rows[:6]:
            sid = str(s.get("id") or "")
            if want_prefix and not sid.startswith(want_prefix):
                continue
            try:
                meta = get(port, f"/api/sessions/{sid}")
            except Exception:  # noqa: BLE001
                continue
            seqs = sorted((r.get("seq") or 0) for r in (meta.get("round_list") or []))
            if not seqs:
                continue
            seq = seqs[-1]
            d = get(port, f"/api/sessions/{sid}/rounds/{seq}")
            evs = d.get("events") or []
            print(f"\n=== 端口 {port}  会话 {sid}  R{seq}  事件 {len(evs)} 个 ===")
            texts = []
            for e in evs:
                if e.get("type") == "tool_call":
                    c = str(e.get("content") or "")
                    th = str(e.get("thinking") or "")
                    if c:
                        texts.append(c.strip()[:70])
                    print(f"  {e.get('time','')[11:19]} tool_call  正文{len(c)}字  思考{len(th)}字"
                          f"  工具={[ (t.get('function') or {}).get('name') for t in (e.get('tool_calls') or []) ]}")
            dup = [t for t in set(texts) if texts.count(t) > 1]
            print(f"  过程发言 {len(texts)} 段；**重复**的 {len(dup)} 段"
                  + (f"：{dup[:2]}" if dup else ""))
            for t in texts[-6:]:
                print(f"    · {t}")
            return 0
    print("没找到会话")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
