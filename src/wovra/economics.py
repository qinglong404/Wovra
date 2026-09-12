"""分裂的经济判据：(B − B′) × N_future − C_split（机械公式，零 LLM）。

依据 `docs/split-assembly-plan.md` §13.3（对齐用户原始畅想）：
「拆分本身也有成本，只有 Context 隔离的收益超过拆分成本时才值得拆」。
`Benefit_split ≤ 0 → 不拆`。

## 两动作分离（§13.3）

**发现职责**（把一类工作记进职责表）与**创建 Agent**（真拆出一份视图）是两个
动作。故本模块只**算与记**，不自动拆：先记录类别，等真出现「base 很大 + 该类
占比高 + 未来还会继续干」时才 split。参数（N_future 怎么估、阈值定多少）在
有真实数据前不硬化。

## 单位与口径（都不猜，写明来源）

两侧一律以 **token 等效输入**计，否则会拿"每轮省下的量"去减"一次性的钱"：

* `B`（拆分前每次调用的基础）= 当前装配体量，实测值（`last_context_estimate`）。
* `B′`（拆分后每次调用的基础）= **最大视图体量**。未来调用落在哪个视图是未知的，
  取最大者即收益的**保守下界**（若连它都赚，那就一定赚）。
* `N_future`（预计未来调用次数）= **该域历史活跃轮数**（它名下块出现过的轮数）。
  同样是保守下界：这个域已经被用了 N 次，未来至少还会被用这么多。乘数不臆造
  增长率，也不给"以后会更忙"的乐观假设。
* `C_split`（一次性成本）= 一次前缀断裂的等效输入 `PREFIX_BREAK_TOKENS`。
  视图物化是零 LLM 纯函数（毫秒级），故一次性成本里唯一实打实的一项是缓存重建。
"""
from typing import Iterable, Optional

from .registry import MAIN_AGENT_ID

# 一次前缀断裂的等效输入（worklog §19 定价：净 18,830 tok 未命中 @ 0.4 元/M
# ≈ 0.0075 元/次）。用等效输入而不是钱：算式两侧同单位才可比。
PREFIX_BREAK_TOKENS = 18_830


def split_benefit(
    b_before: int,
    b_after: int,
    n_future: int,
    c_split: int = PREFIX_BREAK_TOKENS,
) -> dict:
    """机械算式 `(B − B′) × N_future − C_split`，返回可留痕的判定包。"""
    before, after = int(b_before or 0), int(b_after or 0)
    saved = max(0, before - after)
    calls = max(0, int(n_future or 0))
    cost = max(0, int(c_split or 0))
    benefit = saved * calls - cost
    return {
        "b_before": before,
        "b_after": after,
        "saved_per_call": saved,
        "n_future": calls,
        "c_split": cost,
        "benefit": benefit,
        "split": benefit > 0,
        "verdict": "值得拆" if benefit > 0 else "不拆（收益不抵成本）",
        "formula": f"({before:,} − {after:,}) × {calls} − {cost:,} = {benefit:,}",
    }


def assess(
    b_before: int,
    views: Iterable[dict],
    *,
    c_split: int = PREFIX_BREAK_TOKENS,
) -> dict:
    """对一批视图做经济评估：整体收益 + 逐域明细。

    `views` 每项需含 `name` / `tokens`（该视图材料体量）/ `rounds`（该域活跃轮数）。
    返回 `{"overall": {...}, "per_domain": [...]}`——overall 用"最大视图体量"当
    B′（保守下界），per_domain 逐个给"若只留下这一个域视图"的账。
    """
    items = [v for v in (views or []) if isinstance(v, dict)]
    sizes = [int(v.get("tokens") or 0) for v in items]
    after = max(sizes) if sizes else 0
    n_future = max((int(v.get("rounds") or 0) for v in items), default=0)
    overall = split_benefit(b_before, after, n_future, c_split)
    per_domain = []
    for v in items:
        per_domain.append({
            "name": str(v.get("name") or ""),
            "tokens": int(v.get("tokens") or 0),
            "rounds": int(v.get("rounds") or 0),
            **split_benefit(b_before, int(v.get("tokens") or 0),
                            int(v.get("rounds") or 0), c_split),
        })
    return {"overall": overall, "per_domain": per_domain}


def format_lines(assessed: dict, limit: int = 6) -> list[str]:
    """人/账本视图用的几行结论（不 dump 细节）。"""
    if not assessed:
        return []
    overall = assessed.get("overall") or {}
    lines = [
        f"分裂经济判据：{overall.get('formula')} → {overall.get('verdict')}"
        "（B=当前装配体量，B′=最大视图体量，N_future=该域活跃轮数，"
        f"C_split=一次前缀断裂 {PREFIX_BREAK_TOKENS:,} tok 等效输入）"
    ]
    ranked = sorted(
        assessed.get("per_domain") or [],
        key=lambda x: -int(x.get("benefit") or 0),
    )
    for item in ranked[:limit]:
        lines.append(
            f"  - {item['name']}：{item['tokens']:,} tok / 活跃 {item['rounds']} 轮"
            f" → {item['verdict']}（{item['benefit']:,}）"
        )
    return lines


def assess_from_watermarks(
    b_before: int, watermarks: Optional[dict] = None, *, c_split: int = PREFIX_BREAK_TOKENS
) -> dict:
    """便捷入口：直接吃 `views.view_watermarks()` 的产物。"""
    items = [
        {"name": name, "tokens": (wm or {}).get("tokens", 0),
         "rounds": (wm or {}).get("rounds", 0)}
        for name, wm in (watermarks or {}).items()
        if name != MAIN_AGENT_ID   # 主 agent 是兜底桶，不参与"要不要拆出它"
    ]
    return assess(b_before, items, c_split=c_split)
