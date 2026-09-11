"""域视图：把整理/分裂产物机械展开成「每个域一份上下文材料」（纯函数，零 LLM）。

## 这个模块解决什么

`context-differentiation-runtime.md` 的 Level 1 = ①域树落实为注册表条目
②**装配按域分化** ③规则优先路由。本模块是第②步的**材料层**：给每个职责域
派生一份可直接装配的上下文，但不接管主链装配（主链仍是单体装配——装配
一动就撞前缀纪律，见 `cache-layout-intent.md` §2）。

## 为什么是纯函数、零 LLM

与 `registry.py` 同一条纪律：**语义归模型、体量归机制**。域树（哪个文件属
哪个职责域）已经由分裂阶段（LLM）判过并生效；"把块分给域、把账本按文件域
切片"是机械翻译，不该再调一次模型。副作用是：本模块对历史 task.json 同样
成立，可离线对账。

## 视图分段（`cache-layout-intent.md` §3 的三段排法）

```
[1] 共享 system 人设        ← 公共部分在头部，分叉点尽量往后
[2] 重组上下文（本域块全分辨率 + 外域块一行指针 + 域卡）
[3] 子 agent 身份与约束      ← 稳定尾
[4] 运行时信封              ← 绝对尾部（现行纪律，不放中部）
```

不变量：命中 = 从第 0 token 对齐的连续公共前缀。故**公共部分在前、域相关在
中、身份在后**；各视图之间只共享 system 头（实测 ≈1%），收益在"每个视图各自
积累长链"，不在跨视图共享。

## 归属规则（机械、确定性、可对账）

每个块**恰好一个**归宿，按优先级：

1. **域显式声明**：块 ID 出现在某域的 `block_ids` 里（模型的跨域/例外声明）；
2. **文件域命中**：块的文件落在某域 `file_domains` 条目下（精确文件或目录
   前缀）——**最长前缀优先**（`src/a/b.py` 同时命中 `src/` 与 `src/a/` 时归后者，
   职责粒度更细的那个说了算）；
3. **兜底归主 agent**：无归宿的块（纯聊天块、独立思想、无文件块）归主 agent
   ——对齐"排除性判断不做 / 不丢块"：宁可归错到主 agent，也不让块消失。

`verify_completeness()` 把这个不变量变成可断言的事实（整理/分裂的污染就是
从"块没有归宿、静默消失"开始的）。
"""
from typing import Iterable, Optional

from . import blocks as blocks_module
from . import tokens as tokens
from .registry import MAIN_AGENT_ID, latest_domains

# 模型侧注入的账本分片口径（与 `task._MODEL_SIDE_SECTIONS` 对齐）：
# 全局节每轮都进每个视图（目标/现状是"我在干什么"的最小上下文；
# 决策升级与待办实验是**人机协同回路本身**，没有第二个注入点）；
# 分片节按文件域切片——域 A 的"已完成"对域 B 是纯噪声。
GLOBAL_STATE_FIELDS = ("goal", "current_status", "escalations", "experiments")
SHARDED_STATE_FIELDS = ("decisions", "known_issues", "open_questions")


def _file_domain_entries(domains: Iterable[dict]) -> list[tuple[str, str]]:
    """(路径前缀, 域名) 列表，按前缀长度降序（最长前缀优先匹配）。"""
    out: list[tuple[str, str]] = []
    for d in domains or []:
        if not isinstance(d, dict) or not d.get("name"):
            continue
        for fd in d.get("file_domains") or []:
            path = str(fd).strip().strip("/")
            if path:
                out.append((path, str(d["name"])))
    out.sort(key=lambda x: len(x[0]), reverse=True)
    return out


def _match_domain(path: str, entries: list[tuple[str, str]]) -> Optional[str]:
    """文件路径命中的域（精确文件或目录前缀，最长前缀优先）。"""
    for prefix, name in entries:
        if path == prefix or path.startswith(prefix + "/"):
            return name
    return None


