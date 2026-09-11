"""分裂生命周期动作：split / no split（机械判定，零 LLM）。

依据 `docs/split-assembly-plan.md` §13.2：`split` / `no split` / `merge` /
`dissolve` 都是**运行时行为**，不是架构铁律；第一版只做前两个——
merge / dissolve 等真出现「Agent 垃圾堆」的实证再说（不给未发生的问题写代码）。

## 两个动作分离（§13.3）

**发现职责**（把一类工作写进职责表）与**创建 Agent**（真拆出一份视图）不是
一回事。本模块只回答「这批产物的每个域，相对现状是新建、还是延续」，并把
经济判据的裁定一并记下；视图仍由装配层按 domains 机械派生（零 LLM、毫秒级），
故「no split」在机制上的含义是「**不把它当新的分裂单元记账、不为它切路由**」，
不是把已派生的视图删掉——删视图会让它名下的块失去归宿，与「不丢块」冲突。

## status 语义（本次补齐）

* `dormant`：注册表里的默认态（不对话即零成本）；
* `active`：该域**有人用**——它名下有真实轮次（视图非空）。判据是材料事实
  （命中轮数 > 0），不是模型自述；
* 主 agent 恒 `active`（它是默认执行者）。

四动作里的 merge/dissolve 一旦落地，才会出现 `merged` / `closed` 等取值；
现在不预留空状态（预留的空状态会让人以为机制已经在了）。

> 「无域」不等于「不可分」：产物里域列表为空时（判不可分 / 全链单子），
> 本模块返回空计划，不产生任何动作与留痕——没动作就是没动作。
"""
from typing import Iterable, Optional

from .economics import PREFIX_BREAK_TOKENS, split_benefit
from .registry import MAIN_AGENT_ID

# 动作取值（名字即口径，不再造同义词）
SPLIT = "split"
NO_SPLIT = "no split"

ACTIVE = "active"
DORMANT = "dormant"


def _existing_by_name(registry: Optional[Iterable[dict]]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for e in registry or []:
        if isinstance(e, dict) and e.get("name"):
            out[str(e["name"])] = e
    return out


def plan(
    domains: Iterable[dict] | None,
    registry: Optional[Iterable[dict]] = None,
    watermarks: Optional[dict] = None,
    *,
    b_before: int = 0,
    c_split: int = PREFIX_BREAK_TOKENS,
) -> list[dict]:
    """给每个域定一个生命周期动作。

    * `split`：注册表里还没有这个域（本批**发现**了新职责）→ 记账为新建单元；
    * `no split`：注册表里已有（延续；重放/崩溃补做同样落这里，幂等）；
    * `status`：该域名下有命中轮 → `active`，否则 `dormant`（材料事实）；
    * `benefit` / `verdict`：经济判据的逐域读数（`b_before` = 当前装配体量，
      `B′` = 该域视图体量，`N_future` = 该域活跃轮数）。
    """
    existing = _existing_by_name(registry)
    marks = watermarks or {}
    out: list[dict] = []
    for d in domains or []:
        if not isinstance(d, dict) or not d.get("name"):
            continue
        name = str(d["name"])
        mark = marks.get(name) or {}
        rounds = int(mark.get("rounds") or 0)
        tokens_ = int(mark.get("tokens") or 0)
        new = name not in existing
        econ = split_benefit(b_before, tokens_, rounds, c_split)
        out.append({
            "name": name,
            "id": str((existing.get(name) or {}).get("id") or ""),
            "action": SPLIT if new else NO_SPLIT,
            "status": ACTIVE if rounds else DORMANT,
            "tokens": tokens_,
            "rounds": rounds,
            "benefit": econ["benefit"],
            "verdict": econ["verdict"],
            "reason": (
                f"本批发现新职责（视图 {tokens_:,} tok／活跃 {rounds} 轮）"
                if new else
                f"已知域延续（视图 {tokens_:,} tok／活跃 {rounds} 轮）"
            ),
        })
    return out


def apply_status(
    registry: Optional[list], actions: Iterable[dict] | None
) -> list[str]:
    """把动作里的 status 落到注册表条目上（原地），返回被改动的 id 列表。

    只动 `status`——职责描述与文件域由 `registry.merge_into` 负责（分工：
    语义归 merge、运行时状态归这里），免得两处都写同一字段。
    """
    if registry is None:
        return []
    by_id = {
        str(e.get("id")): e for e in registry if isinstance(e, dict)
    }
    changed: list[str] = []
    for item in actions or []:
        entry = by_id.get(str(item.get("id") or ""))
        if entry is None:
            continue
        want = str(item.get("status") or "")
        if want and entry.get("status") != want:
            entry["status"] = want
            changed.append(str(entry.get("id")))
    return changed


def summary_lines(actions: Iterable[dict] | None, limit: int = 6) -> list[str]:
    """人/账本视图用的结论行（不 dump 细节）。"""
    items = [a for a in (actions or []) if isinstance(a, dict)]
    if not items:
        return []
    new_cnt = sum(1 for a in items if a["action"] == SPLIT)
    head = (
        f"生命周期动作：{len(items)} 域——{split_action_count(items, SPLIT)} 个 "
        f"{SPLIT}（本批新发现职责）、{split_action_count(items, NO_SPLIT)} 个 "
        f"{NO_SPLIT}（已知域延续）；发现职责≠创建 Agent，"
        "经济判据为负者只记录不拆（plan §13.3）"
    )
    lines = [head]
    for a in items[:limit]:
        lines.append(
            f"  - {a['name']}：{a['action']}／{a['status']}"
            f"（{a['tokens']:,} tok／{a['rounds']} 轮 → {a['verdict']}）"
        )
    if len(items) > limit:
        lines.append(f"  …（另 {len(items) - limit} 个域略）")
    return lines


def split_action_count(items: Iterable[dict], action: str) -> int:
    return sum(1 for a in items if a.get("action") == action)
