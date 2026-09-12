"""注册表：Agent = 条目 + 上下文视图 + 路由规则（机制三）。

**为什么是纯函数、零 LLM**：`context-differentiation-runtime.md` 定调
"不是先有 Agent 再分配上下文，而是先有真实工作，从上下文的自然分化中
产生 Agent"。分裂分析（`_split_rounds`）已经在做语义判断并产出域树
（`domains`）；把它们变成注册表条目是**机械翻译**——语义归模型、体量
归机制，同一条原则在组织层的落点。

## 路径 ID 体系（2026-09-12 用户拍板改版）

路径 ID 是**职责路径，不是管理树**：管理关系永远只有"主 agent 直管所有
子 agent"这一层，ID 只表达职责粒度。旧体系把主 agent 占成 `A`，于是第一次
分裂产出的是 `A-1`、`A-2`…（看起来像"主 agent 的子目录"），再裂一层成
`A-1-1` —— 与 `docs/思考内容与AI对话.md` §五（"A = 大类、A-1 = 子类"）正好
差一级。新体系：

```text
Main                     主 agent（不再占字母）
├── A   ← 第一次分裂的顶层域（A、B、C…）
│    └── A-1、A-2          A 满了以后在 A 内部再裂一层
└── B
     └── B-1
```

顶层域按出现顺序取字母（1→`A`、26→`Z`、27→`AA`，Excel 式无上限）；子域在
父 ID 后接 `-序号`。旧会话由 `migrate_legacy_id` + `Task.load` 的加载期迁移
一次性搬过来（见 `AGENT_ID_SCHEME`）。

幂等：同一批分裂产物重复 promote（崩溃补做、重启重放）只更新既有
条目，不重复追加——注册表是"现状的投影"，不是事件流水。
"""
from typing import Iterable

MAIN_AGENT_ID = "Main"

# 旧体系的主 agent 标签（迁移用）：`A` 是主 agent，`A-1`、`A-1-2` 是路径。
LEGACY_MAIN_AGENT_ID = "A"

# ID 体系版本（落盘在 Task.agent_id_scheme）：1 = 旧（主 agent 占 A），
# 2 = 新（主 agent = Main，顶层域 A、B、C…）。缺省视作 1（历史数据）。
AGENT_ID_SCHEME = 2


def top_id(index: int) -> str:
    """顶层域的字母 ID：1→`A`、26→`Z`、27→`AA`（Excel 式，无上限）。"""
    try:
        n = int(index)
    except (TypeError, ValueError):
        n = 1
    if n < 1:
        n = 1
    out = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        out = chr(ord("A") + rem) + out
    return out


def migrate_legacy_id(path_id: str) -> str:
    """旧路径 ID → 新路径 ID（纯函数；**只能对旧体系数据调用一次**）。

    * `A`（主 agent）→ `Main`；
    * `A-<k>[-<m>…]` → `<字母 k>` + 余下的 `-m…`（`A-1-2` → `A-2`）。

    形状不认识（已经是新体系、或纯名字）时原样返回——故对同一份数据重复
    调用**不保证幂等**（`A-1` 在新体系里是"域 A 的子域 1"，再迁一次会被
    读成"域 A"），所以调用方必须先看 `Task.agent_id_scheme`（见 `task.py`
    的加载期迁移）。
    """
    old = str(path_id or "").strip()
    if not old:
        return old
    if old == LEGACY_MAIN_AGENT_ID:
        return MAIN_AGENT_ID
    parts = old.split("-")
    if parts[0] != LEGACY_MAIN_AGENT_ID or len(parts) < 2 or not parts[1].isdigit():
        return old
    return "-".join([top_id(int(parts[1]))] + parts[2:])



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

    顶层域取字母（`A`、`B`、`C`…），子域 `A-1`、`A-2`…（见模块 docstring）。

    条目上带 **per-agent 运行时账**（3a，2026-09-12 用户口径：每个子 agent
    有自己的轮次/步数/上下文窗口与占比）：`rounds` / `steps` / `ctx_cur` /
    `ctx_peak` / `window` / `handoffs`。新建条目从 0 起；既有条目由
    `merge_into` 保留（它们是运行时状态，重放产物不该抹掉）。
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
            # ---- per-agent 运行时账（3a）----
            "rounds": 0,      # 该 agent 名下的闭合轮数（按轮闭合时的 active_view）
            "steps": 0,       # 该 agent 累计步数（同上归属）
            "handoffs": 0,    # 它转出（route_to）了多少轮——主 agent 的参与度账
            "ctx_cur": 0,     # 它那份上下文最近一次装配的体量（tok）
            "ctx_peak": 0,    # 峰值
            "window": 0,      # 它自己的水位/窗口（0 = 用全局默认，见 support）
        })
        for i, child in enumerate(_children_of(name, domains), start=1):
            walk(child, f"{path_id}-{i}")

    for i, root in enumerate(roots, start=1):
        walk(root, top_id(i))
    return entries