def block_index(rounds: Iterable[dict]) -> dict[str, dict]:
    """全量块的索引：block_id → {seq, block, summary, round_has_summary}。

    块结构**现场重算 v3**（`segment_round_by_file`）——与整理产物、紧凑视图、
    expand_history 同一口径（持久化的 `r["blocks"]` 现在是 v3，但现场重算才是
    唯一真相：旧数据/旧进程落盘的都可能有残差）。
    """
    index: dict[str, dict] = {}
    for r in rounds or []:
        if not isinstance(r, dict):
            continue
        summaries = r.get("block_summaries") or {}
        for b in blocks_module.segment_round_by_file(r):
            index[str(b["id"])] = {
                "seq": r.get("seq"),
                "round": r,
                "block": b,
                "summary": summaries.get(str(b["id"])) or "",
            }
    return index


def ownership(
    domains: Iterable[dict] | None, index: dict[str, dict]
) -> dict[str, str]:
    """块 → 域名 的归属映射（机械、确定性；每个块恰有一个归宿）。

    规则见模块 docstring 的三级优先。返回的映射覆盖 `index` 里**每一个**块
    ——兜底归主 agent（`MAIN_AGENT_ID`）。
    """
    domains = [d for d in (domains or []) if isinstance(d, dict) and d.get("name")]
    entries = _file_domain_entries(domains)
    explicit: dict[str, str] = {}
    for d in domains:
        for bid in d.get("block_ids") or []:
            explicit.setdefault(str(bid), str(d["name"]))
    out: dict[str, str] = {}
    for bid, item in index.items():
        name = explicit.get(bid)
        if name is None:
            path = str(item["block"].get("file") or "")
            if path:
                name = _match_domain(path, entries)
        out[bid] = name or MAIN_AGENT_ID
    return out


def slice_state(state, file_domains: Iterable[str]) -> dict[str, list[str]]:
    """按文件域切片状态账本（机械匹配，零 LLM）。

    * 全局节（goal/current_status/escalations/experiments）**原样保留**——
      它回答"我在干什么 / 谁在等我拍板"，与域无关；
    * 分片节（decisions/known_issues/open_questions）只留**提到本域文件**的
      条目：条目文本里出现某文件域路径（或它的文件名）即算命中。

    为什么按文件路径而不按语义：语义归属是分裂阶段（LLM）的活；这里只是
    "这条账目提到了我的文件吗"的机械命中。**命中 0 条不是错误**——该域还没
    产生相关决策而已；漏切的风险由"全局节永不分片"兜住（人机协同回路与
    目标永不丢）。
    """
    paths = [str(f).strip().strip("/") for f in (file_domains or []) if str(f).strip()]
    needles = set()
    for p in paths:
        needles.add(p)
        needles.add(p.rsplit("/", 1)[-1])  # 文件名形态（条目常只写文件名）
    out: dict[str, list[str]] = {}
    for field in GLOBAL_STATE_FIELDS:
        value = getattr(state, field, "")
        if field == "current_status":
            if value:
                out[field] = [str(value)]
        elif value:
            # goal 是字符串、escalations/experiments 是列表——必须分别处理。
            # （踩过：对 goal 用 list(value) 会把它拆成单个字符。）
            out[field] = [str(value)] if isinstance(value, str) else list(value)
    for field in SHARDED_STATE_FIELDS:
        items = getattr(state, field, None) or []
        kept = [
            str(x) for x in items
            if any(n and n in str(x) for n in needles)
        ]
        if kept:
            out[field] = kept
    return out


def _terse_block(line_prefix: str, bid: str, item: dict, ledger) -> str:
    """一个块的一行重建（有整理描述用描述，没有则用机械 digest）。"""
    b = item["block"]
    target = b.get("file") or ", ".join(b.get("wrote_files") or []) or "无文件"
    state = ""
    if b.get("kind") == "file" and ledger is not None and b.get("file"):
        state = f"｜{ledger.state_of(b['file'])}"
    body = item["summary"] or blocks_module.block_digest(item["round"], b)
    return f"{line_prefix}{bid}（R{item['seq']}｜{target}{state}）: {body}"


