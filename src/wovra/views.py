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
[2] 本视图历史（逐轮：命中轮的用户原文 + 用户块 + 本域块一行全分辨率）
[3] 子 agent 身份与约束      ← 稳定尾
[4] 运行时信封              ← 绝对尾部（现行纪律，不放中部）
```

不变量：命中 = 从第 0 token 对齐的连续公共前缀。故**公共部分在前、域相关在
中、身份在后**；各视图之间只共享 system 头（实测 ≈1%），收益在"每个视图各自
积累长链"，不在跨视图共享。

## 重组规则：块作筛子、轮作单位（2026-09-12 用户拍板）

整理产物（块）是唯一的原料，分裂写下的文件域（`file_domains`）是筛子，
且筛子**只用一次**——重组出上下文之后它就失效，视图此后按正常上下文纯追加
（增删文件不受限），直到该视图自己水位再达标时重复同一过程。

逐轮过一遍，对每个视图：

* 该轮命中**本视图名下**的块 ≥1 → 把该轮完整用户原文（`👤 / 🎯 / 📌`）与该轮
  的用户块、以及命中的块一行全分辨率描述，按时间序摘进本视图；
* 该轮一块都没命中 → **整轮不出现**（不留轮号占位、不留涉及文件清单、
  不留块 ID）；
* 非本视图的块——描述、摘要、块 ID——在本视图字节里一次都不出现（隔离第一）。

块 → 域 的归属是纯集合判断（文件在不在该域的 `file_domains` 下）：

1. **文件域命中**：块的文件落在某域条目下，**最长前缀优先**（`src/a/b.py`
   同时命中 `src/` 与 `src/a/` 时归后者）——归属必须确定，一块只进一个视图；
2. **兜底归主 agent**：无文件的块（环境块、保底块、用户块）归主 agent
   ——环境块恒定归主 agent（用户口径：它不是任何域的活）；
   "排除性判断不做 / 不丢块"仍然成立：宁可归到主 agent，也不让块消失。

（模型侧产物里的 `block_ids` 不再作为归属依据——它是 v2 时代的命名式指派，
与本规则冲突时以文件集合为准。）

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

    纯集合判断（见模块 docstring 的重组规则）：块的 `file` 落在某域
    `file_domains` 下即归它（最长前缀优先）；无文件的块（环境块、保底块、
    用户块）恒定归主 agent。返回的映射覆盖 `index` 里**每一个**块——兜底归
    主 agent（`MAIN_AGENT_ID`），不丢块。

    注意：产物里的 `block_ids` 不参与判据（v2 命名式指派的遗留）；判据只有
    文件集合本身，这样重组与分裂两边共用同一套口径。
    """
    domains = [d for d in (domains or []) if isinstance(d, dict) and d.get("name")]
    entries = _file_domain_entries(domains)
    out: dict[str, str] = {}
    for bid, item in index.items():
        path = str(item["block"].get("file") or "")
        name = _match_domain(path, entries) if path else None
        out[bid] = name or MAIN_AGENT_ID
    return out


def slice_state(
    state, file_domains: Iterable[str], *, exclude: bool = False
) -> dict[str, list[str]]:
    """按文件域切片状态账本（机械匹配，零 LLM）。

    * 全局节（goal/current_status/escalations/experiments）**原样保留**——
      它回答"我在干什么 / 谁在等我拍板"，与域无关；
    * 分片节（decisions/known_issues/open_questions）只留**提到本域文件**的
      条目：条目文本里出现某文件域路径（或它的文件名）即算命中。

    为什么按文件路径而不按语义：语义归属是分裂阶段（LLM）的活；这里只是
    "这条账目提到了我的文件吗"的机械命中。**命中 0 条不是错误**——该域还没
    产生相关决策而已；漏切的风险由"全局节永不分片"兜住（人机协同回路与
    目标永不丢）。

    `exclude=True`：只留**没提到**这些文件的条目——主 agent 视图用（它的
    file_domains 是所有子域文件的补集；反正已归子域的账目，子域自己看）。
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
            if any(n and n in str(x) for n in needles) != exclude
        ]
        if kept:
            out[field] = kept
    return out


def _terse_block(line_prefix: str, bid: str, item: dict, ledger) -> str:
    """一个块的一行重建（有整理描述用描述，没有则用机械 digest）。"""
    b = item["block"]
    kind = str(b.get("kind") or "")
    if kind == "user":
        target = "用户补充输入"
    elif kind == "environment":
        tags = "、".join(str(t) for t in (b.get("command_types") or []))
        target = f"环境准备（{tags}）" if tags else "环境准备"
    else:
        target = b.get("file") or ", ".join(b.get("wrote_files") or []) or "无文件"
    state = ""
    if kind == "file" and ledger is not None and b.get("file"):
        state = f"｜{ledger.state_of(b['file'])}"
    body = item["summary"] or blocks_module.block_digest(item["round"], b)
    return f"{line_prefix}{bid}（R{item['seq']}｜{target}{state}）: {body}"


def _round_lines(r: dict) -> list[str]:
    """轮头（轮号 + 用户原文 / 意图 / 关键约束）——与紧凑视图同格式。"""
    r = r or {}
    ui = r.get("user_input") or {}
    anchor = r.get("merged_anchor")
    lines = [f"[{anchor}]" if anchor else f"[R{r.get('seq')}]"]
    if ui.get("original"):
        lines.append(f"👤 用户: \"{ui['original']}\"")
    if ui.get("normalized"):
        lines.append(f"🎯 意图: {ui['normalized']}")
    if ui.get("key_constraints"):
        lines.append(f"📌 关键约束: {ui['key_constraints']}")
    return lines


def _bnum(bid: str) -> int:
    """块号（排序键；解析不出来当 0）。"""
    try:
        return int(str(bid).rsplit("-B", 1)[-1])
    except ValueError:
        return 0


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

    重组规则（模块 docstring）：逐轮筛——该轮有本视图名下的块才成一段，
    段内给「轮头（用户原文/意图/约束）+ 该轮用户块 + 本域块一行描述」；
    一块都没命中的轮整轮不出现（不留轮号、不留文件名、不留块 ID）。

    返回：
        name / path_id / card（域卡行）/ history（逐轮渲染文本）/
        own_ids（本视图含的块 ID）/ sharded（账本分片）/ counts / est_tokens / text
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
        card.append("职责：全局协调与未归属事务（环境准备、独立思想、零散块）")

    # 逐轮归堆：本域块（hit）与该轮的用户块（users，随命中轮一起进）。
    per_seq: dict[int, dict] = {}
    for bid, owner in owners.items():
        item = index.get(bid)
        if item is None:
            continue
        try:
            seq = int(item["seq"] or 0)
        except (TypeError, ValueError):
            seq = 0
        rec = per_seq.setdefault(seq, {"round": item["round"], "hit": [], "users": []})
        rec["round"] = item["round"]
        if str(item["block"].get("kind") or "") == "user":
            rec["users"].append((str(bid), item))
        elif owner == name:
            rec["hit"].append((str(bid), item))

    history: list[str] = []
    own_ids: list[str] = []
    hit_rounds = 0
    for seq in sorted(per_seq):
        rec = per_seq[seq]
        if not rec["hit"]:
            continue  # 非本视图的轮：整轮不出现
        hit_rounds += 1
        lines = _round_lines(rec["round"])
        for bid, item in sorted(rec["users"], key=lambda x: _bnum(x[0])):
            lines.append(_terse_block("▸ ", bid, item, ledger))
            own_ids.append(bid)
        for bid, item in sorted(rec["hit"], key=lambda x: _bnum(x[0])):
            lines.append(_terse_block("▸ ", bid, item, ledger))
            own_ids.append(bid)
        history.extend(lines)

    # 账本：子域按自己的 file_domains 切片；主 agent 拿**不属于任何域**的那
    # 一份（它的 F 就是"别人的文件"的补集）。全局节两者都拿（slice_state 保证）。
    entries = _file_domain_entries(domains)
    if state is None:
        sharded = {}
    elif node is None:
        sharded = slice_state(state, [p for p, _n in entries], exclude=True)
    else:
        sharded = slice_state(state, node.get("file_domains") or [])

    text_lines = card + ["", "[本视图历史]（命中轮的用户原文 + 本域块）"] + (
        history or ["（无——本域尚无命中轮）"]
    )
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
        "history": history,
        "own_ids": own_ids,
        "sharded": sharded,
        "counts": {"own": len(own_ids), "rounds": hit_rounds, "total": len(index)},
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
    """块归属完整性对账：每个块**至少**落进一个视图（块 ID 保留可取回）。

    口径更新（2026-09-12，块作筛子）：一个块可以出现在多个视图里——用户块
    随命中轮进每个命中它的视图；因此断言从"恰一处"放宽为"至少一处"，但
    "彻底消失"仍是红色。
    """
    missing = sorted(set(index) - set(owners))
    rendered: set[str] = set()
    for v in views.values():
        rendered.update(str(b) for b in (v.get("own_ids") or []))
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
            f"- {v['path_id']}（{name}）：命中 {c['rounds']} 轮、"
            f"本域 {c['own']} 块、约 {v['est_tokens']:,} tok"
        )
    if not built["completeness"]["ok"]:
        lost = built["completeness"]["missing"] + built["completeness"]["dropped_from_views"]
        lines.append(f"- ⚠ 未归属/未渲染块：{'、'.join(lost[:10])}")
    return lines
