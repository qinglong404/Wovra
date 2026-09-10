"""分裂产物重组验证：按 domains 把整理视图的块拼装成子 agent 上下文。

用法：.venv/bin/python -u scripts/reassemble_test.py
输出：docs/split-reassembly.md + 终端摘要（各域规模/覆盖/重叠检查）。
"""

import json

from wovra import task as task_module


def parse_block_rounds(bid: str) -> list[int]:
    """块 ID → 轮号列表（支持合并组合成 ID 如 R9-10-B1）。"""
    head = bid.rsplit("-B", 1)[0].lstrip("R")
    parts = head.split("-")
    if len(parts) == 1:
        return [int(parts[0])]
    return list(range(int(parts[0]), int(parts[1]) + 1))


def main() -> None:
    d = task_module.Task.load("20260909-181052-orgtest")
    rounds = [r for r in d.rounds if r["seq"] <= 28]
    rounds_by_seq = {r["seq"]: r for r in rounds}
    po0 = rounds[0].get("pending_org") or {}
    domains = po0.get("domains") or []
    unassigned_map = po0.get("unassigned") or {}
    indep_ids = list(unassigned_map.get("block_ids") or [])
    sa = po0.get("split_assessment") or {}

    # 全部块（含摘要）索引：bid -> (轮号, summary)
    all_blocks: dict[str, tuple[int, str]] = {}
    for r in rounds:
        po = r.get("pending_org") or {}
        for bid, s in (po.get("block_summaries") or {}).items():
            all_blocks[bid] = (parse_block_rounds(bid)[0], s)

    def round_head(seq: int) -> str:
        r = rounds_by_seq.get(seq)
        if r is None:
            return ""
        po = r.get("pending_org") or {}
        ui = r.get("user_input") or {}
        lines = [f'[R{seq}] 👤 "{ui.get("original", "")}"']
        if po.get("normalized"):
            lines.append(f"       🎯 {po['normalized']}")
        if po.get("key_constraints"):
            lines.append(f"       📌 {po['key_constraints']}")
        return "\n".join(lines)

    out = ["# 分裂产物重组验证（v4.1-flash · 3 域版）\n"]
    assigned: dict[str, str] = {}  # bid -> domain name
    total_chars = 0

    for dom in domains:
        bids = dom.get("block_ids") or []
        name = dom["name"]
        out.append(f"## 子上下文：{name}\n")
        out.append("### 域卡（路由/交流用）")
        out.append(f"- 描述：{dom.get('description', '')}")
        out.append(f"- 目标：{dom.get('goal', '')}")
        if dom.get("constraints"):
            out.append(f"- 约束：{'；'.join(dom['constraints'])}")
        if dom.get("file_domains"):
            out.append(f"- 文件域：{'、'.join(dom['file_domains'][:12])}")
        out.append("")
        out.append("### 历史（该域相关轮，按轮序）")
        by_round: dict[int, list[str]] = {}
        for bid in bids:
            if bid in all_blocks:
                seq, summary = all_blocks[bid]
                by_round.setdefault(seq, []).append(f"  ▸ {bid}: {summary}")
                assigned[bid] = name
            else:
                by_round.setdefault(0, []).append(f"  ▸ {bid}:（块描述缺失）")
        for seq in sorted(by_round):
            if seq:
                out.append(round_head(seq))
            out.extend(by_round[seq])
            out.append("")
        body = "\n".join(out)
        dom_chars = len(body) - total_chars
        total_chars = len(body)
        out.append(
            f"> 规模：{len(bids)} 块 / {len(by_round)} 轮 / "
            f"≈{dom_chars} 字符（≈{dom_chars // 2:,} tok）\n"
        )

    # 主 agent：独立思想 + 路由表
    out.append("## 主 agent 上下文")
    out.append("### 独立思想块（unassigned：未被任何域认领，归主 agent）")
    if indep_ids:
        out.append(f"（判定说明：{unassigned_map.get('reason', '')}）")
        for bid in indep_ids:
            s = all_blocks.get(bid, (0, "（缺失）"))[1]
            out.append(f"- {bid}：{s[:200]}")
    else:
        out.append("（无）")
    out.append("")
    out.append("### 域路由表（子 agent 交流对象选择）")
    for dom in domains:
        out.append(f"- {dom['name']}：{dom.get('description', '')}")
    out.append("")

    # 覆盖/重叠检查
    covered = set(assigned)
    part_of_domain = covered | set(indep_ids)
    missing = sorted(set(all_blocks) - part_of_domain)
    unassigned_ids = (po0.get("unassigned") or {}).get("block_ids") or []
    out.append("## 校验")
    out.append(f"- 总块数 {len(all_blocks)}；域内 {len(assigned)}；独立思想 {len(indep_ids)}")
    out.append(f"- 未归属块（unassigned 字段）：{unassigned_ids or '无'}")
    out.append(f"- 彻底遗漏的块：{missing or '无'}")
    dup = {}
    for dom in domains:
        for bid in dom.get("block_ids") or []:
            dup[bid] = dup.get(bid, 0) + 1
    overlapped = [b for b, n in dup.items() if n > 1]
    out.append(f"- 跨域重叠块：{overlapped or '无'}")
    out.append(f"- splittable={sa.get('splittable')}：{sa.get('reason', '')[:200]}")

    doc = "\n".join(out)
    open("docs/split-reassembly.md", "w", encoding="utf-8").write(doc)
    print(f"已写入 docs/split-reassembly.md（{len(doc):,} 字符）\n")
    for dom in domains:
        print(f"  域「{dom['name']}」：{len(dom.get('block_ids') or [])} 块")
    print(f"  独立思想：{len(indep_ids)} 块；遗漏：{missing or '无'}；重叠：{overlapped or '无'}")


if __name__ == "__main__":
    main()
