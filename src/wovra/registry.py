"""注册表：Agent = 条目 + 上下文视图 + 路由规则（机制三）。

**为什么是纯函数、零 LLM**：`context-differentiation-runtime.md` 定调
"不是先有 Agent 再分配上下文，而是先有真实工作，从上下文的自然分化中
产生 Agent"。分裂分析（`_split_rounds`）已经在做语义判断并产出域树
（`domains`）；把它们变成注册表条目是**机械翻译**——语义归模型、体量
归机制，同一条原则在组织层的落点。

路径 ID 是**职责路径，不是管理树**：`A-1-1` 表示"某大类 / 某功能 /
某具体文件"的职责粒度，管理关系永远最多两层（主 agent 直管所有子
agent）。故 ID 由域树路径机械生成：顶层域 → `A-1`、`A-2`…；父域为
`A-1` 的子域 → `A-1-1`、`A-1-2`…

幂等：同一批分裂产物重复 promote（崩溃补做、重启重放）只更新既有
条目，不重复追加——注册表是"现状的投影"，不是事件流水。
"""
from typing import Iterable

MAIN_AGENT_ID = "A"


def _index_by_name(domains: list[dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for d in domains:
        if isinstance(d, dict) and d.get("name"):
            out[str(d["name"])] = d
    return out


def _children_of(name: str, domains: list[dict]) -> list[dict]:
    out = []
    for d in domains:
        if isinstance(d, dict) and str(d.get("parent") or "") == name:
            out.append(d)
    return out


def build_entries(domains: Iterable[dict] | None) -> list[dict]:
    """把分裂产物的域树翻译成注册表条目（纯函数，零 LLM）。

    域树的 `parent` 指向父域 name（顶层留空）。父名不存在时按顶层处理
    ——畸形产物宁可多长一个顶层节点，也不能因为一条脏 parent 把整棵
    子树吞掉（对齐"排除性判断不做"：不裁视野、不丢块）。环由 visited
    集合截断，防止脏数据把递归拖死。
    """
    domains = [d for d in (domains or []) if isinstance(d, dict) and d.get("name")]
    if not domains:
        return []
    by_name = _index_by_name(domains)
    # parent 指向不存在的域 → 视为顶层（脏数据兜底）
    roots = [
        d for d in domains
        if not str(d.get("parent") or "").strip()
        or str(d.get("parent")).strip() not in by_name
    ]
    if not roots:
        # 脏数据兜底：每个节点的 parent 都指向已存在的域（最简单的情形是
        # 成环）→ 没有天然根。取出现顺序第一个节点当根，否则整棵树会被
        # 吞成空——"信息不切开、不丢块"比树形好看重要（环由 visited 截断）。
        roots = domains[:1]
    entries: list[dict] = []
    visited: set[str] = set()

    def walk(node: dict, path_id: str) -> None:
        name = str(node["name"])
        if name in visited:
            return  # 环/重复名：截断，不递归
        visited.add(name)
        entries.append({
            "id": path_id,
            "name": name,
            "description": str(node.get("description") or ""),
            "goal": str(node.get("goal") or ""),
            "file_domains": [str(f) for f in (node.get("file_domains") or [])],
            # 休眠是默认态（不对话即零成本），分裂产生的节点初始即休眠；
            # 路由/装配分化接入后才会有节点进入 active（后续步骤）
            "status": "dormant",
            "inbox": [],
        })
        for i, child in enumerate(_children_of(name, domains), start=1):
            walk(child, f"{path_id}-{i}")

    for i, root in enumerate(roots, start=1):
        walk(root, f"{MAIN_AGENT_ID}-{i}")
    return entries


def latest_domains(rounds: Iterable[dict] | None) -> list[dict]:
    """取**最近一次**已生效的分裂产物（域树）。

    分裂是对"全体现状"做归属分析，故最新一次即最完整的现状投影
    （对齐 `context-differentiation-runtime.md` 的"决定只认已生效
    状态 / 提案新鲜度"）。旧批次的域树被取代，不作并集——并集会
    把已消失的域复活成僵尸条目。
    """
    chosen: list[dict] = []
    for r in rounds or []:
        if not isinstance(r, dict):
            continue
        doms = r.get("domains")
        if isinstance(doms, list) and doms:
            chosen = doms          # 后出现的覆盖先出现的
    return chosen


def backfill(
    registry: list[dict] | None, rounds: Iterable[dict] | None
) -> tuple[list[str], list[str]]:
    """加载期回填：把历史上已生效的分裂产物补进注册表（幂等）。

    为什么需要：注册表落实是 2026-09-11 才接上的下游，此前 promote 过
    的分裂产物（`r["domains"]` 已进正式字段）不会再走 promote 路径，
    注册表就永远补不上——同 v1→v3 块迁移，属"历史数据没被新机制覆盖"。
    """
    return merge_into(registry, latest_domains(rounds))


def merge_into(
    registry: list[dict] | None, domains: Iterable[dict] | None
) -> tuple[list[str], list[str]]:
    """把域树条目幂等并入注册表，返回 (新增 id 列表, 更新 id 列表)。

    已存在的 id 只更新职责描述/文件域/目标（现状变了就跟着变），
    **不动 status 与 inbox**——那是运行时状态，重放产物不该把它抹掉。
    """
    entries = build_entries(domains)
    if not entries:
        return [], []
    if registry is None:
        registry = []
    by_id = {str(e.get("id")): e for e in registry if isinstance(e, dict)}
    added: list[str] = []
    updated: list[str] = []
    for entry in entries:
        existing = by_id.get(entry["id"])
        if existing is None:
            registry.append(entry)
            by_id[entry["id"]] = entry
            added.append(entry["id"])
            continue
        changed = False
        for key in ("name", "description", "goal", "file_domains"):
            if existing.get(key) != entry[key]:
                existing[key] = entry[key]
                changed = True
        if changed:
            updated.append(entry["id"])
    return added, updated
