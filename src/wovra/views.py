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
import json
from typing import Iterable, Optional

from . import blocks as blocks_module
from . import tokens as tokens
from .registry import MAIN_AGENT_ID, build_entries, latest_domains

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


# ---- per-agent 运行时账（派生，不落盘）---------------------------------------

def _tool_call_names(message: dict) -> list[str]:
    return [
        str((c.get("function") or {}).get("name") or "")
        for c in (message.get("tool_calls") or [])
        if isinstance(c, dict)
    ]


def event_owners(r: dict, lookup: Optional[dict] = None) -> list[str]:
    """轮内**逐事件的执行方**（与 `round_step_segments` 同一套判据，一处实现）。

    换手点在 `route_to` 的**工具调用事件之后**（调用者执行了那次调用，故调用
    自身仍归上一手）；`switch_view`/`notify` 只管下一轮，不在本规则内。
    消费方：对话页逐事件的 agent 标签（`serve.round_detail`）与步数分段。
    """
    lookup = lookup or {}
    cur = str(lookup.get(str(r.get("route_explicit") or "").strip()) or MAIN_AGENT_ID)
    out: list[str] = []
    for event in r.get("events") or []:
        out.append(cur)
        if not isinstance(event, dict):
            continue
        message = event.get("message") or {}
        if str(message.get("role") or "") != "assistant":
            continue
        if _tool_call_names(message) != ["route_to"]:
            continue
        for call in message.get("tool_calls") or []:
            arguments = (call.get("function") or {}).get("arguments") or "{}"
            try:
                target = str((json.loads(arguments) or {}).get("agent") or "")
            except (TypeError, ValueError):
                target = ""
            # 查表认名字与 ID 两种写法；查不到就**照原名认账**（模型明确说了
            # 转给谁，不该因为名册里没有就静默算在上一手头上）
            if target:
                cur = str(lookup.get(target) or target)
    return out


def round_step_segments(r: dict, lookup: Optional[dict] = None) -> list[tuple[str, int]]:
    """一轮里「谁执行了几步」的分段（走事件流，零 LLM）。

    * **步** = 一条 `assistant` 事件（`tool_call` 或 `final_answer`），也就是
      一次模型往返——与轮上累计的 `steps_used` 是同一件东西（空响应重试
      不产生事件，故这里只会略少、不会多）。
    * **转交点** = 事件流里那个 `route_to` 工具调用事件：调用者执行了它，
      从下一条事件起归接手方。**所以轮上不必另存交接锚点**——锚点本来就在
      事件流里，而且这样天然支持一轮内多次转交（`route_hops` 上限之内）；
      历史轮同样能算。
    * **起点** = 显式转交（`switch_view`/`notify` 写下的意志）的轮由目标起手，
      其余一律主 agent 起手（2026-09-12 用户口径：每轮恒由主 agent 先触发）。

    归的是**执行步**（谁花的手）；**轮**的归属是另一件事——按落点（轮上的
    `active_view`），见 `agent_ledger`。
    """
    owners = event_owners(r, lookup)
    segs: list[tuple[str, int]] = []
    for owner, event in zip(owners, r.get("events") or []):
        if not isinstance(event, dict):
            continue
        message = event.get("message") or {}
        if str(message.get("role") or "") != "assistant":
            continue
        if segs and segs[-1][0] == owner:
            segs[-1] = (owner, segs[-1][1] + 1)
        else:
            segs.append((owner, 1))
    return segs


def answer_view(r: dict, lookup: Optional[dict] = None) -> str:
    """一轮**最后由谁在干活**（事件流分段里的最后一段）。"""
    segs = round_step_segments(r, lookup)
    if segs:
        return segs[-1][0]
    return str(lookup.get(str(r.get("route_explicit") or "").strip()) or MAIN_AGENT_ID)


