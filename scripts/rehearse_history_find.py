"""实验：把"展开细节"从"整轮倒出来"改成"在细节里检索 + 取窗口"（零 LLM，只读）。

为什么要有它（2026-09-17 用户口径）：
* "信息是不丢失了，因此需要提供工具，将细节展开"；
* "**不能是一轮直接展开**，应该提供工具，在细节中高效的找到自己需要的细节"；
* "展开细节作为工具运行结果追加在后面。不修改前面的上下文"。

现状 `Agent.expand_history(ids, level="full")` 对轮 ID 会**逐事件倒出全文**且**无体积上限**
——一段 10 万 token 的重轮能一次灌满上下文。本脚本把提议的替代形态跑出来给人看：

    find(pattern, 范围?, limit=5, 窗口=120)  → 命中清单（位置 + 一行上下文 + 总数）
    show(事件ID, around=关键字|offset=N, chars=M) → 该处的窗口片段，可续取

用法：

    uv run --no-sync python scripts/rehearse_history_find.py <会话ID> [正则]
    uv run --no-sync python scripts/rehearse_history_find.py <会话ID> --sizes   # 只看原文体量分布

脚本不删（AGENTS.md §0.3）。
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

TASKS = Path(__file__).resolve().parents[1] / "tasks"


def event_text(e: dict) -> str:
    """事件的可检索原文（与装配里看到的一致）。"""
    m = e.get("message") or {}
    parts: list[str] = []
    if m.get("tool_calls"):
        parts.append(json.dumps(m["tool_calls"], ensure_ascii=False))
    c = m.get("content")
    if isinstance(c, str):
        parts.append(c)
    elif c:
        parts.append(json.dumps(c, ensure_ascii=False))
    if e.get("full"):
        parts.append(str(e["full"]))
    return "\n".join(parts)


def find(rounds: list[dict], pattern: str, limit: int = 5, window: int = 120,
         scope: str = "") -> str:
    """在**折叠前的原文**里检索：返回命中清单 + 总数 + 续取方式（有界）。"""
    try:
        rx = re.compile(pattern)
    except re.error as error:
        return f"[历史检索] 正则不合法：{error}"
    lo, hi = 1, 10 ** 9
    m = re.match(r"^R?(\d+)(?:-R?(\d+))?$", scope.strip())
    if m:
        lo = int(m.group(1)); hi = int(m.group(2) or lo)
    hits: list[tuple[int, str, str, int]] = []      # (seq, eid, type, index)
    for r in rounds:
        if not (lo <= int(r.get("seq") or 0) <= hi):
            continue
        for e in r.get("events") or []:
            text = event_text(e)
            for hit in rx.finditer(text):
                hits.append((int(r["seq"]), str(e.get("id")), str(e.get("type")), hit.start()))
                break                                # 同一事件只记一次（首处）
    head = (f"[历史检索] pattern=/{pattern}/  范围={'R%d–R%d' % (lo, hi) if scope else '全部轮'}"
            f"  命中 {len(hits)} 处（{len({h[1] for h in hits})} 个事件）")
    if not hits:
        return head + "\n（无匹配。**这不等于'历史里没有这回事'**——换个词或放宽正则再试一次。）"
    lines = [head]
    for i, (seq, eid, etype, idx) in enumerate(hits[:limit], 1):
        e = next(e for r in rounds if int(r["seq"]) == seq for e in r["events"] if e["id"] == eid)
        text = event_text(e).replace("\n", " ")
        a = max(0, idx - window // 2)
        snippet = text[a:idx + window // 2].strip()
        lines.append(f"{i}. R{seq}/{eid} ({etype}) @{idx}  …{snippet}…")
    rest = len(hits) - limit
    if rest > 0:
        lines.append(f"（还有 {rest} 处未显示：find(pattern=…, offset={limit}) 续取，"
                     f"或 show(\"{hits[limit][1]}\", around=…) 直接看某一处上下文）")
    return "\n".join(lines)


def show(rounds: list[dict], eid: str, around: str = "", offset: int = 0,
         chars: int = 800) -> str:
    """取某个事件的**窗口**（不是整段）：可给关键字定位，也可给 offset 续读。"""
    for r in rounds:
        for e in r.get("events") or []:
            if str(e.get("id")) != eid:
                continue
            text = event_text(e)
            at = text.find(around) if around else offset
            if at < 0:
                return f"[展开] {eid} 里没有找到 `{around}`（可用 find 先定位）"
            head = f"[展开] {eid}（{e.get('type')}，全文 {len(text):,} 字符）"
            if around:
                head += f"  关键字 `{around}` @{at}"
            body = text[max(0, at):at + chars]
            tail = f"\n（本窗口 {len(body)} 字符；续读：show(\"{eid}\", offset={max(0, at) + chars})）"
            return head + "\n" + body + tail
    return f"[展开] 未找到事件 {eid}"


def main() -> int:
    ap = argparse.ArgumentParser(description="历史检索/取窗口的实验")
    ap.add_argument("session_id")
    ap.add_argument("pattern", nargs="?", default="")
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--scope", default="")
    ap.add_argument("--sizes", action="store_true", help="只看原文体量分布")
    args = ap.parse_args()

    data = json.loads((TASKS / args.session_id / "task.json").read_text(encoding="utf-8"))
    rounds = data.get("rounds") or []

    if args.sizes:
        sizes = sorted(
            (len(event_text(e)), int(r["seq"]), str(e.get("id")))
            for r in rounds for e in r.get("events") or []
        )
        n = len(sizes)
        print(f"事件 {n} 个；原文体量 p50 {sizes[n // 2][0]:,}　"
              f"p90 {sizes[int(n * 0.9)][0]:,}　max {sizes[-1][0]:,} 字符")
        print("最大的 5 个事件：")
        for size, seq, eid in sizes[-5:][::-1]:
            print(f"  R{seq}/{eid}  {size:,} 字符")
        return 0

    patterns = [args.pattern] if args.pattern else [
        "web_search", "路径越界|越界", "评分器|句号", "ModelScope",
    ]
    for p in patterns:
        print(find(rounds, p, limit=args.limit, scope=args.scope))
        print()
    sample = next((h for r in rounds for h in [f"R{r['seq']}-E01"]), "")
    print(show(rounds, sample, chars=300))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
