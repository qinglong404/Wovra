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
import hashlib
import json
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
    """条目维护的**具体文件清单**（2026-09-12 新口径：归属按文件，不看路径）。

    2026-09-14：也认**叶子节点**的单文件字段（`file`——树里一个文件一个叶子）。
    """
    out: list[str] = []
    single = str(entry.get("file") or "").strip().strip("/")
    if single:
        out.append(single)
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
    """这个文件是不是该条目维护的（清单优先，历史前缀兜底）。

    2026-09-14：路径匹配走 `pathmatch`（精确 → 路径边界后缀 → 唯一同名；
    目录前缀按前缀归属）——模型抄的路径形态（带工作区前缀/绝对路径/./）
    不影响归属判断。
    """
    want = str(rel or "").strip()
    if not want:
        return False
    from . import pathmatch as pathmatch_module
    if pathmatch_module.matches(want, entry_files(entry)):
        return True
    return any(pathmatch_module.entry_matches(want, p)
               for p in entry_prefixes(entry))


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
    # 主 agent 的闲聊桶节点不参与文件校验（它按定义没有文件）
    domains = [d for d in (domains or [])
               if isinstance(d, dict) and not d.get("main_agent")]
    all_files = [str(f).strip().strip("/") for f in (files or []) if str(f).strip()]
    # 纯分组节点（自己没有文件、但子节点有）是**合法**的语义层：2026-09-15
    # 用户口径"分裂只需要写结构树和每个 agent 的职责"之后，分组节点本来就不该
    # 管文件——旧规则把它判"空域"整批拒收，正是"树写细一点就失败"的来源。
    has_children = {str(e.get("parent") or "") for e in entries if e.get("parent")}
    for entry in entries:
        name = f"{entry.get('id')}（{entry.get('name')}）"
        if (not entry_files(entry) and not entry_prefixes(entry)
                and str(entry.get("name") or "") not in has_children):
            defects.append(f"空域：{name} 没有给任何文件清单——不知道它维护什么")
        # 一个域自己内部不许重复声明（同文件既在清单又在某前缀下）
        if len(all_files) and (entry_files(entry) or entry_prefixes(entry)):
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
        # 未覆盖**不再**是缺陷（2026-09-15）：`_bind_files_by_path` 已把每个
        # 活性文件机械分给某个节点、分不出去的进 Runtime 机械桶——到这一步
        # 还"没人认领"只可能是把产物喂给校验器的调用方没走绑定，报出来只
        # 会让整批白算（用户口径："我只要效果"）。
    # **非 LIVE（历史）文件的落点不再由这里判定**（2026-09-15 用户报障：
    # "分裂又失败了"——实测会话 20260915-155432-b13727 整批产物只因两个历史
    # 文件落点缺陷被拒收，4 轮全 rejected、注册表只剩 Main，一个 agent 都没
    # 长出来）。非 LIVE 文件是**结论的载体**，不是谁写谁读的冲突源：Runtime
    # 在 `_bind_files_by_path` 后按"最近的活性兄弟/同目录"机械挂载，挂不上
    # 的进机械桶——不需要模型填、也不会因此作废整批。
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
    # **Runtime 自动归类的桶不生成 agent**（2026-09-15）：那是 `_auto_claim` 给
    # "没人认领的文件"按目录先接住的机械桶（`runtime_auto=True`），语义上还不是
    # 一条工作线。让它们参与选层会**凭空多出几个子 agent**（实测一次瘦身实验里
    # 4 个「cpp（Runtime 自动归类）」之类的桶各占一个 agent，直接破坏用户满意的
    # "不过分分裂"）。它们的文件仍在覆盖之内（覆盖检查按 domains 算，与 entries
    # 无关），下一批分裂里模型把文件认领进语义节点时自然接手。
    if any(d.get("runtime_auto") for d in domains):
        domains = [d for d in domains if not d.get("runtime_auto")]
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
    # **选层规则（2026-09-14 用户拍板，代码定分裂）**：
    # 顶层 >1 → 就在顶层分裂；顶层 ==1 → 往下取第一个 >1 的层分裂；
    # 最低到 LIVE 层（叶子 = 单个活性文件；链到底仍只有 1 个叶子就按它收口）。
    # 注：主 agent 的"闲聊/未归属"桶由 Runtime 物化成顶层节点（`main_agent`
    # 标记）——它让顶层计数 +1，但**不生成子 agent**（归主 agent 自己）。
    seen_levels: set[str] = set()
    while len(level) == 1:
        name = str(level[0].get("name") or "")
        if not name or name in seen_levels:
            break                         # 环/重名：停在这里，别死转
        seen_levels.add(name)
        kids = _children_of(name, domains)
        if not kids:
            break
        level = list(kids)

    def subtree(node: dict) -> tuple[list[str], list[str], list[str], dict]:
        """子树里的 (LIVE 文件, 历史文件, 约束, 文件描述)。"""
        files: list[str] = []
        hist: list[str] = []
        cons: list[str] = []
        notes: dict[str, str] = {}
        seen: set[str] = set()

        def walk(n: dict) -> None:
            name = str(n.get("name") or "")
            if not name or name in seen:
                return                        # 环/重复名截断
            seen.add(name)
            if n.get("file"):                 # 叶子：一个文件一个叶子（2026-09-14）
                files.append(str(n["file"]))
            files.extend(str(f) for f in (n.get("files") or []))
            hist.extend(str(f) for f in (n.get("history_files") or []))
            cons.extend(str(c) for c in (n.get("constraints") or []))
            for k, v in (n.get("file_notes") or {}).items():
                if str(k).strip() and str(v).strip():
                    notes[str(k).strip()] = " ".join(str(v).split())[:30]
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
        return uniq, uniq_hist, cons, notes

    entries: list[dict] = []
    for i, node in enumerate(level, start=1):
        if node.get("main_agent"):
            continue                      # 闲聊/未归属桶：归主 agent，不建子 agent
        files, hist, cons, notes = subtree(node)
        path_id = f"{parent_id}-{i}" if parent_id else top_id(i)
        # **描述兜底**（2026-09-15）：指令现在只要求**顶层节点**写描述（产物太长
        # 会撞端点输出预算被截断——实测一次 13,523 字符的产物整批作废、另有两次
        # 截断靠抢救才活下来）。非顶层节点没描述不是错误，但注册表/职责表不能
        # 出现"（无描述）"——路由就靠它。用**节点名 + 它的文件清单**机械拼一句。
        desc = str(node.get("description") or "").strip()
        if not desc:
            own = [str(f) for f in (files or [])][:4]
            desc = f"{node['name']}：维护 " + "、".join(own) + (
                "…" if len(files or []) > 4 else "") if own else                 f"{node['name']}（结构树节点）"
        entries.append({
            "id": path_id,
            "name": str(node["name"]),
            "description": desc,
            "goal": str(node.get("goal") or ""),
            "constraints": cons,
            # 归属**按文件**：`files` = 该域维护的具体文件清单（精确匹配）；
            # `file_domains` 是老产物的路径前缀，留作历史兼容读取
            "files": files,
            "file_domains": [str(f) for f in (node.get("file_domains") or [])],
            "history_files": hist,
            # 文件的一句话描述（≤30 字）：**第一次地图由分裂产物自带**，之后每轮
            # 由干活的 agent 自己改（`update_responsibility(file_notes=…)`）
            "file_notes": notes,
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


def domains_digest(domains: Iterable[dict] | None) -> str:
    """域树产物的指纹（预备归属的"对得上哪一版产物"判据）。

    用途：异步线程按**未生效**的产物预判归属（`pending_view` 预备区），
    轮边界生效时用它核对"预备所依据的产物 == 现在落地的产物"——对不上
    （产物被拒收、换了批次）就丢弃预备值，交给 `_settle_views` 重算。
    空产物给空串（"没有产物"本身也是一个可比较的状态）。
    """
    payload = [d for d in (domains or []) if isinstance(d, dict)]
    if not payload:
        return ""
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


def backfill(
    registry: list[dict] | None, rounds: Iterable[dict] | None
) -> tuple[list[str], list[str]]:
    """加载期回填：把历史上已生效的分裂产物补进注册表（幂等）。

    为什么需要：注册表落实是 2026-09-11 才接上的下游，此前 promote 过
    的分裂产物（`r["domains"]` 已进正式字段）不会再走 promote 路径，
    注册表就永远补不上——同 v1→v3 块迁移，属"历史数据没被新机制覆盖"。
    """
    return merge_into(registry, latest_domains(rounds))


def settle_ownership(registry: list[dict] | None, entries: list[dict]) -> list[str]:
    """**归属结算**：产物认领的文件，从其他条目的清单里减掉（原地改，返回明细）。

    为什么必须有这一步（2026-09-13 实测缺陷，worklog §78）：分裂产物是
    **全体 LIVE 文件的完全划分**（F3 强制每个 LIVE 文件恰好落一个域），可
    `merge_into` 原先只写新条目、只更新同 id 条目——**分裂方（主 agent）自己
    的清单谁也不碰**。于是主 agent 从创建时累积的清单（F5 谁创建谁拥有）会
    与子域清单**重叠**：实测会话 `20260913-125849-2963df` 里 `Main.files`(17)
    ∩ `D.files`(17) = 16 个文件，另有一个 `output/_verify_state.py` 一边在
    `Main.files` 算 LIVE、一边在 `D.history_files` 算已删，而盘上确实不存在。
    后果不是"页面难看"：`_file_permission` 先看 `mine`，两家 `file_owned_by`
    都返回 True → **两个 agent 都能写同一批文件**，F2 互斥事实上失效。

    口径来源是分裂指令自己写的那句「把**全部文件活**搬进它，主 agent 只剩
    闲聊 + 环境块」——写在提示词里不算数，机制负责执行（用户口径：机制机械
    强制，不靠提示词纪律）。所以：

    * 产物认领的 LIVE（`files`）→ 从其他条目的 `files` **和** `history_files` 减掉；
    * 产物认领的历史（`history_files`）→ 同样从两处减掉（一个文件只能有一个归宿）；
    * 清单里没了的文件，`file_notes` 一并摘掉，不给主 agent 留空抽屉。

    只查**精确文件清单**：老产物只有 `file_domains` 路径前缀，前缀归属是历史
    兼容读取（`entry_prefixes`），不参与结算——旧会话原样保留（用户口径
    2026-09-13：「旧会话原样保留，只修机制」）。
    """
    if not entries:
        return []
    claimed = {f for e in entries for f in entry_files(e)}
    claimed_hist = {f for e in entries for f in entry_history(e)}
    taken = claimed | claimed_hist
    new_ids = {str(e.get("id") or "") for e in entries}
    moved: list[str] = []
    for e in registry or []:
        if not isinstance(e, dict) or str(e.get("id") or "") in new_ids:
            continue
        who = f"{e.get('id')}（{e.get('name')}）"
        old_files = entry_files(e)          # 已归一（去空格/去首尾斜杠）
        old_hist = entry_history(e)
        keep_files = [f for f in old_files if f not in taken]
        keep_hist = [f for f in old_hist if f not in taken]
        lost = [f for f in old_files if f in taken]
        if len(keep_files) != len(old_files) or len(keep_hist) != len(old_hist):
            e["files"] = keep_files
            e["history_files"] = keep_hist
            alive = set(keep_files) | set(keep_hist)
            notes = {k: v for k, v in (e.get("file_notes") or {}).items()
                     if k in alive}
            if notes != (e.get("file_notes") or {}):
                e["file_notes"] = notes
            moved.extend(f"{who} 交出 {f}" for f in lost)
    return moved


def registry_defects(registry: Iterable[dict] | None) -> list[str]:
    """**跨条目**归属互斥体检（F2 的全注册表版，2026-09-13）。

    `split_defects` 只查**新产物内部**的互斥与完备——它看不见注册表里早已
    据着同一批文件的旧条目，于是 `产物 ⊕ 现状` 那一步无人查，F2 破坏被静默
    合并（worklog §78）。这里把互斥判据抬到**整个注册表**：任意两个条目的
    `files`/`history_files` 都不许交集。

    只查精确清单，理由同 `settle_ownership`（前缀属历史兼容口径）。
    """
    entries = [e for e in (registry or []) if isinstance(e, dict) and e.get("name")]
    defects: list[str] = []
    for i, a in enumerate(entries):
        for b in entries[i + 1:]:
            for ka in ("files", "history_files"):
                for kb in ("files", "history_files"):
                    left = set(entry_history(a) if ka == "history_files" else entry_files(a))
                    right = set(entry_history(b) if kb == "history_files" else entry_files(b))
                    both = sorted(left & right)
                    if both:
                        defects.append(
                            f"F2 违反：{a.get('id')}.{ka} 与 {b.get('id')}.{kb} "
                            f"共认 {len(both)} 个文件（{'、'.join(both[:5])}"
                            + ("…" if len(both) > 5 else "") + "）——一个文件只能一个域"
                        )
    return defects


def project_merge(
    registry: list[dict] | None,
    domains: Iterable[dict] | None,
    parent_id: str = "",
    retire_id: str = "",
) -> tuple[list[dict], list[str], list[str], list[str]]:
    """**预演**一次分裂合并（纯函数，不改传入的注册表）。

    返回 `(合并后的注册表, 新增 id, 更新 id, 归属结算明细)`。存在的理由：合并
    必须**先看结果再落地**——`registry_defects` 要在写入之前跑，不通过就整批
    拒收（"根基错误直接停，不许静默兜底"）。故 `merge_into` 内部走同一条路，
    只是最后把投影结果写回去：门与落地**同一套代码**，不会各算各的。
    """
    entries = build_entries(domains, parent_id)
    merged = [dict(e) for e in (registry or []) if isinstance(e, dict)]
    if not entries:
        return merged, [], [], []
    gone = str(retire_id or parent_id or "")
    retired: list[str] = []
    if gone and gone != MAIN_AGENT_ID:
        keep = [e for e in merged if str(e.get("id")) != gone]
        if len(keep) != len(merged):
            retired = [gone]
            merged[:] = keep
    by_id = {str(e.get("id")): e for e in merged}
    added: list[str] = []
    updated: list[str] = []
    for entry in entries:
        existing = by_id.get(entry["id"])
        if existing is None:
            merged.append(dict(entry))
            by_id[entry["id"]] = merged[-1]
            added.append(entry["id"])
            continue
        changed = False
        for key in ("name", "description", "goal", "files", "file_domains",
                    "history_files"):
            if existing.get(key) != entry[key]:
                existing[key] = entry[key]
                changed = True
        # 文件描述**合并**而不是覆盖：产物带的是"第一次地图"，而 agent 每轮自己
        # 改的那几条要活过下一次分裂（否则辛苦写的描述每次分裂都被抹掉）
        notes = dict(existing.get("file_notes") or {})
        notes.update(entry.get("file_notes") or {})
        if existing.get("file_notes") != notes:
            existing["file_notes"] = notes
            changed = True
        if changed:
            updated.append(entry["id"])
    # 归属结算必须在"新条目都在场"之后：结算要拿产物的全集去减别人的清单
    settle = settle_ownership(merged, [by_id[e["id"]] for e in entries])
    return merged, added + retired, updated, settle


def land(registry: list[dict], merged: list[dict]) -> None:
    """把投影结果写回注册表（原地），**按 id 复用原条目对象**。

    投影为了无副作用用的是浅拷贝，但运行时有别人握着条目引用（`_claim_new_file`/
    `update_responsibility` 拿到后就地改），整表换成拷贝会让那些引用变成"改了
    也不生效"的僵尸。故落地时把内容搬回原对象，只对真正新增的 id 用新对象。
    """
    by_old = {str(e.get("id")): e for e in registry if isinstance(e, dict)}
    landed: list[dict] = []
    for entry in merged:
        old = by_old.get(str(entry.get("id") or ""))
        if old is None:
            landed.append(entry)
        else:
            old.clear()
            old.update(entry)
            landed.append(old)
    registry[:] = landed


def merge_into(
    registry: list[dict] | None,
    domains: Iterable[dict] | None,
    parent_id: str = "",
    retire_id: str = "",
    settle_lines: list[str] | None = None,
) -> tuple[list[str], list[str]]:
    """把分裂产物幂等并入注册表，返回 (新增 id 列表, 更新 id 列表)。

    已存在的 id 只更新职责描述/文件清单/目标（现状变了就跟着变），
    **不动 status 与 inbox**——那是运行时状态，重放产物不该把它抹掉。

    **两级替换**（2026-09-12 用户口径，worklog §63）：`parent_id` = 正在分裂的
    那个域；它的产物平级登记为 `parent_id-1`、`parent_id-2`…，被分裂的域
    自己**随之消失**（`retire_id`，默认同 `parent_id`）。故注册表里永远是
    "主 agent + 一排平级子域"，不会长出三层。

    **归属结算**（2026-09-13，worklog §78）：产物认领的文件从其他条目（含分裂
    方主 agent）的清单里减掉——否则一个文件会同时挂在两个 agent 名下。传
    `settle_lines` 可以取回结算明细（给维护流水记账用）。
    """
    merged, added, updated, settle = project_merge(
        registry, domains, parent_id, retire_id
    )
    if registry is None:
        registry = []
    land(registry, merged)
    if settle_lines is not None:
        settle_lines.extend(settle)
    return added, updated
