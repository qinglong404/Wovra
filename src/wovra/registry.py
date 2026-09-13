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
from typing import Iterable, Optional

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


def entry_files(entry: dict) -> list[str]:
    """条目维护的**具体文件清单**（2026-09-12 新口径：归属按文件，不看路径）。"""
    out: list[str] = []
    for f in entry.get("files") or []:
        rel = str(f).strip().strip("/")
        if rel:
            out.append(rel)
    return out


def entry_prefixes(entry: dict) -> list[str]:
    """条目上的**历史路径前缀**（老分裂产物只有 file_domains，按前缀匹配）。"""
    out: list[str] = []
    for f in entry.get("file_domains") or []:
        rel = str(f).strip().strip("/")
        if rel:
            out.append(rel)
    return out


def entry_history(entry: dict) -> list[str]:
    """条目下的**历史文件**（被重构替代/只读/已删）——不是分裂单元。

    它们挂在最相关 LIVE 文件所属的域下面当记录；对权限而言**只读**
    （谁都不写历史），但**有主**（不该报"没有任何域认领"）。
    """
    out: list[str] = []
    for f in entry.get("history_files") or []:
        rel = str(f).strip().strip("/")
        if rel:
            out.append(rel)
    return out


def file_owned_by(entry: dict, rel: str) -> bool:
    """这个文件是不是该条目维护的（精确清单优先，历史前缀兜底）。"""
    want = str(rel or "").strip().strip("/")
    if not want:
        return False
    if want in entry_files(entry):
        return True
    return any(want == p or want.startswith(p + "/") for p in entry_prefixes(entry))


def owner_of_file(
    registry: Iterable[dict] | None, rel: str
) -> Optional[str]:
    """文件归谁：**精确清单优先**，其次最长前缀（历史产物）。

    返回拥有者的展示串（`id（name）`）；没有任何域认领时返回 None——那是
    分裂/整理的缺陷（用户口径：不存在"未认领文件"），不是正常态。
    """
    want = str(rel or "").strip().strip("/")
    if not want:
        return None
    best: tuple[int, dict] | None = None
    for entry in registry or []:
        if not isinstance(entry, dict) or not entry.get("name"):
            continue
        if want in entry_files(entry):
            return f"{entry.get('id')}（{entry.get('name')}）"
        for prefix in entry_prefixes(entry):
            if want == prefix or want.startswith(prefix + "/"):
                if best is None or len(prefix) > best[0]:
                    best = (len(prefix), entry)
    if best is None:
        return None
    entry = best[1]
    return f"{entry.get('id')}（{entry.get('name')}）"


def split_defects(
    domains: Iterable[dict] | None,
    files: Iterable[str] | None,
    history_files: Iterable[str] | None = None,
) -> list[str]:
    """分裂产物的**机械缺陷**（F2 互斥 / F3 完备）——有缺陷就拒收，不静默兜底。

    用户口径（2026-09-12，worklog §62）：

    * **F2**：一个文件不可能两个子 agent 共同维护——任意两个域命中同一个文件
      就是缺陷（按文件清单与历史前缀两种表达都查，前缀重叠如 `src/` 与
      `src/wovra/` 也算）；
    * **F3**：被分裂者名下的文件必须 **100% 分完**——每个现有文件都得恰好一个归宿；
    * **结构**：域必须给出它维护的文件（空域 = 没说要管什么）。

    返回缺陷描述列表（空 = 通过）。调用方（promote 入口）据此**拒收 + 停 + 报错**
    ——"有些错误是根基，其错了，测试无意义"（用户原话）。
    """
    entries = build_entries(domains)
    defects: list[str] = []
    if not entries:
        return defects
    all_files = [str(f).strip().strip("/") for f in (files or []) if str(f).strip()]
    for entry in entries:
        name = f"{entry.get('id')}（{entry.get('name')}）"
        if not entry_files(entry) and not entry_prefixes(entry):
            defects.append(f"空域：{name} 没有给任何文件清单——不知道它维护什么")
        # 一个域自己内部不许重复声明（同文件既在清单又在某前缀下）
        if len(all_files):
            hits = [f for f in all_files if file_owned_by(entry, f)]
            if not hits:
                defects.append(f"空域：{name} 的文件清单没有命中任何现有文件")
    for path in all_files:
        owners = [f"{e.get('id')}（{e.get('name')}）" for e in entries
                  if file_owned_by(e, path)]
        if len(owners) > 1:
            defects.append(
                f"重叠：{path} 同时被 " + "、".join(owners) + " 认领"
                "（一个文件只能属于一个域）"
            )
        elif not owners:
            defects.append(
                f"未覆盖：{path} 没有任何域认领（分裂必须把现有文件 100% 分完）"
            )
    # **非 LIVE 的落点**（用户口径：只读/被取代/被删的都挂在最相关 LIVE 块
    # 下面当历史）——它们不属于分裂单元，但**必须有归宿**（写在某个域的
    # history_files 或 files 里），否则就是漏项。
    hist_entries = [(e, f"{e.get('id')}（{e.get('name')}）") for e in entries]

    def _claims(entry: dict, path: str) -> bool:
        return (file_owned_by(entry, path) or path in entry_history(entry))

    for path in [str(f).strip().strip("/")
                 for f in (history_files or []) if str(f).strip()]:
        owners = [label for e, label in hist_entries if _claims(e, path)]
        if len(owners) > 1:
            defects.append(
                f"历史重叠：{path} 同时被 " + "、".join(owners) + " 认领"
                "（历史文件也只能挂一处）"
            )
        elif not owners:
            defects.append(
                f"历史未落点：{path} 没有任何域认领（非 LIVE 也要挂在最相关"
                " LIVE 块所属的域下当历史）"
            )
    return defects


