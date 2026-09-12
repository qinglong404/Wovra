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
from .registry import MAIN_AGENT_ID, latest_domains, runtime_stats

# 模型侧注入的账本分片口径（与 `task._MODEL_SIDE_SECTIONS` 对齐）：
# 全局节每轮都进每个视图（目标/现状是"我在干什么"的最小上下文；
# 决策升级与待办实验是**人机协同回路本身**，没有第二个注入点）；
# 分片节按文件域切片——域 A 的"已完成"对域 B 是纯噪声。
GLOBAL_STATE_FIELDS = ("goal", "current_status", "escalations", "experiments")
SHARDED_STATE_FIELDS = ("decisions", "known_issues", "open_questions")

# 分档口径（2026-09-12，与装配同源）：本视图只保留最近 N 批整理的块细节，
# 更早代折叠为「文件名清单 + N 块已折叠」。基准是**本视图自己的**代次水位，
# 不是全局最新代次——否则别的域一整理，本视图字节就跟着漂、冻结性失效
# （plan §2/§5.1）。未整理的轮永不折叠（分辨率损失只允许来自整理）。
FOLD_KEEP_GENERATIONS = 3


def round_generation(r: dict) -> int:
    """轮的整理代次（`done` 才有；未整理返回 0）。

    与装配端同口径：已整理但无 `org_generation` 的旧轮视为第 1 代
    （最老、优先折叠）——两处判据必须一致，否则同一轮的档位会打架。
    """
    r = r or {}
    if str(r.get("org_state") or "") != "done":
        return 0
    try:
        return int(r.get("org_generation") or 1)
    except (TypeError, ValueError):
        return 1


def view_keep_min(rounds: Iterable[dict]) -> Optional[int]:
    """本视图自己的分档基准：低于它的已整理轮折叠；无已整理轮时 None。

    只喂**本视图命中的轮**——这就是"按视图各自计量"的落点。
    """
    gens = [g for g in (round_generation(r) for r in rounds) if g > 0]
    if not gens:
        return None
    return max(gens) - (FOLD_KEEP_GENERATIONS - 1)