def _ledger_roster(
    domains: Iterable[dict] | None, registry: Iterable[dict] | None
) -> tuple[list[dict], dict[str, str]]:
    """agent 名册 + 「视图串 → 名册名」查表（名字与 ID 两种写法都认）。

    主 agent 的名册名恒为哨兵 ID `Main`（机器键要稳定，别随注册表里的显示名
    漂移——历史数据里 `active_view` 存的一直是哨兵）；它的显示名另放
    `display`（注册表里通常叫「主agent」）。查表把哨兵与显示名都指向 `Main`。
    """
    roster: list[dict] = []
    lookup: dict[str, str] = {}
    main_display = MAIN_AGENT_ID
    for e in registry or []:
        if isinstance(e, dict) and str(e.get("id") or "") == MAIN_AGENT_ID:
            main_display = str(e.get("name") or main_display)
    roster.append({
        "id": MAIN_AGENT_ID, "name": MAIN_AGENT_ID,
        "view": MAIN_AGENT_ID, "display": main_display,
    })
    lookup[MAIN_AGENT_ID] = MAIN_AGENT_ID
    lookup[main_display] = MAIN_AGENT_ID
    by_name = {
        str(e.get("name")): str(e.get("id") or "")
        for e in registry or []
        if isinstance(e, dict) and e.get("name")
    }
    if not by_name:
        # 注册表没给（离线检视/尚未 promote）：用同一套机械翻译就地派生路径 ID
        # ——账本的 ID 必须与视图表、人视图一致，否则同一个域在两处显示成两个
        # 身份（`build_views` 同一个坑，同一个解法）。
        by_name = {str(e["name"]): str(e["id"]) for e in build_entries(domains)}
    for d in domains or []:
        if not isinstance(d, dict) or not d.get("name"):
            continue
        name = str(d["name"])
        if name in lookup:
            continue
        aid = by_name.get(name, name)
        roster.append({"id": aid, "name": name, "view": name, "display": name})
        lookup[name] = name
        if aid:
            lookup[aid] = name
    return roster, lookup


def agent_lookup(
    domains: Iterable[dict] | None = None, registry: Iterable[dict] | None = None
) -> dict[str, str]:
    """「视图串（域名或 ID）→ 名册名」查表——消费方复用同一套归属判据。"""
    return _ledger_roster(domains, registry)[1]


def _round_generation_of(r: dict) -> int:
    try:
        return int(r.get("org_generation") or 0)
    except (TypeError, ValueError):
        return 0


def stage_plan(rounds: Iterable[dict] | None) -> dict:
    """阶段划分的两个机械信号（供 `stage_index` 用）。

    * `boundaries`：**产出域树的代次**（有代次的载体轮）——某代 g 的轮是产出
      那棵树**之前**产生的，故其阶段 = `#{b : b < g}`；
    * `carriers`：**域树载体轮的 seq**（域树落盘在产出它的批次首轮上）。载体轮
      自己就是那棵树的来源，必然处于它生效**之前**；它的代次可能缺失（未整理
      就落盘的批次），故单独认。
    """
    rounds = [r for r in (rounds or []) if isinstance(r, dict)]
    gens: set[int] = set()
    carriers: list[int] = []
    for r in rounds:
        if not (r.get("domains") or []):
            continue
        try:
            seq = int(r.get("seq") or 0)
        except (TypeError, ValueError):
            continue
        carriers.append(seq)
        g = _round_generation_of(r)
        if g > 0:
            gens.add(g)
    return {"boundaries": sorted(gens), "carriers": sorted(carriers)}


def stage_boundaries(rounds: Iterable[dict] | None) -> list[int]:
    """**分裂生效点**的整理代次（升序）——`stage_plan` 的薄封装（旧调用点）。"""
    return stage_plan(rounds)["boundaries"]