def build_entries(
    domains: Iterable[dict] | None, parent_id: str = ""
) -> list[dict]:
    """把分裂产物翻译成注册表条目（纯函数，零 LLM）。

    **两级、分裂层登记**（2026-09-12 用户拍板，worklog §63）：
    产物是一棵**架构**（LIVE 文件按相关性合并成小域→大域，叶子是文件块）。
    注册的 agent = **能形成有效分裂的那一层**的节点，**平级**登记；被分裂的
    那个域（`parent_id`）随之**消失**（两级替换：A 分裂 → A-1、A-2，A 不再存在）。

    层的取法：主 agent 分裂（`parent_id=""`）时，顶层 1 个节点**也裂**（裂 1 个
    子 agent，把文件活全搬出主 agent）；子 agent 再分裂时，顶层只有 1 个节点就
    **往下钻**，直到出现 ≥2 个节点；**最细到一个 LIVE 文件为止**（不再拆到块）。

    ID：主 agent 分裂出的域取 `A`、`B`、`C`…（`top_id`）；A 再分裂则产物是
    `A-1`、`A-2`…（名字留血缘，层级仍平级）。

    每个条目的 `files` = 该节点**子树**里全部 LIVE 文件（精确清单）；
    `history_files` = 子树里的历史文件（被取代/只读/已删，挂在这条线下）。
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
        # 吞成空——"信息不切开、不丢块"比树形好看重要。
        roots = domains[:1]
    level = list(roots)
    if parent_id or len(level) != 1:
        # 子 agent 分裂：顶层单节点要下钻；主 agent 分裂：顶层单节点保留
        while len(level) == 1:
            kids = _children_of(str(level[0].get("name")), domains)
            if not kids:
                break
            level = list(kids)

    def subtree(node: dict) -> tuple[list[str], list[str], list[str]]:
        """子树里的 (LIVE 文件, 历史文件, 约束)。"""
        files: list[str] = []
        hist: list[str] = []
        cons: list[str] = []
        seen: set[str] = set()

        def walk(n: dict) -> None:
            name = str(n.get("name") or "")
            if not name or name in seen:
                return                        # 环/重复名截断
            seen.add(name)
            files.extend(str(f) for f in (n.get("files") or []))
            hist.extend(str(f) for f in (n.get("history_files") or []))
            cons.extend(str(c) for c in (n.get("constraints") or []))
            for child in _children_of(name, domains):
                walk(child)

        walk(node)
        # 老产物只有 file_domains（路径前缀）：当作历史兼容挂在同一处
        uniq: list[str] = []
        for f in files:
            if f and f not in uniq:
                uniq.append(f)
        uniq_hist: list[str] = []
        for f in hist:
            if f and f not in uniq and f not in uniq_hist:
                uniq_hist.append(f)
        return uniq, uniq_hist, cons

    entries: list[dict] = []
    for i, node in enumerate(level, start=1):
        files, hist, cons = subtree(node)
        path_id = f"{parent_id}-{i}" if parent_id else top_id(i)
        entries.append({
            "id": path_id,
            "name": str(node["name"]),
            "description": str(node.get("description") or ""),
            "goal": str(node.get("goal") or ""),
            "constraints": cons,
            # 归属**按文件**：`files` = 该域维护的具体文件清单（精确匹配）；
            # `file_domains` 是老产物的路径前缀，留作历史兼容读取
            "files": files,
            "file_domains": [str(f) for f in (node.get("file_domains") or [])],
            "history_files": hist,
            # 休眠是默认态（不对话即零成本），分裂产生的节点初始即休眠
            "status": "dormant",
            "inbox": [],
            # ---- 观测字段（不落"账"，见模块 docstring）----
            "ctx_cur": 0,
            "ctx_peak": 0,
            "window": 0,
        })
    return entries


# 曾经的 `runtime_stats()`（写入式 per-agent 账）已删除：账本改为**派生**
# ——见 `views.agent_ledger`（承载轮/答复轮/步数/转出从轮与事件流现场算）。
# 这里不再提供同名函数，是为了让"读存下来的账"这条路彻底断掉：留着它，
# 早晚有人接回去，然后又一次在第一次分裂之后读到过期的数。



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
    registry: list[dict] | None,
    domains: Iterable[dict] | None,
    parent_id: str = "",
    retire_id: str = "",
) -> tuple[list[str], list[str]]:
    """把分裂产物幂等并入注册表，返回 (新增 id 列表, 更新 id 列表)。

    已存在的 id 只更新职责描述/文件清单/目标（现状变了就跟着变），
    **不动 status 与 inbox**——那是运行时状态，重放产物不该把它抹掉。

    **两级替换**（2026-09-12 用户口径，worklog §63）：`parent_id` = 正在分裂的
    那个域；它的产物平级登记为 `parent_id-1`、`parent_id-2`…，被分裂的域
    自己**随之消失**（`retire_id`，默认同 `parent_id`）。故注册表里永远是
    "主 agent + 一排平级子域"，不会长出三层。
    """
    entries = build_entries(domains, parent_id)
    if not entries:
        return [], []
    if registry is None:
        registry = []
    gone = str(retire_id or parent_id or "")
    retired: list[str] = []
    if gone and gone != MAIN_AGENT_ID:
        keep = [e for e in registry
                if not (isinstance(e, dict) and str(e.get("id")) == gone)]
        if len(keep) != len(registry):
            retired = [gone]
            registry[:] = keep
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
        for key in ("name", "description", "goal", "files", "file_domains",
                    "history_files"):
            if existing.get(key) != entry[key]:
                existing[key] = entry[key]
                changed = True
        if changed:
            updated.append(entry["id"])
    return added + retired, updated
