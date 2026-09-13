"""量在跑的 serve 各接口的**响应时间**（"一直加载中"最快的判据）。

用户报"会话记录一直在加载中，其它窗口页面也一直加载中"——所有面板一起卡，
第一嫌疑不是前端，而是**服务端不回答**（请求排队/单请求极慢）。
用法：`uv run --no-sync python scripts/probe_serve_latency.py`
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request

PORTS = (8600, 8602)


def timed(url: str, timeout: float = 20.0) -> tuple[float | None, int, str]:
    t0 = time.time()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:  # noqa: S310
            body = r.read()
        return time.time() - t0, r.status, f"{len(body)} 字节"
    except Exception as exc:  # noqa: BLE001
        return None, 0, f"{type(exc).__name__}: {exc}"


def main() -> int:
    for port in PORTS:
        base = f"http://127.0.0.1:{port}"
        dt, code, note = timed(base + "/api/sessions")
        print(f"端口 {port}  /api/sessions  "
              + (f"{dt*1000:.0f} ms  HTTP {code}  {note}" if dt else f"不通（{note}）"))
        if not dt:
            continue
        try:
            with urllib.request.urlopen(base + "/api/sessions", timeout=20) as r:  # noqa: S310
                rows = json.loads(r.read().decode("utf-8"))
        except Exception:  # noqa: BLE001
            continue
        rows = rows if isinstance(rows, list) else rows.get("sessions", [])
        if not rows:
            continue
        sid = str(rows[0].get("id") or rows[0].get("session"))
        for path in ("", "/plan"):
            dt2, code2, note2 = timed(f"{base}/api/sessions/{sid}{path}")
            print(f"          {path or '/meta'}       "
                  + (f"{dt2*1000:.0f} ms  HTTP {code2}  {note2}" if dt2 else f"不通（{note2}）"))
        dt3, code3, note3 = timed(f"{base}/api/sessions/{sid}/rounds/1")
        print(f"          /rounds/1     "
              + (f"{dt3*1000:.0f} ms  HTTP {code3}  {note3}" if dt3 else f"不通（{note3}）"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