def stage_index(
    r: dict, plan: Iterable[int] | dict | None
) -> int:
    """该轮属于哪个阶段：0 = **分裂前**，i≥1 = 第 i 次分裂生效之后。

    * 域树载体轮（产出某棵树的那一批的首轮）处于那棵树生效之前——它在阶段
      `该树序号`（第一棵树的载体轮 = 阶段 0 = 分裂前）；
    * 有代次的轮：阶段 = `#{生效点 b : b < 代次}`；
    * 未整理的轮（没有代次）属于**当前阶段**（最后一次分裂之后）。
    """
    if isinstance(plan, dict):
        carriers = list(plan.get("carriers") or [])
        boundaries = list(plan.get("boundaries") or [])
    else:                                  # 兼容旧签名（只给 boundaries）
        carriers, boundaries = [], list(plan or [])
    try:
        seq = int(r.get("seq") or 0)
    except (TypeError, ValueError):
        seq = 0
    if seq and seq in carriers:
        return carriers.index(seq)
    g = _round_generation_of(r)
    if g > 0:
        return sum(1 for b in boundaries if b < g)
    return len(boundaries) if boundaries else (1 if carriers else 0)


def stage_spans(
    rounds: Iterable[dict] | None, domains: Iterable[dict] | None = None
) -> list[dict]:
    """按**阶段**给轮分组（用户口径：显示按阶段分，不是笼统一坨）。

    * 阶段 0「**分裂前**」：第一次分裂生效之前产生的轮——那时还没有子 agent
      可分，故整段记一个数、**不归任何 agent**（也就不用谈"主 agent 名下 13 轮"）。
    * 阶段 i≥1「第 i 次分裂后」：该阶段内的轮按**落点**归 agent。

    恒等式仍然成立：Σ各阶段轮数 = 总轮数（= 最大 R 号）。
    """
    rounds = [r for r in (rounds or []) if isinstance(r, dict)]
    plan = stage_plan(rounds)
    stages: dict[int, dict] = {}
    for r in rounds:
        try:
            seq = int(r.get("seq") or 0)
        except (TypeError, ValueError):
            continue
        idx = stage_index(r, plan)
        st = stages.setdefault(idx, {
            "stage": idx,
            "label": "分裂前" if idx == 0 else f"第 {idx} 次分裂后",
            "seqs": [], "by_agent": {}, "unassigned": [], "unattributed": [],
        })
        st["seqs"].append(seq)
        landing = str(r.get("active_view") or "").strip()
        if idx == 0:
            # 分裂前的轮**不归任何 agent**（那时还没有子 agent 可分）
            st["unattributed"].append(seq)
        elif not landing:
            # 分裂之后产生、但没有落点的轮（机制生效前闭合的老轮）
            st["unassigned"].append(seq)
        else:
            st["by_agent"].setdefault(landing, []).append(seq)
    out: list[dict] = []
    for idx in sorted(stages):
        st = stages[idx]
        st["seqs"].sort()
        for v in st["by_agent"].values():
            v.sort()
        st["unassigned"].sort()
        st["unattributed"].sort()
        st["rounds"] = len(st["seqs"])
        st["first"] = st["seqs"][0] if st["seqs"] else 0
        st["last"] = st["seqs"][-1] if st["seqs"] else 0
        st["by_agent_counts"] = {k: len(v) for k, v in st["by_agent"].items()}
        out.append(st)
    return out


def round_account(
    rounds: Iterable[dict] | None,
    domains: Iterable[dict] | None = None,
    registry: Iterable[dict] | None = None,
) -> dict:
    """会话级轮账：总轮数 = 最大 R 号 = 各阶段轮数之和（按阶段分组）。

    分组口径见 `stage_spans`：分裂前的轮整段记（不归 agent），分裂后的轮按
    落点归 agent。**只记新轮**——被压缩掉的旧轮不进 per-agent 活账。
    """
    rounds = [r for r in (rounds or []) if isinstance(r, dict)]
    stages = stage_spans(rounds, domains)
    ledger = agent_ledger(rounds, domains, registry)
    live = 0
    seen: set[int] = set()
    for key, rec in ledger.items():
        if key == "__unassigned__" or id(rec) in seen:
            continue
        seen.add(id(rec))
        live += int(rec.get("rounds") or 0)
    presplit = next((s for s in stages if s["stage"] == 0), None)
    total = max((int(r.get("seq") or 0) for r in rounds), default=0)
    counted = sum(int(s["rounds"]) for s in stages)
    return {
        "total": total,
        "stages": stages,
        "presplit": int((presplit or {}).get("rounds") or 0),
        "presplit_span": presplit or {},
        "compressed": int((presplit or {}).get("rounds") or 0),   # 兼容旧名
        "attributed": live,
        "unassigned": int((ledger.get("__unassigned__") or {}).get("rounds") or 0),
        "balanced": counted == total,
    }