def runtime_stats(registry: Iterable[dict] | None) -> dict[str, dict]:
    """各 agent 的运行时账（3a）：轮次/步数/转出/上下文体量与窗口占比。

    **按名字与 ID 双键**：`active_view` 上主 agent 存的是哨兵 ID（`Main`），
    子域存的是名字——消费方（CLI 人视图、serve/agent_stats）两种键都会查，
    这里一次给全，免得各自再猜一遍。
    """
    out: dict[str, dict] = {}
    for entry in registry or []:
        if not isinstance(entry, dict) or not entry.get("name"):
            continue
        cur = int(entry.get("ctx_cur") or 0)
        window = int(entry.get("window") or 0)
        stat = {
            "id": str(entry.get("id") or ""),
            "name": str(entry.get("name") or ""),
            "rounds": int(entry.get("rounds") or 0),
            "steps": int(entry.get("steps") or 0),
            "handoffs": int(entry.get("handoffs") or 0),
            "ctx_cur": cur,
            "ctx_peak": int(entry.get("ctx_peak") or 0),
            "window": window,
            "share": (cur / window) if window else 0.0,
        }
        out[stat["name"]] = stat
        if stat["id"] and stat["id"] != stat["name"]:
            out[stat["id"]] = stat
    return out



def _legacy_id_shaped(value: str) -> bool:
    """值是否恰好是旧体系的路径 ID 形态（`A` / `A-1` / `A-1-2`…）。"""
    v = str(value or "").strip()
    if not v:
        return False
    parts = v.split("-")
    if parts[0] != LEGACY_MAIN_AGENT_ID:
        return False
    return all(p.isdigit() for p in parts[1:]) if len(parts) > 1 else True


def migrate_agent_ids(
    registry: list[dict] | None,
    rounds: Iterable[dict] | None = None,
) -> tuple[int, list[str]]:
    """把旧体系（主 agent 占 `A`）的 ID **就地**迁到新体系（2026-09-12）。

    只动 **ID 形态**的字段，不碰任何自由文本与职责描述：

    * 注册表条目的 `id`（`A` → `Main`；`A-1` → `A`；`A-1-2` → `A-2`）；
    * 条目收件箱里的 `from`/`to`（值恰为旧 ID 形态时）；
    * 轮上的 `active_view` / `route_explicit` 与 `route_handoff.{from,to}`
      ——这些存的是**域名**，只有主 agent 的哨兵值 `A` 需要换名（域是
      模型给的中文名，不是 ID）。`Task.pending_view` 同理，由调用方
      （`task.py` 的加载期迁移）自己换。

    **不是幂等的**：新体系里 `A-1` 另有含义（域 A 的子域 1），重复迁移会
    把子域读成顶层域。故调用方必须先确认 `agent_id_scheme < 2`
    （落盘在 `Task.agent_id_scheme`，见 `task.py` 的加载期迁移）。

    返回 `(改动处数, 样例说明)`。
    """
    changed = 0
    samples: list[str] = []
    for entry in registry or []:
        if not isinstance(entry, dict):
            continue
        old = str(entry.get("id") or "")
        new = migrate_legacy_id(old)
        if new != old:
            entry["id"] = new
            changed += 1
            if len(samples) < 8:
                samples.append(f"{old}→{new}")
        for item in entry.get("inbox") or []:
            if not isinstance(item, dict):
                continue
            for key in ("from", "to"):
                value = str(item.get(key) or "")
                if value and _legacy_id_shaped(value):
                    item[key] = migrate_legacy_id(value)
                    changed += 1
    for r in rounds or []:
        if not isinstance(r, dict):
            continue
        for key in ("active_view", "route_explicit"):
            if str(r.get(key) or "") == LEGACY_MAIN_AGENT_ID:
                r[key] = MAIN_AGENT_ID
                changed += 1
        handoff = r.get("route_handoff")
        if isinstance(handoff, dict):
            for key in ("from", "to"):
                if str(handoff.get(key) or "") == LEGACY_MAIN_AGENT_ID:
                    handoff[key] = MAIN_AGENT_ID
                    changed += 1
    return changed, samples


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