def view_for_domain(
    name: str,
    domains: Iterable[dict] | None,
    index: dict[str, dict],
    owners: dict[str, str],
    state,
    *,
    id_of: Optional[dict[str, str]] = None,
    ledger=None,
) -> dict:
    """派生一个域的视图（纯数据，不装配）。

    返回：
        name / path_id / card（域卡行）/ own（本域块全分辨率行）/
        pointers（外域块一行指针）/ sharded（账本分片）/ counts / est_tokens
    """
    domains = [d for d in (domains or []) if isinstance(d, dict) and d.get("name")]
    node = next((d for d in domains if str(d["name"]) == name), None)
    id_of = id_of or {}
    path_id = id_of.get(name, MAIN_AGENT_ID if name == MAIN_AGENT_ID else name)

    card: list[str] = [f"[职责域] {name}（{path_id}）"]
    if node:
        if node.get("description"):
            card.append(f"职责：{node['description']}")
        if node.get("goal"):
            card.append(f"目标：{node['goal']}")
        fds = [str(f) for f in (node.get("file_domains") or [])]
        card.append(f"所有权文件域：{'、'.join(fds) if fds else '未划定'}")
        sup = node.get("superseded") or []
        for s in sup:
            if isinstance(s, dict) and s.get("note"):
                card.append(f"（前史：{s.get('note')}）")
    else:
        card.append("职责：全局协调与未归属事务（独立思想/零散块）")

    own_sorted: list[tuple[int, int, str]] = []
    by_owner: dict[str, list[str]] = {}
    for bid, owner in owners.items():
        item = index.get(bid)
        if item is None:
            continue
        if owner == name:
            # 排序键显式取 (轮号, 块号)，不用解析渲染后的文本（脆）
            try:
                bnum = int(str(bid).rsplit("-B", 1)[-1])
            except ValueError:
                bnum = 0
            own_sorted.append((
                int(item["seq"] or 0), bnum, _terse_block("▸ ", bid, item, ledger)
            ))
        else:
            by_owner.setdefault(owner, []).append(str(bid))

    own_sorted.sort()
    own = [line for _s, _b, line in own_sorted]

    # 外域块指针：**按属主域聚合**为一行（块 ID 清单），不是一块一行。
    # 实测量化（2026-09-11，269 块的真实会话）：一块一行约 250 行 ≈ 9K tok，
    # 而指针的用途只是"这个事实在别处、要细节就按 ID 展开或问属主"——块 ID
    # 清单同等地可达 expand_history / view_context，聚合后降到 ~2K tok。
    # 保底是"不丢指针"：每个外域块 ID 都必须出现在某一行的清单里。
    pointers: list[str] = []
    for owner, bids in sorted(by_owner.items()):
        bids.sort(key=lambda s: (int(s.lstrip("R").split("-B")[0]),
                                 int(s.rsplit("-B", 1)[-1])))
        pointers.append(f"· {owner}：{len(bids)} 块——{'、'.join(bids)}")

    # 账本分片：子域按自己的 file_domains 切片；**主 agent 拿全量**——
    # 它是路由器与默认执行者，需要全局视野（它的体量削减来自"外域块一行
    # 指针"，不来自账本切分）。子域切片里已含全局节（slice_state 保证）。
    if state is None:
        sharded = {}
    elif node is None:
        sharded = {f: list(getattr(state, f, None) or []) for f in GLOBAL_STATE_FIELDS}
        if state.current_status:
            sharded["current_status"] = [state.current_status]
        for f in SHARDED_STATE_FIELDS:
            items = getattr(state, f, None) or []
            if items:
                sharded[f] = list(items)
    else:
        sharded = slice_state(state, node.get("file_domains") or [])

    text_lines = card + ["", "[本域块]（全分辨率）"] + (own or ["（无）"])
    if pointers:
        text_lines += ["", "[外域块]（按属主域聚合的块 ID 清单；细节走 expand_history 取块原文，或 view_context 看对方视图）"] + pointers
    if sharded:
        text_lines += ["", "[本域账本]"]
        for field, items in sharded.items():
            label = {
                "goal": "目标", "current_status": "现状", "escalations": "决策升级",
                "experiments": "待办实验", "decisions": "已决策",
                "known_issues": "已知问题", "open_questions": "待解决问题",
            }.get(field, field)
            text_lines.append(f"{label}：" + "；".join(str(x) for x in items))
    text = "\n".join(text_lines)
    return {
        "name": name,
        "path_id": path_id,
        "card": card,
        "own": own,
        "pointers": pointers,
        "sharded": sharded,
        "counts": {"own": len(own), "pointers": len(pointers), "total": len(index)},
        "est_tokens": tokens.estimate(text),
        "text": text,
    }