def is_folded(r: dict, keep_min: Optional[int]) -> bool:
    """该轮在本视图里是否走折叠档（未整理 / 无基准 → 永不折叠）。"""
    gen = round_generation(r)
    if gen <= 0 or keep_min is None:
        return False
    return gen < keep_min


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

    判据四条（按序）：
    1. **环境块 → 主 agent**（用户口径：环境准备不是任何域的活）；
    2. **文件块**：`file` 落在某域 `file_domains` 下即归它（最长前缀优先）；
    3. **保底块（纯聊天/结论）→ 跟本轮 `active_view` 走**（渐近归属，
       2026-09-12 用户口径）：维护窗口内到达的轮次补判归域之后，它的聊天
       块随轮一起进那个域的视图——"只把上下文归过去"；
    4. 其余（用户块、不属于任何域的文件块）→ 主 agent（unassigned 残留桶）。

    用户块虽然记在主 agent 名下，但**渲染时随命中轮进域视图**（R68 口径：
    用户块和最上面的用户输入规则一样）——见 `view_for_domain` 的逐轮归堆。

    注意：产物里的 `block_ids` 不参与判据（v2 命名式指派的遗留）；判据只有
    文件集合 + 轮上的视图标记，这样重组与分裂两边共用同一套口径。
    """
    domains = [d for d in (domains or []) if isinstance(d, dict) and d.get("name")]
    entries = _file_domain_entries(domains)
    out: dict[str, str] = {}
    for bid, item in index.items():
        block = item["block"]
        kind = str(block.get("kind") or "")
        if kind == "environment":
            out[bid] = MAIN_AGENT_ID
            continue
        path = str(block.get("file") or "")
        name = _match_domain(path, entries) if path else None
        if name:
            out[bid] = name
            continue
        if kind == "fallback":
            round_view = str((item.get("round") or {}).get("active_view") or "")
            out[bid] = round_view or MAIN_AGENT_ID
            continue
        out[bid] = MAIN_AGENT_ID
    return out


def coverage_gap(
    domains: Iterable[dict] | None,
    rounds: Iterable[dict] | None = None,
    index: Optional[dict[str, dict]] = None,
    owners: Optional[dict[str, str]] = None,
) -> dict:
    """分裂的**覆盖缺口**事实（零 LLM）——喂回分裂分析的硬数据（2026-09-12）。

    动机（worklog §40.3 / §44.3-5）：主 agent 桶里若"未覆盖文件"占大头，涨的
    不是主 agent 的定位，而是**分裂的覆盖不全**；这是可机械发现的信号，故必须
    显式打出来让模型自纠。本函数只给事实，判定仍归模型：

    * `uncovered`：有文件、却不落在任何域 `file_domains` 下的文件路径（它们只能
      掉进主 agent 兜底桶）。同一文件在更早的产物里可能有归属，最近一批没再
      声明 → 这里如实列出当前缺口。
    * `overlapped_rounds`：被 **≥2 个非主 agent 域**命中的轮（`A-1` 与 `A-1-1`
      这类父子重叠、或多域共命同一批轮的直接信号——共命越多，路由越容易横跳，
      粘滞率越低）。父域与子域同时命中是**语义重叠**，不是因为代码文件重叠。
    """
    rounds = [r for r in (rounds or []) if isinstance(r, dict)]
    index = index if index is not None else block_index(rounds)
    owners = owners if owners is not None else ownership(domains, index)
    entries = _file_domain_entries(domains)
    uncovered: list[str] = []
    for bid, item in index.items():
        block = item["block"]
        if str(block.get("kind") or "") != "file":
            continue
        path = str(block.get("file") or "")
        if not path or _match_domain(path, entries):
            continue
        if owners.get(bid) != MAIN_AGENT_ID:
            continue  # 已被某域接管（例如整轮补判归域），不算缺口
        if path not in uncovered:
            uncovered.append(path)
    by_round: dict[int, set[str]] = {}
    for bid, owner in owners.items():
        if owner == MAIN_AGENT_ID:
            continue
        item = index.get(bid)
        if item is None:
            continue
        try:
            seq = int(item["seq"] or 0)
        except (TypeError, ValueError):
            continue
        by_round.setdefault(seq, set()).add(str(owner))
    overlapped = sorted(seq for seq, names in by_round.items() if len(names) >= 2)
    return {
        "uncovered": sorted(uncovered),
        "overlapped_rounds": overlapped,
        "round_domain_counts": {seq: len(n) for seq, n in by_round.items()},
    }


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


def _file_lines_and_count(pairs: Iterable[tuple[str, dict]], ledger) -> list[str]:
    """折叠档的块行：文件名清单 + 块 ID + 「N 块已折叠」（与装配二级折叠同口径）。

    折叠只去块**描述**——膨胀来源就是描述（实测折叠档 80% 体量是块摘要）；
    **块 ID 保留**（R68 用户口径：「留，不然如何取回原文，展开工具就没有用了」）：
    它是 `expand_history` 定位块原文的锚，且体量极小。已删文件标「已删」但
    仍列出——清单是"这轮碰过什么"的索引，比整块消失好。
    """
    files: list[str] = []
    ids: list[str] = []
    for bid, item in pairs or []:
        if item is None:
            continue
        ids.append(str(bid))
        b = (item or {}).get("block") or {}
        f = str(b.get("file") or "")
        if not f:
            wrote = b.get("wrote_files") or []
            f = str(wrote[0]) if wrote else ""
        if f and f not in files:
            files.append(f)
    lines: list[str] = []
    if files:
        parts = [
            f"{f}（已删）" if ledger is not None and ledger.state_of(f) == "dead" else f
            for f in files
        ]
        lines.append("涉及文件：" + "、".join(parts))
    if ids:
        shown = "、".join(sorted(ids, key=_bnum)[:12])
        more = "…" if len(ids) > 12 else ""
        lines.append(
            f"（{len(ids)} 块细节已折叠：{shown}{more}——"
            "块 ID 保留，按轮号或块 ID expand_history 可展开本轮）"
        )
    return lines


def folded_block_lines(
    r: dict,
    rec: dict,
    index: Optional[dict[str, dict]] = None,
    ledger=None,
) -> list[str]:
    """装配侧：本视图里某一折叠轮的块行（装配的 rec 用 own_ids/user_ids 形态）。

    用户块**不折叠**——它与轮头用户原文同规则（用户输入是输入，不是细节）；
    折叠只针对本域块**描述**（那才是会单调膨胀的部分），块 ID 一律保留
    （R68 口径：没有块 ID，`expand_history` 就取不回原文）。
    """
    idx = index or {}
    pairs = [
        (str(bid), idx.get(str(bid)))
        for bid in (rec.get("own_ids") or [])
        if idx.get(str(bid)) is not None
    ]
    return _file_lines_and_count(pairs, ledger)


def _bnum(bid: str) -> int:
    """块号（排序键；解析不出来当 0）。"""
    try:
        return int(str(bid).rsplit("-B", 1)[-1])
    except ValueError:
        return 0


def view_blocks_by_round(
    rounds: Iterable[dict],
    domains: Iterable[dict] | None,
    view_name: str,
    index: Optional[dict[str, dict]] = None,
    owners: Optional[dict[str, str]] = None,
) -> dict[int, dict]:
    """某视图的「命中轮」归堆（供装配用；与 `view_for_domain` 同一套筛法）。

    这是「块作筛子、轮作单位」的可复用形态：返回
    `{seq: {"round", "own_ids", "user_ids"}}`，**只含命中轮**——一块都没
    命中的轮不进返回（整轮不出现，不留占位）。装配按轮取本域块 ID 渲染，
    故与 `view_for_domain` 的文字形态不可能各说各话（同一判据、同一 index）。

    **主 agent 也走这一套**（2026-09-12 用户拍板：主 agent 只吃自己那份料）：
    它的桶就是**补集**——环境块 + 用户块 + 保底块 + 不属任何域的文件块，
    口径与 `view_for_domain` 逐字一致（同一 `ownership`，同一 index），故
    它同样只填 `own_ids`（用户块本来就归它，不必再随命中轮补一次）。
    """
    rounds = [r for r in (rounds or []) if isinstance(r, dict)]
    index = index if index is not None else block_index(rounds)
    owners = owners if owners is not None else ownership(domains, index)
    main = str(view_name) == MAIN_AGENT_ID
    out: dict[int, dict] = {}
    for bid, owner in owners.items():
        item = index.get(bid)
        if item is None or owner != view_name:
            continue
        # 用户块一律走下面第二遍（渲染时挂在轮头，与用户原文同位置）；
        # 若在这里也收进 own_ids，主 agent 视图会把同一块渲染两遍。
        if str(item["block"].get("kind") or "") == "user":
            continue
        try:
            seq = int(item["seq"] or 0)
        except (TypeError, ValueError):
            seq = 0
        rec = out.setdefault(
            seq, {"round": item["round"], "own_ids": [], "user_ids": []}
        )
        rec["round"] = item["round"]
        rec["own_ids"].append(str(bid))
    # 用户块随命中轮进（口径：与轮头用户原文同规则）——故第二遍扫块类型，
    # 只把**已在命中轮**里的用户块收进来（非命中轮仍然整轮不出现）。
    # 主 agent 同理：只有它名下有块的那几轮，轮头用户原文才留下来。
    for bid, item in index.items():
        if str(item["block"].get("kind") or "") != "user":
            continue
        try:
            seq = int(item["seq"] or 0)
        except (TypeError, ValueError):
            seq = 0
        rec = out.get(seq)
        if rec is not None:
            rec["user_ids"].append(str(bid))
    for rec in out.values():
        rec["own_ids"].sort(key=_bnum)
        rec["user_ids"].sort(key=_bnum)
    return out


def files_by_domain(
    rounds: Iterable[dict],
    domains: Iterable[dict] | None,
    index: Optional[dict[str, dict]] = None,
    owners: Optional[dict[str, str]] = None,
) -> dict[str, list[str]]:
    """域名 → 该域名下**真实出现过**的文件（机械统计，零 LLM）。

    用途：路由的文件命中判据。职责表里的 `file_domains` 常写目录
    （`src/wovra/tools/`），而用户提问常只提文件名（「safety.py 加个白名单」）
    ——把材料里真实归属过该域的文件收进来，路由才不至于只有写全路径时才命中。
    """
    rounds = [r for r in (rounds or []) if isinstance(r, dict)]
    index = index if index is not None else block_index(rounds)
    owners = owners if owners is not None else ownership(domains, index)
    out: dict[str, list[str]] = {}
    for bid, owner in owners.items():
        item = index.get(bid)
        if item is None:
            continue
        path = str(item["block"].get("file") or "")
        if not path:
            continue
        bucket = out.setdefault(str(owner), [])
        if path not in bucket:
            bucket.append(path)
    return out


def view_watermarks(
    rounds: Iterable[dict],
    state=None,
    domains: Iterable[dict] | None = None,
    registry: Iterable[dict] | None = None,
    watermark: int | None = None,
) -> dict[str, dict]:
    """每个视图自身的材料体量与「是否到自己的水位」（逐层分裂的机械判据）。

    水位口径（plan §13.1 + 保护机制）：**分裂后水位按 agent 各自计量**——故
    子视图到达自己的水位时，应按同一套机制在它内部再裂一层（`A-1` → `A-1-1`），
    终态是「只操作单个文件为止」。

    产物每项：`tokens`（该视图材料体量，与 `human_report` 同口径）、`blocks`、
    `rounds`（该域活跃轮数＝命中轮数，同时是经济判据里 `N_future` 的保守下界）、
    `over`（是否已达自身水位）。

    口径提醒（2026-09-12 **订正**）：`tokens` 是该视图**全部块的一行式重建**，
    已按**本视图自己的代次水位**分档（最近 3 批全分辨率、更早代折叠为文件名
    清单）。此前注释称它"属上界"——**实测是下界，且差得不小**：同一会话同一域，
    材料口径 5,144 tok 而装配口径（块内事件全文）90,979 tok，差 17.7×
    （worklog §44.3-2，仪器 `scripts/diag_view_assembly_buckets.py`）。
    故：
    * 本函数的 `tokens` / `over` 是**材料口径**，只可用于"材料在长没长"；
    * 凡与经济判据 `(B − B′) × N − C`、水位（`over`）比较的口径，必须与 `B`
      同源（`B` = `last_context_estimate` = **装配口径**）——故运行时用
      `Agent._view_assembly_watermarks()`（它在本函数结果上覆写为装配口径），
      不要直接拿本函数的 `tokens` 进算式（2026-09-12 之前正是这么混用的，
      结果同一份材料给出方向相反的两个结论：worklog §44.3-3）。
    仪器（`scripts/maint_health.py`）会临时关掉折叠再派生一次做 A/B——同一份
    材料两次派生，跨会话比数没有意义（域树与轮数都会变）。
    """
    built = build_views(rounds, state, domains=domains, registry=registry)
    out: dict[str, dict] = {}
    for name, view in (built.get("views") or {}).items():
        counts = view.get("counts") or {}
        tokens_ = int(view.get("est_tokens") or 0)
        out[str(name)] = {
            "tokens": tokens_,
            "blocks": int(counts.get("own") or 0),
            "rounds": int(counts.get("rounds") or 0),
            # `None` = 调用方没给水位（不判）；数字（含 0）都是合法阈值。
            # 曾用 bool(watermark) 判，把 0 当成"未给"——而 0 在测试/调试里
            # 是"全都算到线"的常用口径，会把判据静默关掉（实测踩到）。
            "over": (
                watermark is not None and tokens_ >= int(watermark)
            ),
        }
    return out


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
    folded_rounds = 0
    # 分档基准取自**本视图命中轮**（不是全局最新代次）——见 view_keep_min。
    keep_min = view_keep_min(rec["round"] for rec in per_seq.values() if rec.get("hit"))
    for seq in sorted(per_seq):
        rec = per_seq[seq]
        if not rec["hit"]:
            continue  # 非本视图的轮：整轮不出现
        hit_rounds += 1
        lines = _round_lines(rec["round"])
        # 用户块**不折叠**（与轮头用户原文同规则）；只折本域块描述。
        for bid, item in sorted(rec["users"], key=lambda x: _bnum(x[0])):
            lines.append(_terse_block("▸ ", bid, item, ledger))
        if is_folded(rec["round"], keep_min):
            folded_rounds += 1
            lines += _file_lines_and_count(rec["hit"], ledger)
        else:
            for bid, item in sorted(rec["hit"], key=lambda x: _bnum(x[0])):
                lines.append(_terse_block("▸ ", bid, item, ledger))
        # 块 ID 恒进 own_ids（完整性对账用）：折叠只改**渲染文本**，
        # 不改归属——块仍有归宿，仍可按轮号 expand_history 取回原文。
        own_ids.extend(
            bid for bid, _item in sorted(rec["users"], key=lambda x: _bnum(x[0]))
        )
        own_ids.extend(
            bid for bid, _item in sorted(rec["hit"], key=lambda x: _bnum(x[0]))
        )
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

    text_lines = card + [
        "",
        "[本视图历史]（命中轮的用户原文 + 本域块；"
        f"最近 {FOLD_KEEP_GENERATIONS} 批整理全分辨率，更早代折叠）",
    ] + (history or ["（无——本域尚无命中轮）"])
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


def human_report(built: dict, registry: list | None = None) -> list[str]:
    """域视图的人视图摘要（零 LLM；`wovra report` / `wovra maint` 共用）。

    `registry` 给了就带上各 agent 的**运行时账**（3a：轮次/步数/上下文占比）
    ——"每个子 agent 有自己的轮次、步数、窗口"必须看得见；历史会话没这本账
    时留空（不编数）。
    """
    if not built or not built.get("views"):
        return []
    runtime = runtime_stats(registry)
    lines = [
        "## 域视图（Level 1 第二步：装配按域分化的材料）",
        "",
        f"- 块总数 {built['completeness']['total']}，"
        f"归属完整：{'是' if built['completeness']['ok'] else '否'}",
    ]
    for name, v in built["views"].items():
        c = v["counts"]
        line = (
            f"- {v['path_id']}（{name}）：命中 {c['rounds']} 轮、"
            f"本域 {c['own']} 块、约 {v['est_tokens']:,} tok"
        )
        stat = runtime.get(name) or {}
        if stat.get("rounds") or stat.get("steps"):
            line += f"｜agent 账：轮 {stat['rounds']}／步 {stat['steps']}"
            if stat.get("handoffs"):
                line += f"／转出 {stat['handoffs']}"
            if stat.get("window"):
                line += f"／上下文 {stat['ctx_cur']:,}（{stat['share']:.0%}）"
        lines.append(line)
    if not built["completeness"]["ok"]:
        lost = built["completeness"]["missing"] + built["completeness"]["dropped_from_views"]
        lines.append(f"- ⚠ 未归属/未渲染块：{'、'.join(lost[:10])}")
    return lines
