"""把历史轮次落盘的 **v1 粗分块** 迁移为 v3（按文件聚合）。

**为什么需要**（2026-09-11 机制评审的收尾）：`close_round` 曾落 v1
（`segment_round`，以写/改为截止），而整理产物 / 紧凑视图 /
`expand_history` 一律按 v3（`segment_round_by_file`，按文件聚合）取块。
两套分块器**编号空间相同**（`R{n}-B{k}`）而切法不同——实测全会话 45 个
同 ID 块的覆盖区间**零一致**，于是任何"按 ID 查表"的地方都会静默错位
（R1 纯读轮曾因此丢掉 23/24 条块描述）。落盘口径已统一到 v3（新轮不再
产生 v1），但**历史数据仍是 v1**：留着就是那颗地雷——未来任何直接读
`r["blocks"]` 的代码都会踩。本模块把它刷成 v3。

**附带的标签修正**：v1 块里可能存着旧判定留下的 `fail_tags`（按全文
子串匹配"不存在"，把读成功误标成幽灵）。重算 v3 时标签会按新口径
（只看首行）重新生成，历史误标一并清除——这正是"历史块标签重算"的落点。

性质：
* **纯函数、零 LLM、确定性**——同一份事件流重算出的块号与当初生成
  `block_summaries` 时一致（同一个 `segment_round_by_file`），故迁移后
  描述与块仍严丝合缝；
* **幂等**——已是 v3 的轮不动（判据：块带 `events` 键，v1 块没有）；
* 只改 `rounds[*]["blocks"]`，不碰事件、不碰摘要、不碰任务状态。
"""
from .segment import segment_round_by_file


def is_v3(block: dict) -> bool:
    """块是否为 v3 结构（v3 块恒带 `events` 键；v1 块只有连续区间）。"""
    return "events" in block


def needs_migration(round_: dict) -> bool:
    """该轮的落盘块是否需要迁移（有块、且存在非 v3 块）。"""
    blocks = round_.get("blocks")
    if not blocks:
        return False
    return any(not is_v3(b) for b in blocks)


def migrate_round(round_: dict) -> bool:
    """就地迁移一轮；返回是否发生改动。"""
    if not needs_migration(round_):
        return False
    round_["blocks"] = segment_round_by_file(round_)
    return True


def migrate_rounds(rounds: list[dict]) -> tuple[int, list[tuple[int, str, str]]]:
    """批量迁移；返回 (改动轮数, [(seq, 旧块类, 新块类), ...])。

    report 里只列**真被改动**的轮，供 CLI 打印与人工核对。
    """
    changed = 0
    report: list[tuple[int, str, str]] = []
    for round_ in rounds:
        if not needs_migration(round_):
            continue
        before = ",".join(sorted({str(b.get("kind")) for b in round_["blocks"]}))
        migrate_round(round_)
        after = ",".join(
            sorted({str(b.get("kind")) for b in round_["blocks"]})
        )
        changed += 1
        report.append((int(round_.get("seq") or 0), before, after))
    return changed, report