def build_views(
    rounds: Iterable[dict],
    state,
    domains: Iterable[dict] | None = None,
    registry: Iterable[dict] | None = None,
    ledger=None,
) -> dict:
    """派生**全部**域视图 + 完整性对账（纯函数）。

    * `domains` 缺省取 `latest_domains(rounds)`（最近一次已生效的分裂产物）；
    * 主 agent 视图**恒定存在**（独立思想与未归属块的归宿），且它永远拿到
      "全部块里不属于任何子域"的那一份——这是"不丢块"的兜底落点。
    """
    rounds = [r for r in (rounds or []) if isinstance(r, dict)]
    domains = list(domains) if domains is not None else latest_domains(rounds)
    index = block_index(rounds)
    owners = ownership(domains, index)
    id_of: dict[str, str] = {}
    for e in registry or []:
        if isinstance(e, dict) and e.get("name"):
            id_of[str(e["name"])] = str(e.get("id") or "")
    if not id_of:
        # registry 未给（离线检视/尚未 promote）时，用同一套机械翻译就地派生
        # 路径 ID——视图的 path_id 必须与注册表一致，否则人视图里同一个域
        # 在两个地方显示成两个名字（踩过：退化成域名，与注册表栏对不上）。
        from .registry import build_entries
        for e in build_entries(domains):
            id_of[str(e["name"])] = str(e["id"])
    names: list[str] = [MAIN_AGENT_ID]
    for d in domains:
        if isinstance(d, dict) and d.get("name"):
            nm = str(d["name"])
            if nm not in names:
                names.append(nm)
    views = {
        nm: view_for_domain(
            nm, domains, index, owners, state, id_of=id_of, ledger=ledger
        )
        for nm in names
    }
    return {
        "views": views,
        "index": index,
        "owners": owners,
        "domains": domains,
        "completeness": verify_completeness(index, owners, views),
    }


def verify_completeness(
    index: dict[str, dict], owners: dict[str, str], views: dict[str, dict]
) -> dict:
    """块归属完整性对账：每个块恰有一个归宿，且真的落进了某个视图。"""
    missing = sorted(set(index) - set(owners))
    rendered: set[str] = set()
    for v in views.values():
        for line in v["own"]:
            rendered.add(line.split("（R", 1)[0].replace("▸ ", "").strip())
    dropped = sorted(set(index) - rendered)
    return {
        "total": len(index),
        "owned": len(owners),
        "missing": missing,
        "dropped_from_views": dropped,
        "ok": not missing and not dropped,
    }


def human_report(built: dict) -> list[str]:
    """域视图的人视图摘要（零 LLM；`wovra report` / `wovra maint` 共用）。"""
    if not built or not built.get("views"):
        return []
    lines = [
        "## 域视图（Level 1 第二步：装配按域分化的材料）",
        "",
        f"- 块总数 {built['completeness']['total']}，"
        f"归属完整：{'是' if built['completeness']['ok'] else '否'}",
    ]
    for name, v in built["views"].items():
        c = v["counts"]
        lines.append(
            f"- {v['path_id']}（{name}）：本域 {c['own']} 块、"
            f"指针 {c['pointers']} 块、约 {v['est_tokens']:,} tok"
        )
    if not built["completeness"]["ok"]:
        lost = built["completeness"]["missing"] + built["completeness"]["dropped_from_views"]
        lines.append(f"- ⚠ 未归属/未渲染块：{'、'.join(lost[:10])}")
    return lines