def agent_ledger(
    rounds: Iterable[dict] | None,
    domains: Iterable[dict] | None = None,
    registry: Iterable[dict] | None = None,
) -> dict[str, dict]:
    """每个 agent 名下的**分裂后活轮**（按落点）+ 步（按执行者）——派生，不落盘。

    口径（2026-09-12 用户拍板，worklog §56/§58）——**轮数统一，按阶段分**：

    * **轮**：`R{n}` 是会话级唯一序列，只增、不重编。**分裂前**那一段整段记、
      不归任何 agent（那时还没有子 agent 可分——"主 agent 名下 13 轮"是错的说法，
      那是分裂前的 13 轮）；**分裂之后**产生的轮，一轮恰好归一个**落点**
      （"它被附加到哪个 agent 的上下文"= 轮上的 `active_view`），实时落定、
      **只记新轮**、不回算旧轮。阶段划分见 `stage_spans`。
    * **步**：一次模型调用 = 一步，归**执行它的那个 agent**（同一轮里可有
      多家：主 agent 走路由那一步算它的，接手方走的算接手方的）。轮的总步
      = 各执行方之和。步数按事件流里的 `route_to` 转交点分段，历史轮同样能算；
      该轮的步**不因后来被压缩而改**（谁花的手是事实）。
    * **钱**：不在这里——每次调用在 `core._accumulate_usage` 就按执行方落账
      （`by=` 段），轮的总消费 = 各调用方之和；整理/压缩/分裂开销单列运行时账。

    对外显示一律用**总轮（R 号）**：`seqs` 就是这个 agent 名下的号，
    `first`/`last` 给"计数 + 首末范围"用（号多了不至于把页面撑爆），
    点开列具体号——**号本身就是展开入口**，显示层与展开层同一套编号。

    `ctx_cur` / `ctx_peak` / `window` 是**观测**不是投影，仍从注册表条目读取
    （"最近装配多大 / 历史峰值 / 窗口"是运行时事实，且峰值单调、不该下调）。
    """
    rounds = [r for r in (rounds or []) if isinstance(r, dict)]
    domains = list(domains) if domains is not None else latest_domains(rounds)
    roster, lookup = _ledger_roster(domains, registry)
    plan = stage_plan(rounds)

    def blank(entry: dict) -> dict:
        return {
            "id": entry["id"],
            "name": entry["name"],
            "display": entry["display"],
            "rounds": 0,
            "seqs": [],
            "first": 0,
            "last": 0,
            "steps": 0,
            "handoffs": 0,
        }

    by_view = {entry["view"]: blank(entry) for entry in roster}
    by_name = {entry["name"]: by_view[entry["view"]] for entry in roster}
    unassigned: list[int] = []

    def rec_for(name: str) -> dict:
        """取名册条目；名册里没有（产物未入册/名字写错）就照原名立一格。

        宁可多长一行，也不让"确实有人干过的活"因为不在名册里被静默丢掉。
        """
        rec = by_name.get(name)
        if rec is None:
            rec = blank({"id": name, "name": name, "display": name})
            by_view[name] = rec
            by_name[name] = rec
        return rec

    for r in rounds:
        try:
            seq = int(r.get("seq") or 0)
        except (TypeError, ValueError):
            seq = 0
        segs = round_step_segments(r, lookup)
        # 步：按执行者（一轮里可以多家）——**不论该轮是否已被压缩**都记，
        # 因为"谁花的手"是发生过的事实，不会因为后来被压缩而改变。
        for name, count in segs:
            rec_for(name)["steps"] += count
        # 转出记给**转出方**（每一次换手算一次；一轮可转多次）
        for name, _count in segs[:-1]:
            rec_for(name)["handoffs"] += 1
        # 轮：只算**分裂之后**的活轮。分裂前那一段整段记（那时还没有子 agent
        # 可分，谈不上"主 agent 名下 13 轮"，见 `stage_spans`）；分裂之后但
        # 没有落点的（机制生效前闭合的老轮）另记，不硬塞给主 agent 充数。
        if stage_index(r, plan) == 0:
            continue
        landing = str(r.get("active_view") or "").strip()
        if not landing:
            unassigned.append(seq)
            continue
        rec_for(str(lookup.get(landing) or landing))["seqs"].append(seq)

    for rec in by_view.values():
        rec["seqs"].sort()
        rec["rounds"] = len(rec["seqs"])
        rec["first"] = rec["seqs"][0] if rec["seqs"] else 0
        rec["last"] = rec["seqs"][-1] if rec["seqs"] else 0

    # 观测字段（存的是事实，不是投影）+ 双键
    seen = {
        str(e.get("id") or ""): e
        for e in registry or []
        if isinstance(e, dict) and e.get("id")
    }
    obs = {}
    for e in registry or []:
        if isinstance(e, dict) and e.get("name"):
            obs[str(e["name"])] = e
    out: dict[str, dict] = {}
    for entry in roster:
        rec = by_view[entry["view"]]
        source = seen.get(str(entry["id"])) or obs.get(entry["name"]) or {}
        window = int(source.get("window") or 0)
        cur = int(source.get("ctx_cur") or 0)
        rec["ctx_cur"] = cur
        rec["ctx_peak"] = int(source.get("ctx_peak") or 0)
        rec["window"] = window
        rec["share"] = (cur / window) if window else 0.0
        out[entry["name"]] = rec
        if entry["id"] and entry["id"] != entry["name"]:
            out[str(entry["id"])] = rec
    out["__unassigned__"] = {
        "id": "", "name": "", "display": "无落点（机制生效前）",
        "rounds": len(unassigned), "seqs": sorted(unassigned),        "first": min(unassigned) if unassigned else 0,
        "last": max(unassigned) if unassigned else 0,
        "steps": 0, "handoffs": 0,
        "ctx_cur": 0, "ctx_peak": 0, "window": 0, "share": 0.0,
    }
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
        # per-agent 派生账（名下 R 号 + 步）+ 会话级轮账（总轮 = 最大 R 号）
        "ledger": agent_ledger(rounds, domains, registry),
        "account": round_account(rounds, domains, registry),
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

    agent 账取自 `built["ledger"]`（派生）：**一律按总轮（R 号）显示**——
    每个 agent 名下是一串 R 号（默认给「计数 + 首末范围」），加上"已压缩段"
    一行。Σ各 agent 轮数 + 已压缩段 = 会话总轮数（恒等式，不是巧合）。
    """
    if not built or not built.get("views"):
        return []
    runtime = built.get("ledger") or {}
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
        if stat:
            line += f"｜名下 {_seq_range(stat)}｜步 {stat['steps']}"
            if stat.get("handoffs"):
                line += f"／转出 {stat['handoffs']}"
            if stat.get("window"):
                line += f"／上下文 {stat['ctx_cur']:,}（{stat['share']:.0%}）"
        lines.append(line)
    if not built["completeness"]["ok"]:
        lost = built["completeness"]["missing"] + built["completeness"]["dropped_from_views"]
        lines.append(f"- ⚠ 未归属/未渲染块：{'、'.join(lost[:10])}")
    return lines


def _seq_range(stat: dict) -> str:
    """「N 轮（R21–R30）」——号多了不逐条列，首末给范围（用户口径）。"""
    n = int(stat.get("rounds") or 0)
    if not n:
        return "0 轮"
    first, last = int(stat.get("first") or 0), int(stat.get("last") or 0)
    if first == last:
        return f"{n} 轮（R{first}）"
    return f"{n} 轮（R{first}–R{last}）"
