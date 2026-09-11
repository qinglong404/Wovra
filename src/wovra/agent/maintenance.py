"""Agent 维护管线：水位批量整理（organization）、分裂分析（split）、
暂存与 promote、baseline 阈值压缩。整理 = 一次追加式对话，分裂是它的
纯追加延续。
"""
import json
import re
import threading
import time
from typing import Optional
from .. import blocks as blocks_module
from .. import lifecycle as lifecycle_module
from .. import registry as registry_module
from .. import tokens as tokens
from .. import truncate as truncate
from .support import (
    MODE_MANAGED,
    _COMPRESS_THRESHOLD,
    _clip_quote,
    maint_tools,
)
from .prompts import (
    _ORG_META_INFO,
    _ORG_FORMAT_DISCIPLINE,
    _ORG_TAG_INSTRUCTIONS,
    _SPLIT_INSTRUCTIONS,
)


_ORG_SUBMIT_TOOL = "submit_organization"

_SPLIT_SUBMIT_TOOL = "submit_domains"

# 带诊断重发的修正要求（见 _MaintenanceMixin._org_repair_messages）。
# 第 2 条直接针对实测根因：长中文叙述里混进未转义 ASCII 双引号会截断
# JSON 串（worklog-20260911.md §11.2）。
_ORG_REPAIR_HINT = (
    "上一条 submit_organization 的参数不是合法 JSON，无法解析，本次提交被拒。\n"
    "诊断：{evidence}\n\n"
    "请重新调用 submit_organization 提交（唯一出口，正文说明不采纳）。要求：\n"
    "1. 参数必须是**单个合法 JSON 对象**：严格双引号、无尾随逗号、无未转义"
    "控制字符，反斜杠只用于 \\\" \\\\ \\/ \\b \\f \\n \\r \\t \\uXXXX 这些合法转义；\n"
    "2. 描述等文本里**不要使用英文双引号**——需要引用时用中文引号「」或“”，"
    "英文双引号会截断 JSON 字符串（本次失败即由此引起）；\n"
    "3. 内容覆盖与上次一致（同样的轮次与块），只修正格式。"
)

# 分裂阶段的同类修正要求（2026-09-11 对称补齐）：submit_domains 同样
# 面临超长截断（指令里明确写了"必须在单次响应内完整输出"），此前失败
# 只留痕、无恢复路径——已知风险却没有第二跳，与 org 不对称。
_SPLIT_REPAIR_HINT = (
    "上一条 submit_domains 的参数不是可用产物（不是合法 JSON，或截断成空壳），"
    "本次分裂分析作废，已保守按不可分处理。\n"
    "诊断：{evidence}\n\n"
    "请重新调用 submit_domains 提交（唯一出口，正文说明不采纳）。要求：\n"
    "1. 参数必须是**单个合法 JSON 对象**：严格双引号、无尾随逗号、无未转义"
    "控制字符；\n"
    "2. 文本里不要使用英文双引号——需要引用时用中文引号「」或“”；\n"
    "3. 内容与上次一致（同样的域与块归属），只修正格式；"
    "参数必须在**单次响应内完整输出**，超长截断会令整个分析作废。"
)

# 轻量 JSON 修复用（见 _MaintenanceMixin._loads_lenient）
_BAD_ESCAPE = re.compile(r"\\(?![\\/\"bfnrtu])")
_TRAILING_COMMA = re.compile(r",(\s*[}\]])")
# 结构引号（串边界/键边界）右侧第一个非空白字符必属于此集合
_QUOTE_CLOSERS = frozenset(":,}]")


def _requote_json(text: str) -> str:
    """给 JSON 串里**未转义的英文双引号**补转义（零 LLM，纯结构判据）。

    实测根因（worklog-20260911.md §11.2）：模型在长中文叙述里直接写英文
    双引号（`于是"读到含'不存在'的文件"整块`），JSON 串提前结束 → 30,740
    字符的整理产物整批作废。

    判据（只看局部上下文，不需要理解语义）：一个引号若是**结构引号**，
    它右侧第一个非空白字符必是 `:`、`,`、`}`、`]`（键结尾或值结尾）；
    否则视为**内容引号**，补 `\\`。`\\` 开头的转义序列整体跳过。

    诚实边界：正文里"引号紧跟逗号"（`他说"好",然后`）会误判为结构引号 →
    修不好 → 解析失败 → 该候选被弃用（**解析就是校验**），退回带诊断重发。
    所以本函数只提高命中率，不承担正确性。
    """
    out: list[str] = []
    i, n = 0, len(text)
    in_string = False
    while i < n:
        ch = text[i]
        if not in_string:
            out.append(ch)
            if ch == '"':
                in_string = True
            i += 1
            continue
        if ch == "\\":                      # 已有转义序列：整体复制
            out.append(text[i:i + 2])
            i += 2
            continue
        if ch == '"':
            j = i + 1
            while j < n and text[j] in " \t\r\n":
                j += 1
            nxt = text[j] if j < n else ""
            if nxt in _QUOTE_CLOSERS or nxt == "":
                out.append('"')             # 结构引号：正常收尾
                in_string = False
            else:
                out.append('\\"')           # 内容引号：补转义
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _strip_trailing_commas(text: str) -> str:
    """去掉尾随逗号（反复到不动点：`[1,,,]` 这类连环要收敛）。"""
    prev = None
    while prev != text:
        prev = text
        text = _TRAILING_COMMA.sub(r"\1", text)
    return text


def _json_candidates(raw: str):
    """按**最小干预优先**给出候选文本，逐个试解析（解析成功即止）。

    顺序即优先级：原样 → 结构引号修复 → 非法转义修复 → 两者叠加 →
    各自再加尾随逗号。先试干预最少的，避免"本来能解析却被改坏"。
    """
    requoted = _requote_json(raw)
    unescaped = _BAD_ESCAPE.sub("", raw)
    base = [
        ("原样", raw),
        ("结构引号修复", requoted),
        ("非法转义修复", unescaped),
        ("引号+转义修复", _requote_json(unescaped)),
    ]
    out = list(base)
    for name, text in base:
        fixed = _strip_trailing_commas(text)
        if fixed != text:
            out.append((name + "＋尾随逗号", fixed))
    return out


class _MaintenanceMixin:
    def _unorganized_rounds(self) -> list[dict]:
        """已闭合且尚未整理完成的轮次（按时间正序）。

        org_state ∈ ""（从未整理）/ "pending"（排队或上次崩溃遗留）/
        "failed"（上次解析失败）都算未整理；整理中（inflight）的不算，
        防止批量排队期间被下一次触发重复收编。
        """
        return [
            r for r in self.rounds
            if r.get("end_state") == "completed"
            and r.get("org_state") != "done"
            and r["seq"] not in self._org_inflight
        ]

    def _maybe_organize_batch(self) -> None:
        """水位检查：当前上下文窗口体量达阈值且本轮已闭合时，批量入队整理。

        水位口径 = last_context_estimate（最近一次装配的估算，轮闭合时
        即本轮峰值）——不是未整理积压量（2026-09-07 用户拍板）。每次轮
        闭合到水位则收编**全部**未整理轮（2026-09-09 用户拍板：批次上限
        删除，触发只看水位；最老的先整理）。产物暂存不直写：本轮装配
        保持原样，下一轮开启才生效（_promote_org_results）；过程对用户
        静默。
        """
        if self.last_context_estimate < self._org_watermark:
            return
        # 保护机制：宽限期（3 轮全豁免）+ 冷却间隔（2026-09-09 用户拍板：
        # 双条件不认可，恢复全豁免——提出 3 轮时已考虑 229 步巨轮，巨轮
        # 在宽限期内同样豁免；代价知情：超水位推迟的整理由冷却后的批次
        # 补上）。窗口保底（紧急折叠）不在豁免范围，是独立的生存线。
        current_seq = self.rounds[-1]["seq"] if self.rounds else 0
        if current_seq <= self._org_grace:
            return  # 宽限期：开头几轮是"解释现状的最小历史"，全豁免
        last_maintained = max(
            (
                r["seq"]
                for r in self.rounds
                if r.get("org_state") == "done"
                or (
                    r.get("org_state") == "pending"
                    and r["seq"] in self._org_inflight
                )
            ),
            default=0,
        )
        if last_maintained and current_seq - last_maintained < self._org_cooldown:
            # 冷却口径只认"真正维护过"：done（完成）或本进程在飞（pending
            # 且 seq ∈ _org_inflight）。崩溃遗留的 pending（上个进程维护
            # 线程被中断的半程状态）不算已维护——否则冷却把它们当刚维护
            # 过，挡掉本次会话第一次补整理（F 组实证：6 轮 pending 续跑，
            # R7/R8 闭合被 8-6=2 < 3 连挡两轮，压缩迟迟不开始）。
            return  # 冷却间隔：两次维护之间的最小轮距，防高频
        unorganized = self._unorganized_rounds()
        if not unorganized:
            return
        # 快照先取、且必须在改 org_state 之前：快照就是"活前缀"，维护调用
        # 原样追加指令，生产环境里这次调用的输入端骑满前缀缓存。
        # 协议不完整（以悬空 tool_calls 收尾）时不取：追加的 user 指令会
        # 成为非法序列，严格端点直接 400。里程碑闭合发生在工具方法体内部
        # 时正是这个形状——推迟到该调用的 tool 结果落盘后补做
        # （core.py::_finish_tool_result 认领 _maint_deferred）。
        snapshot = self._maint_snapshot()
        if snapshot is None:
            self._maint_deferred = True
            if self.task is not None:
                self.task.record(
                    "maintenance",
                    "水位检查推迟：调用方 tool 结果尚未落盘，装配尾部是未回复的 "
                    "tool_calls（追加整理指令会破坏协议）。结果落盘后自动补做。",
                )
            return
        batch = unorganized
        for r in batch:
            r["org_state"] = "pending"
            self._org_inflight.add(r["seq"])
        if self.async_organization:
            self._org_queue.put((batch, snapshot))
            self._ensure_worker()
        else:
            # 同步模式（run 命令/测试）：立即整理，结果随轮次落盘
            try:
                self._parallel_maintenance(batch, snapshot)
            finally:
                for r in batch:
                    self._org_inflight.discard(r["seq"])

    def organize_backlog(self) -> None:
        """立即整理全部未整理轮（同步）——run 模式进程收尾用。

        chat 模式不调用：水位设计允许小会话全程不整理（成本归零）；
        run 是一次性任务单元，退出前补整理，TaskState 才能跟得上
        下一次自主推进（"根据任务状态决定下一步"依赖这本账）。
        """
        if self.context_mode != MODE_MANAGED or self.task is None:
            return
        while True:
            unorganized = self._unorganized_rounds()
            if not unorganized:
                return
            # 收尾整理同走协议闸门：装配不完整（悬空 tool_calls）时宁可不整理，
            # 也不能发出一条必然 400 的请求（run 模式退出路径，轮多已闭合，
            # 正常情况下这里一定是干净快照）
            snapshot = self._maint_snapshot()
            if snapshot is None:
                return
            batch = unorganized
            for r in batch:
                r["org_state"] = "pending"
                self._org_inflight.add(r["seq"])
            try:
                org_ok, _split_ok = self._parallel_maintenance(batch, snapshot)
            except Exception:  # noqa: BLE001——收尾整理失败不阻塞任务退出
                for r in batch:
                    r["org_state"] = "failed"
                self._persist_rounds()
                return
            finally:
                for r in batch:
                    self._org_inflight.discard(r["seq"])
            if not org_ok:
                return

    def _ensure_worker(self) -> None:
        if self._org_thread is not None and self._org_thread.is_alive():
            return
        self._org_thread = threading.Thread(
            target=self._org_worker, name="wovra-organization", daemon=True
        )
        self._org_thread.start()

    def _org_worker(self) -> None:
        while True:
            batch, base_messages = self._org_queue.get()
            try:
                self._parallel_maintenance(batch, base_messages)
            except Exception:  # noqa: BLE001——整理失败不影响主对话
                for r in batch:
                    r.pop("pending_org", None)
                    r["org_state"] = "failed"
                self._persist_rounds()
            finally:
                self._org_queue.task_done()
                for r in batch:
                    self._org_inflight.discard(r["seq"])

    def flush_organization(self, timeout: float = 10.0) -> bool:
        """等待异步整理队列清空（chat 退出限时等待；run 用同步模式无需调用）。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._org_queue.unfinished_tasks == 0:
                return True
            time.sleep(0.1)
        return self._org_queue.unfinished_tasks == 0

    def _block_map_lines(
        self, rounds: list[dict]
    ) -> tuple[dict[int, list[dict]], list[str], list[dict]]:
        """分块地图（v3 按文件分块 + 标签行，零 LLM），供整理/分裂指令用。

        现场重算——不信轮上持久化的 blocks（181052 等旧会话存的是
        kind=work 旧结构）；FileLedger 按批内轮序推演，用每轮开始时的
        版本数判定 创建 vs 重构（与 scripts/file_block_structure.py 的
        build 同流程，标签行即分辨率指令）。

        连续纯聊天轮（单保底块且无工具）合并为一组（2026-09-10 用户
        拍板：组内只输出一个结果）：地图显示 R1-2（2 轮·纯聊天合并），
        合成块 ID R1-2-B1；merged_groups 携带组信息，供 _stage_org_state
        落地时广播到组内每轮。
        """
        ledger = lifecycle_module.FileLedger()
        round_blocks: dict[int, list[dict]] = {}
        merged_groups: list[dict] = []
        map_lines: list[str] = []
        i = 0
        n = len(rounds)
        while i < n:
            r = rounds[i]
            blocks = blocks_module.segment_round_by_file(r)
            round_blocks[r["seq"]] = blocks
            versions_before = {
                p: len(e["versions"]) for p, e in ledger.entries().items()
            }
            is_chat = (
                len(blocks) == 1 and blocks[0]["kind"] == "fallback"
                and not blocks_module.round_has_tool_calls(r)
            )
            if not is_chat:
                if blocks:
                    orig = (r.get("user_input") or {}).get("original") or ""
                    sub = [
                        f"R{r['seq']}（{len(blocks)} 块）· 👤 {_clip_quote(orig)}"
                    ]
                    round_tools = blocks_module.round_has_tool_calls(r)
                    for b in blocks:
                        if b["kind"] == "file":
                            state = blocks_module.state_label(
                                blocks_module.block_end_state(
                                    ledger.state_of(b["file"]), b
                                )
                            )
                        else:
                            state = ""
                        label = blocks_module.label_line(
                            b, versions_before, state, round_tools
                        )
                        extra = ""
                        if b["kind"] == "user":
                            # 用户块：附补充输入原话锚点（轮头之后的用户事件）
                            bevs = set(b.get("events") or [])
                            texts = [
                                e["message"].get("content") or ""
                                for e in r["events"]
                                if e.get("type") == "user" and e.get("id") in bevs
                            ]
                            if texts:
                                extra = f" · 👤 {_clip_quote('；'.join(texts))}"
                        sub.append(
                            f"  {b['id']} {label}{extra}"
                            f"（{b['start_event']}~{b['end_event']}）"
                        )
                    map_lines.append("\n".join(sub))
                else:
                    map_lines.append(
                        f"R{r['seq']}：无分块结构（改用 refined_index 事件摘要）"
                    )
                ledger.update(r, blocks=blocks)
                i += 1
                continue
            # 连续纯聊天轮 → 合并组（组内只输出一个结果）
            group = [r]
            j = i + 1
            while j < n:
                rj = rounds[j]
                bj = blocks_module.segment_round_by_file(rj)
                if (
                    len(bj) == 1 and bj[0]["kind"] == "fallback"
                    and not blocks_module.round_has_tool_calls(rj)
                ):
                    group.append(rj)
                    j += 1
                else:
                    break
            first_seq, last_seq = group[0]["seq"], group[-1]["seq"]
            anchor = (
                f"R{first_seq}" if first_seq == last_seq
                else f"R{first_seq}-{last_seq}"
            )
            bid = f"{anchor}-B1"
            first_ev = group[0]["events"][0].get("id") or ""
            last_ev = group[-1]["events"][-1].get("id") or ""
            merged_groups.append({
                "bid": bid,
                "anchor": anchor,
                "seqs": [g["seq"] for g in group],
                "first_seq": first_seq,
            })
            sub = [f"{anchor}（{len(group)} 轮·纯聊天合并）"]
            for g in group:
                go = (g.get("user_input") or {}).get("original") or ""
                sub.append(f"  👤 {_clip_quote(go)}")
            sub.append(f"  {bid} 【保底块】：（{first_ev}~{last_ev}）")
            map_lines.append("\n".join(sub))
            for g in group:
                round_blocks[g["seq"]] = [{
                    "id": bid,
                    "kind": "fallback",
                    "start_event": first_ev,
                    "end_event": last_ev,
                }]
                ledger.update(
                    g, blocks=blocks_module.segment_round_by_file(g)
                )
            i = j
        return round_blocks, map_lines, merged_groups

    def _organize_rounds(
        self, rounds: list[dict], base_messages: Optional[list[dict]] = None
    ) -> tuple:
        """批量整理已闭合的 Round 们（维护管线的工作单元，V3 §4 机制）。

        2026-09-08 用户拍板：**整理 = 一次追加式对话**。输入 = 触发时刻
        的装配原文快照（base_messages 一字不动——纯追加才骑得住前缀缓存，
        任何内联改写都会把缓存从插入点打断）+ 尾部追加"分块地图 + 整理
        指令"。全部工具禁用、单次生成：read_full 退役（原文本来就在眼前，
        截断索引再造一遍反而丢了细节还多付一遍生成）。
        输出 = 各轮 Normalized 意图 + 关键约束 + 逐块信息量自适应描述 + 合并
        State Patch，经常驻工具 submit_organization 提交（与工作对话同一
        tools 数组——序列化恒定，前缀缓存常骑；字段语义以 schema 描述为
        唯一事实源），**全部写入 pending_org 暂存区**：本轮装配必须纹丝
        不动（连贯性 + 缓存前缀稳定），下一轮开启时由 _promote_org_results
        生效。产物对应不上批次轮（seq 缺失等畸形，GLM 实测会整字段省略）
        时整批 org_state=failed 回入水位，原始层永远不受影响——**不重试**
        （2026-09-10 用户拍板：重试只是再付一遍完整生成，不能确定解决
        失败）。返回 (是否成功, 交换记录或 None)——交换记录供分裂阶段
        纯追加（(messages, content, ordered)）。
        """
        round_blocks, map_lines, merged_groups = self._block_map_lines(rounds)

        seq_list = "、R".join(str(r["seq"]) for r in rounds)
        ms_lines = self._milestone_map_lines(rounds)
        instruction = (
            "[整理指令]\n"
            "以上是本会话的完整上下文。请把其中这些轮次整理成结构化档案："
            f"R{seq_list}。其余轮次不要输出。\n\n"
            + _ORG_META_INFO.format(seq_list=seq_list)
            + "\n"
            "[分块地图]（块由 Runtime 按工作对象确定性划分并打了标签；"
            "标签 = 写多细、写什么的指令，按标签执行，不需要判断哪些"
            "内容重要）\n"
            + "\n".join(map_lines)
            + "\n\n"
            + ("\n".join(ms_lines) + "\n\n" if ms_lines else "")
            + _ORG_FORMAT_DISCIPLINE
            + _ORG_TAG_INSTRUCTIONS
            + "\n完成后调用 submit_organization 工具提交结果（唯一出口，不要"
            "在正文中输出 JSON）。字段语义以工具定义为准；块 ID 逐字取自"
            "[分块地图]，每个块一条、一个不落、与地图同序。"
        )
        messages = list(base_messages or self._org_fallback_base(rounds))
        messages.append({"role": "user", "content": instruction})

        # tools 数组：默认与工作调用完全相同（缓存复议结论，2026-09-11）。
        # 缘起：09-10 曾收窄为只留 submit_organization（防模型把整理指令
        # 当工作对话乱调工具），代价是整个前缀缓存失效（实测命中 0.4%，
        # 约 0.6 元/批）。复议实测表明这笔交易不划算——维护调用**从不执行
        # 工具**（只捕获提交参数），漂移只导致"这批没产物"，而失败已有
        # 带诊断重发兜底；且当时工具已只剩一个出口，模型照样调了不存在的
        # 工具（幻觉），收窄并未真正防住。故改为恒定数组骑满缓存，
        # WOVRA_MAINT_NARROW_TOOLS=1 可切回收窄（见 support.maint_tools）。
        org_tools = maint_tools(self._schemas, _ORG_SUBMIT_TOOL)
        content, ordered, _usage = self._stream_call(
            messages, tools=org_tools, purpose="organization",
        )
        foreign = self._foreign_tool_calls(ordered, _ORG_SUBMIT_TOOL)
        if foreign and self.task is not None:
            # 漂移的观测口径：恒定数组下模型偶尔会调工作工具（无害——
            # 不执行，产物仍从 submit_organization 取），留痕供复议取数
            self.task.record(
                "maintenance",
                f"org：模型调用了非出口工具 {foreign[:4]}，已忽略",
            )
        state, strategy = self._extract_org_state(content, ordered)
        staged = self._stage_org_state(
            state, rounds, round_blocks, merged_groups
        )
        retried = False
        if staged and strategy not in ("", "原样", "正文 JSON") and self.task:
            # 自动修复成功也留痕：模型产物有多脏、哪种修复在起作用，是
            # "要不要加提示词护栏"的决策依据（§11.6）
            self.task.record(
                "maintenance", f"org：模型产物畸形，已自动修复（{strategy}）"
            )
        if staged == 0:
            # 失败即"查因 → 调整 → 再试一次"（2026-09-11 用户澄清：当初的
            # "不重试"是测试期用来逼出原因的手段，不是机制）。但重试**必须
            # 带诊断**——把"JSON 第 N 字符处不合法"回给模型让它就地修正，
            # 而不是原封不动重发整份产物（那才是 09-10 被否掉的做法，实测
            # 同因失败）。messages 一字不动、只在尾部追加失败现场，所以
            # 这次调用骑满前缀缓存，比重新整理便宜得多。
            evidence = self._org_failure_evidence(content, ordered)
            repair = self._org_repair_messages(messages, content, ordered, evidence)
            if repair is not None:
                retried = True
                if self.task is not None:
                    self.task.record(
                        "maintenance",
                        f"org：产物不可用，带诊断重发一次（{evidence}）",
                    )
                content, ordered, _usage = self._stream_call(
                    repair, tools=org_tools, purpose="organization",
                )
                state, strategy = self._extract_org_state(content, ordered)
                staged = self._stage_org_state(
                    state, rounds, round_blocks, merged_groups
                )
                if staged and strategy not in ("", "原样", "正文 JSON") and self.task:
                    self.task.record(
                        "maintenance",
                        f"org：重发产物畸形，已自动修复（{strategy}）",
                    )
        if staged == 0:
            # 无可用产物：保持 Runtime 视图（org_state=failed，回入水位
            # 等下次触发），原始层永远不受影响。失败现场必须留痕——原先
            # 只留 org=False，事后查因要重跑 213K token 的输入（见 §11）。
            evidence = self._org_failure_evidence(content, ordered)
            if self.task is not None:
                self.task.record(
                    "maintenance",
                    f"org：无可用产物（{len(rounds)} 轮批次；"
                    f"{'已带诊断重发一次仍失败；' if retried else ''}{evidence}）",
                )
            for r in rounds:
                r.pop("pending_org", None)
                r["org_state"] = "failed"
            self._persist_rounds()
            return False, None
        # 逐轮判定（2026-09-11 实测修复）：只有**真拿到产物**的轮才算 done。
        # 原先无条件对全批打 done —— 模型少输出一轮时（seq 匹配不上、位置
        # 兜底又要求项数相等），那一轮既无描述又被标 done，视图降级渲染成
        # 事件索引，且因 org_state=done 永不再整理：静默的质量损失，账面上
        # 看不出来。实测：批次 3 轮、模型只回 2 项 → ok=True，R3 被标 done
        # 但无任何块描述。现在缺产物的轮判 failed 回入水位（下批重做），
        # 原始层不受影响。
        staged_rounds = [r for r in rounds if r.get("pending_org")]
        missing = [r["seq"] for r in rounds if not r.get("pending_org")]
        for r in rounds:
            r["org_state"] = "done" if r.get("pending_org") else "failed"
        if missing and self.task is not None:
            self.task.record(
                "maintenance",
                f"org：产物只覆盖 {len(staged_rounds)}/{len(rounds)} 轮，"
                f"R{missing} 未收到产物（判 failed，回入水位下批重做）",
            )
        # 代次打标：最近 3 批视图保留，更早的按文件状态折叠（2026-09-10）
        self._org_generation += 1
        for r in staged_rounds:
            r["org_generation"] = self._org_generation
        self._persist_rounds()
        # 交换记录（2026-09-10）：split 阶段纯追加这段对话——org 刚跑完
        # KV 全热，分裂白得整理产物（12K+），只付自己的指令 ~1.5K
        exchange = (messages, content or "", ordered or [])
        return True, exchange

    def _stage_org_state(
        self, state: Optional[dict], rounds: list[dict], round_blocks: dict,
        merged_groups: Optional[list[dict]] = None,
    ) -> int:
        """把整理产物写进各轮的 pending_org 暂存区，返回匹配到轮的数量。

        轮匹配三级：item["seq"] 整数 → 字符串数字强转 → 一一对应声明下
        按位置兜底（GLM 实测会整字段省略 seq，2026-09-08 复测发现）。
        全都对应不上由调用方判失败，不静默吞掉。
        """
        if not isinstance(state, dict):
            return 0
        rounds_by_seq = {r["seq"]: r for r in rounds}
        items = [it for it in (state.get("rounds") or []) if isinstance(it, dict)]
        staged = 0
        for idx, item in enumerate(items):
            r = None
            try:
                r = rounds_by_seq.get(int(item.get("seq")))
            except (TypeError, ValueError):
                r = None
            if r is None and len(items) == len(rounds):
                r = rounds[idx]
            if r is None:
                continue
            staged += 1
            # 暂存区：不直写正式字段——本轮对话期间的装配由这些字段
            # 组成，动它们就是"下一步替换"，会破坏连贯性和缓存前缀
            pending = r["pending_org"] = {}
            if item.get("normalized_user_input"):
                pending["normalized"] = str(item["normalized_user_input"])
            if item.get("key_constraints"):
                pending["key_constraints"] = str(item["key_constraints"])
            blocks = round_blocks.get(r["seq"]) or []
            blocks_by_id = {b["id"]: b for b in blocks}
            summaries = {}
            for bs in item.get("block_summaries") or []:
                if (
                    isinstance(bs, dict)
                    and str(bs.get("id") or "") in blocks_by_id
                    and bs.get("summary")
                ):
                    summaries[str(bs["id"])] = str(bs["summary"])
            if blocks_by_id:
                # 完整性兜底：LLM 漏标的块用确定性路由行补齐——视图里
                # 不允许出现没有描述的块（用户拍板：保证完整的细节描述）
                for bid, b in blocks_by_id.items():
                    if bid not in summaries:
                        parts = [f"{b['start_event']}~{b['end_event']}"]
                        if b["kind"] == "file":
                            parts.append(b["file"])
                        elif b.get("wrote_files"):  # 旧 v2 块兼容
                            parts.append("写: " + ", ".join(b["wrote_files"]))
                        if b.get("command_types"):
                            parts.append("命令[" + ", ".join(b["command_types"]) + "]")
                        summaries[bid] = "（LLM 未标注，仅路由）" + " · ".join(parts)
                pending["block_summaries"] = summaries
                if not r.get("blocks"):
                    pending["blocks"] = blocks  # 旧轮现场算出的块结构一并落盘
            valid_ids = {e["id"] for e in r["events"]}
            for line_item in item.get("refined_index") or []:
                if (
                    isinstance(line_item, dict)
                    and line_item.get("id") in valid_ids
                    and line_item.get("line")
                ):
                    pending.setdefault("refined_index", {})[line_item["id"]] = str(
                        line_item["line"]
                    )
        patch = state.get("state_patch")
        if isinstance(patch, dict) and rounds:
            if state.get("is_done") is not None and "is_done" not in patch:
                patch["is_done"] = bool(state["is_done"])
            # 批次级补丁挂在批内第一轮上：生效时只应用一次
            rounds[0].setdefault("pending_org", {})["state_patch"] = patch
        # 合并组广播（2026-09-10 用户拍板）：连续纯聊天轮合并为一个
        # 结果——组内任一轮拿到合成块描述则全组共享；组首轮标记
        # merged_anchor（视图显示 [R1-2] 锚点），非组首轮标记
        # merged_skip（视图跳过，由组首合并显示）
        for mg in merged_groups or []:
            text = ""
            for seq in mg["seqs"]:
                r = rounds_by_seq.get(seq)
                po = (r or {}).get("pending_org") or {}
                s = po.get("block_summaries", {}).get(mg["bid"])
                if s:
                    text = s
                    break
            if not text:
                continue  # 组内无人写（LLM 全跳）：保持各自兜底行
            for seq in mg["seqs"]:
                r = rounds_by_seq.get(seq)
                if r is None:
                    continue
                po = r.setdefault("pending_org", {})
                po["block_summaries"] = {mg["bid"]: text}
                if seq == mg["first_seq"]:
                    po["merged_anchor"] = mg["anchor"]
                else:
                    po["merged_skip"] = mg["anchor"]
        return staged

    @staticmethod
    def _org_fallback_base(rounds: list[dict]) -> list[dict]:
        """base_messages 缺省时的兜底装配：批次轮的原始协议消息（直调/测试用）。"""
        return [e["message"] for r in rounds for e in r["events"]]

    def _split_rounds(
        self, rounds: list[dict], exchange: Optional[tuple],
        base_messages: Optional[list[dict]] = None,
    ) -> bool:
        """分裂分析（串行维护管线的第二阶段，2026-09-10 用户定稿）。

        输入 = 整理对话的**纯追加延续**：org 的 messages + assistant(提交
        调用) + org 产物 + 分裂指令。org 刚跑完 KV 全热——分裂白得完整
        整理产物（12K+），只付自己的指令 ~1.5K（缓存友好的关键）。
        判据三步（见 _SPLIT_INSTRUCTIONS）：保底块归属（独立思想归主
        agent）→ 按功能语义聚合文件域层级（不按路径）→ 找最浅可分层
        判分裂。硬数据（活性文件清单 + 上限）由 Runtime 注入，LLM 只做
        语义判断。开思考（用户拍板：不开思考不调工具且质量低）。
        Level 0 只分析不分裂：产物暂存待查。
        """
        if exchange is not None:
            org_messages, org_content, org_ordered = exchange
            messages = list(org_messages)
            if org_ordered:
                # 工具调用形态（真实路径）：重建提交对话保证结构合法
                messages.append({
                    "role": "assistant",
                    "content": org_content or None,
                    "tool_calls": [
                        {
                            "id": tc.get("id") or f"org_{i}",
                            "type": "function",
                            "function": {
                                "name": tc.get("name") or "submit_organization",
                                "arguments": tc.get("arguments") or "{}",
                            },
                        }
                        for i, tc in enumerate(org_ordered)
                    ],
                })
                messages.append({
                    "role": "tool",
                    "tool_call_id": (org_ordered[0].get("id") if org_ordered
                                     else "org_0"),
                    "content": "已收到整理产物。",
                })
            else:
                # 正文 JSON 形态（fallback）：assistant 原文即可
                messages.append({
                    "role": "assistant", "content": org_content or "",
                })
        else:
            messages = list(
                base_messages or self._org_fallback_base(rounds)
            )

        # 硬数据（Runtime 生成，零 LLM）：活性文件清单 + 数量上限
        hard_lines, n_live = self._split_hard_data(rounds)
        round_blocks, map_lines, _merged = self._block_map_lines(rounds)
        # 主 agent 残留桶占比（零 LLM 体量事实）：纯对话块合计占本批内容
        # 多少——顶层节点计数的门槛由它定（判据见 _SPLIT_INSTRUCTIONS）。
        chat_share = self._chat_block_share(rounds, round_blocks)
        all_ids = {
            b["id"] for blocks in round_blocks.values() for b in blocks
        }
        seq_list = "、R".join(str(r["seq"]) for r in rounds)
        instruction = (
            "[分裂分析指令]\n"
            "以上是本会话的完整上下文（含刚完成的整理产物与分块地图）。"
            f"请做**现状归属分析**（不是话题分类），对象为这些轮次：R{seq_list}。\n\n"
            "[硬数据]（Runtime 生成，零 LLM）\n"
            + "\n".join(hard_lines)
            + f"\n- 活性文件数（分裂单元数上限）：{n_live}"
            + f"\n- 纯对话块（无文件交互，闲聊）内容占比：约 {chat_share * 100:.0f}%"
            + "\n\n"
            "[分块地图]（块按工作对象确定性划分，条目格式 = 块ID=事件范围）\n"
            + "\n".join(map_lines)
            + "\n\n"
            + _SPLIT_INSTRUCTIONS
        )
        messages.append({"role": "user", "content": instruction})

        # 与 org 同策略：恒定 tools 数组（缓存复议结论，见 org 处注释）。
        # 开思考（不传 thinking disabled）：语义判断需要推理，实测
        # 不开思考不调工具、质量低（用户拍板）。
        split_tools = maint_tools(self._schemas, _SPLIT_SUBMIT_TOOL)
        content, ordered, _usage = self._stream_call(
            messages, tools=split_tools, purpose="split",
        )
        foreign = self._foreign_tool_calls(ordered, _SPLIT_SUBMIT_TOOL)
        if foreign and self.task is not None:
            self.task.record(
                "maintenance",
                f"split：模型调用了非出口工具 {foreign[:4]}，已忽略",
            )
        product = self._extract_domains(content, ordered)
        evidence = ""
        for tc in ordered or []:
            if tc.get("name") == _SPLIT_SUBMIT_TOOL:
                args = tc.get("arguments") or ""
                evidence = (f"submit_domains 参数 {len(args)} 字符："
                            f"{args[:120]!r}")
                break
        if product is None:
            # 失败即"查因 → 调整 → 再试一次"（2026-09-11：与 org 对称补齐）。
            # 此前分裂只有留痕、无恢复路径，而 submit_domains 同样面临超长
            # 截断（指令明说"必须在单次响应内完整输出"）——已知风险却没有
            # 第二跳。messages 一字不动、尾部追加失败现场，只重发一次。
            if not evidence:
                evidence = f"无 submit_domains 调用；正文 {len(content or '')} 字符"
            repair = self._split_repair_messages(
                messages, content, ordered, evidence
            )
            retried = False
            if repair is not None:
                retried = True
                if self.task is not None:
                    self.task.record(
                        "maintenance",
                        f"split：产物不可用，带诊断重发一次（{evidence}）",
                    )
                content, ordered, _usage = self._stream_call(
                    repair, tools=split_tools, purpose="split",
                )
                product = self._extract_domains(content, ordered)
                if product is not None and self.task is not None:
                    self.task.record(
                        "maintenance", "split：重发产物可用，分析继续"
                    )
        if product is None:
            # 仍无可用产物：不静默落空（2026-09-11 实测：submit_domains
            # 参数截断/解析失败 → 分裂无任何痕迹）——落一个保守的"不可分"
            # 判定并留痕，账本可查。失败现场一并记录（arguments 长度+头部 /
            # 正文长度），下次直接能看出是截断还是空壳还是模型没走工具出口。
            if not evidence:
                evidence = f"无 submit_domains 调用；正文 {len(content or '')} 字符"
            reason = (
                "分裂分析无可用产物（" + evidence + "），"
                "保守按不可分处理"
                + ("，已带诊断重发一次仍失败" if retried else "，不重试")
            )
            pending = rounds[0].setdefault("pending_org", {})
            pending["split_assessment"] = {
                "splittable": False,
                "reason": reason,
            }
            if self.task is not None:
                self.task.record(
                    "maintenance",
                    f"split：无可用产物（{len(rounds)} 轮批次；"
                    f"{'已带诊断重发一次仍失败；' if retried else ''}"
                    f"{evidence}）",
                )
            self._persist_rounds()
            return False
        domains, unassigned, split = product
        # 文件域自动归属（2026-09-11）：块的 file ∈ 某域 file_domains 时，
        # Runtime 机械归入该域——模型不必逐块列 block_ids（大幅缩小输出，
        # 防超长截断令分析作废）。模型显式声明的 block_ids 优先（跨域/
        # 例外仍可写）。
        self._auto_assign_domains(domains, round_blocks)
        # 主 agent 兜底（2026-09-10，thoughts 字段收敛后）：没被任何域
        # 认领的块 = 独立思想/零散块 → Runtime 自动归 unassigned（主
        # agent 剩余集合）。模型忘了填 unassigned 也不丢块——语义上
        # "没有域认领"与"归主 agent"等价，不需要模型再声明一次。
        covered = {
            b for d in domains if isinstance(d, dict)
            for b in (d.get("block_ids") or [])
        }
        kept = [
            b for b in ((unassigned or {}).get("block_ids") or [])
            if b in all_ids
        ]
        orphans = sorted(all_ids - covered - set(kept))
        if orphans:
            unassigned = {
                "block_ids": kept + orphans,
                "reason": (unassigned or {}).get("reason")
                or "未被任何文件域认领（独立思想/零散块），归主 agent",
            }
            if self.task is not None:
                self.task.record(
                    "maintenance",
                    f"split：{len(orphans)} 块未被域认领，已自动归主 agent "
                    f"{orphans[:8]}" + ("…" if len(orphans) > 8 else ""),
                )
        # 暂存到批首轮（与 state_patch 同通道），下一轮开启随 promote 生效
        pending = rounds[0].setdefault("pending_org", {})
        pending["domains"] = domains
        if unassigned:
            pending["unassigned"] = unassigned
        if split:
            pending["split_assessment"] = split
        self._persist_rounds()
        return True

    def _auto_assign_domains(
        self, domains: list[dict], round_blocks: dict[int, list[dict]]
    ) -> None:
        """把文件块按文件归属机械归入对应域（零 LLM，原地修改 domains）。

        匹配规则：块的文件 == 域 file_domains 条目（精确文件），或在该
        条目前缀下（目录形态 "js/" → 其下全部文件）。已显式出现在任何
        域 block_ids 里的块不覆盖（尊重模型的跨域/例外声明）。
        """
        if not domains:
            return
        block_file = {
            b["id"]: b.get("file")
            for blocks in round_blocks.values() for b in blocks
            if b.get("kind") == "file" and b.get("file")
        }
        if not block_file:
            return
        claimed = {
            b for d in domains if isinstance(d, dict)
            for b in (d.get("block_ids") or [])
        }
        file_domains: list[tuple[str, str]] = []
        domain_by_name: dict[str, dict] = {}
        for d in domains:
            if not isinstance(d, dict) or not d.get("name"):
                continue
            domain_by_name[d["name"]] = d
            for fd in d.get("file_domains") or []:
                file_domains.append((str(fd).rstrip("/"), d["name"]))
        if not file_domains:
            return
        for bid, f in block_file.items():
            if bid in claimed:
                continue
            for prefix, name in file_domains:
                if f == prefix or f.startswith(prefix + "/"):
                    target = domain_by_name.get(name)
                    if target is not None:
                        target.setdefault("block_ids", []).append(bid)
                    break

    def _chat_block_share(
        self, rounds: list[dict], round_blocks: dict[int, list[dict]]
    ) -> float:
        """纯对话块（fallback，无文件交互）内容占本批的比例（零 LLM）。

        分裂判据的体量事实（2026-09-11 用户拍板）：闲聊很多时，主 agent
        残留桶（纯对话/未入域块）与域并列构成顶层节点（该分裂）；占比
        ≤ _SPLIT_CHAT_MERGE_RATIO（15%，≈15K @ 100K 水位）则不拆出，
        并入主 agent、不计入节点数。内容量按事件 message.content 字符数
        估算（与装配同口径的量级，精确值不重要）。
        """
        total = 0
        chat = 0
        for r in rounds:
            by_id = {e["id"]: e for e in (r.get("events") or [])}
            for b in round_blocks.get(r["seq"]) or []:
                size = sum(
                    len((by_id.get(i) or {}).get("message", {}).get("content") or "")
                    for i in b.get("events") or []
                )
                total += size
                if b.get("kind") == "fallback":
                    chat += size
        return (chat / total) if total else 0.0

    def _split_hard_data(self, rounds: list[dict]) -> tuple[list[str], int]:
        """分裂硬数据（零 LLM）：活性文件清单 + 数量。

        新会话文件状态已变——必须现场重算 ledger（不信持久化状态）。
        只列 live / read_only（磁盘上存在的），dead 不列（分裂对象是
        现状，死文件不占域、不占上限）。
        """
        ledger = lifecycle_module.FileLedger()
        for rr in self.rounds:
            ledger.update(rr, blocks=blocks_module.segment_round_by_file(rr))
        lines = ["- 活性文件清单（状态 live/read_only，块引用供归属）："]
        n = 0
        for path, e in sorted(ledger.entries().items()):
            if e["state"] == lifecycle_module.STATE_DEAD:
                continue
            n += 1
            refs = " ".join(e.get("block_refs") or [])
            lines.append(
                f"  - {path}（{e['state']}；写 {e['write_count']}/"
                f"读 {e['read_count']}；块: {refs or '无'}）"
            )
        if n == 0:
            lines.append("  （无——当前无任何活性文件，无可分域）")
        return lines, n

    @staticmethod
    def _extract_domains(content: str, ordered: list):
        """从分裂分析响应提取产物：优先 submit_domains 调用参数，回退
        正文 JSON。返回 (domains, unassigned, split_assessment) 或 None。

        空 domains 是合法结果（判不可分/全链单子时域列表为空），不能当
        失败丢弃——2026-09-11 实测：合法的"不可分"响应被当无产物，分裂
        静默落空。只有解析不出任何 dict 状态才算无产物。
        """
        state = None
        for tc in ordered or []:
            if tc.get("name") != "submit_domains":
                continue
            try:
                state = json.loads(tc.get("arguments") or "{}")
            except json.JSONDecodeError:
                continue
            if isinstance(state, dict):
                break
            state = None
        if state is None:
            state = _MaintenanceMixin._parse_state_json(content)
        if not isinstance(state, dict):
            return None
        # 空壳判定（2026-09-11）：截断到只剩 {} 的 arguments 能解析成功，
        # 但三个产物键一个都没有——那不算"空域合法"，是无产物。
        if not any(
            k in state for k in ("domains", "unassigned", "split_assessment")
        ):
            return None
        domains = _MaintenanceMixin._dedupe_domains(state.get("domains") or [])
        return (
            domains,
            state.get("unassigned") or {},
            state.get("split_assessment") or {},
        )

    @staticmethod
    def _dedupe_domains(domains: list) -> list:
        """代码层兜底（2026-09-10）：同块跨域重复（模型偶发）按先到先留
        去重——子上下文拼装时同块出现在两个域会浪费且引起归属歧义。
        域名单保持原顺序；重复只从后续域移除。"""
        seen: set = set()
        for d in domains:
            if not isinstance(d, dict):
                continue
            ids = d.get("block_ids") or []
            kept = []
            for bid in ids:
                if bid in seen:
                    continue
                seen.add(bid)
                kept.append(bid)
            d["block_ids"] = kept
        return domains

    def _parallel_maintenance(
        self, batch: list[dict], base: list[dict]
    ) -> tuple:
        """水位维护管线：整理 → 分裂（串行两阶段，2026-09-10 用户定稿）。

        原并行双路（各自骑 base 快照）改为串行追加：分裂作为整理对话的
        纯追加延续——org 刚跑完 KV 全热，分裂白得完整整理产物（12K+），
        只付自己的指令 ~1.5K。**org 失败则 split 跳过**（分裂依赖整理
        质量，失败批次不产出）。

        缓存代价（2026-09-11 两轮实测，均为本会话 history 的 llm_call 对账）：
        本函数不设开关、不分支，只记录事实（工具数组的选择在
        support.maint_tools）。
        * 收窄时代（R9 的决定，已被 R11 推翻）：把 tools 从常驻数组收窄
          为单一出口**确实会打破前缀缓存**——org 首跳 prompt=215,940 /
          cached=896（0.4%）、split 首跳 290,142 / 896（0.3%），其余批次
          1.1%–5.1%。机理属**服务端行为、本机未直接验证**：SDK 里 tools
          字段其实排在 messages **之后**（见 llm.py 的 payload 构造），
          但实测维护调用整段未命中，说明服务端把工具定义算进了缓存前缀
          （或缓存键）——不能因为它在 JSON 里靠后就假设它与前缀无关。
          原注释"cached=83,968/命中 95%、收窄不影响前缀缓存"是错的（那是
          工作调用在 tools 数组未变时的读数），worklog §11.6(d) 的 ≈0 命中
          才对。
        * 恒定时代（R11 改回默认，21:29 重启后生效，§16 复测）：org
          98,954 / 90,112（91.1%）、split 106,714 / 98,944（92.7%）。
          split 的 cached 98,944 ≈ org 的 prompt 98,954——org 那一段几乎
          100% 被继承，split 只付自己新增的 7,770 tok（分块地图 + 分裂
          指令），纯追加继承按设计工作。残余 ~9% 未命中是**固有成本**：
          尾部信封（TaskState/文件地图/todo）+ 整理指令与分块地图必然是
          新 token；working 调用能到 97% 是因为它每步只付"一步增量"，
          而维护调用一次付掉整个尾部。
        * 维护总 miss：收窄时代 6 次合计 ≈1,161,381 tok → 恒定时代
          2 次合计 16,612 tok（−98.6%）。
        故 §12.5/§13.1 里那组 0.4%/0.3% 是**历史读数**，不是现况——
        引用前先看 §16。

        **硬上限 WOVRA_MAINT_TIMEOUT**：两阶段总预算（F 组实测大基座
        深度思考 23 分钟不完成，读超时不触发——token 在流；到点判
        failed 解锁管线）。守护线程化保证进程退出不被挂起调用拖住。
        启动/结束落账 history，挂起可观测。返回 (org_ok, split_ok)。
        """
        if self.task is not None:
            self.task.record(
                "maintenance",
                f"启动：批次 R{batch[0]['seq']}-R{batch[-1]['seq']}"
                f"（{len(batch)} 轮，输入快照 {len(base)} 条消息，硬上限 {self._org_maint_timeout:.0f}s）",
            )
        results = {"org": False, "split": False}
        box: dict = {}

        def run() -> None:
            try:
                results["org"], exchange = self._organize_rounds(batch, base)
                box["exchange"] = exchange
            except Exception as error:  # noqa: BLE001——失败不拖垮管线
                for r in batch:
                    r.pop("pending_org", None)
                    r["org_state"] = "failed"
                self._persist_rounds()
                if self.task is not None:
                    self.task.record(
                        "maintenance", f"org 阶段失败：{str(error)[:150]}"
                    )
                return
            if not results["org"]:
                return  # org 失败：split 跳过（依赖整理质量）
            try:
                results["split"] = self._split_rounds(
                    batch, box.get("exchange"), base
                )
            except Exception as error:  # noqa: BLE001
                if self.task is not None:
                    self.task.record(
                        "maintenance", f"split 阶段失败：{str(error)[:150]}"
                    )

        thread = threading.Thread(
            target=run, name="wovra-maintenance", daemon=True
        )
        thread.start()
        thread.join(self._org_maint_timeout)
        timed_out = thread.is_alive()
        if self.task is not None:
            self.task.record(
                "maintenance",
                f"结束：org={results['org']} split={results['split']}"
                + ("（超时返回，挂起阶段随守护线程终结或迟到完成）" if timed_out else ""),
            )
        return results["org"], results["split"]

    def _promote_org_results(self) -> None:
        """把暂存的整理产物落进正式视图（仅在**新 Round 开启时**调用）。

        本轮对话期间装配必须保持原样（连贯性 + 缓存前缀稳定），所以
        整理线程只把产物写进各轮的 pending_org 暂存区；直到下一轮
        开启，才替换精修索引/Normalized 意图、应用状态补丁并落盘。
        崩溃安全：pending_org 随 rounds 一起持久化，重启后第一次开
        新轮时补生效。
        """
        changed = False
        for r in self.rounds:
            pending = r.pop("pending_org", None)
            if not pending:
                continue
            changed = True
            if pending.get("normalized"):
                r["user_input"]["normalized"] = pending["normalized"]
            if pending.get("key_constraints"):
                r["user_input"]["key_constraints"] = pending["key_constraints"]
            if pending.get("blocks") and not r.get("blocks"):
                r["blocks"] = pending["blocks"]  # 旧轮补块结构
            if pending.get("block_summaries"):
                r["block_summaries"] = pending["block_summaries"]
            if pending.get("refined_index"):
                r.setdefault("refined_index", {}).update(pending["refined_index"])
            # 合并组标记（连续纯聊天轮合并显示）：组首 anchor，非组首 skip
            if pending.get("merged_anchor"):
                r["merged_anchor"] = pending["merged_anchor"]
            if pending.get("merged_skip"):
                r["merged_skip"] = pending["merged_skip"]
            # 分裂分析产物（Level 1：落档 + 落实为注册表条目）
            if pending.get("domains"):
                r["domains"] = pending["domains"]
                # 组织层落地点（2026-09-11 用户拍板 A：Level 1 视图分化）：
                # 域树 → 注册表条目是**机械翻译**（语义归模型、体量归机制）。
                # 幂等合并：崩溃补做/重启重放只更新既有条目。注册表在此
                # 才第一次长出主 agent 之外的条目——此前永远只有 A。
                if self.task is not None:
                    added, updated = registry_module.merge_into(
                        self.task.registry, pending["domains"]
                    )
                    if added or updated:
                        detail = []
                        if added:
                            detail.append(f"新增 {len(added)}（{'、'.join(added)}）")
                        if updated:
                            detail.append(
                                f"更新 {len(updated)}（{'、'.join(updated)}）"
                            )
                        self.task.record(
                            "maintenance",
                            "registry：分裂产物落实为注册表条目——"
                            + "，".join(detail),
                        )
            if pending.get("unassigned"):
                r["unassigned"] = pending["unassigned"]
            if pending.get("split_assessment"):
                r["split_assessment"] = pending["split_assessment"]
            patch = pending.get("state_patch")
            if patch and self.task is not None:
                report = self.task.apply_state_patch(patch)
                self._record_close_report(report)
        if changed:
            self._persist_rounds()

    def _record_close_report(self, report: dict) -> None:
        """结案报告的留痕（结案是删除操作，必须可见）。

        为什么必须留痕：结案会把条目从状态账本里**删掉**——模型下一轮
        就看不到了。history 是唯一能回答"这条什么时候、被谁（哪次整理）
        结掉的"的地方；也是误删时人工恢复的唯一线索。
        未匹配片段同样留痕——它说明模型认为该结案但机制找不到，
        是提示词质量或片段表述的信号（找到 0 条或多条一律不动，见
        `TaskState.close_items` 的注释：宁可留化石，不可错删活账）。
        """
        if self.task is None or not report:
            return
        closed = report.get("closed") or []
        unmatched = report.get("unmatched") or []
        if closed:
            detail = "；".join(f"{field}：{_clip_quote(text, 60)}" for field, text in closed)
            self.task.record(
                "maintenance", f"state：结案 {len(closed)} 条——{detail}",
            )
        if unmatched:
            detail = "；".join(f"{field}：{_clip_quote(text, 60)}" for field, text in unmatched)
            self.task.record(
                "maintenance",
                f"state：结案片段未匹配（不动，原样保留）——{detail}",
            )

    def _read_full_event(self, event_id: str) -> str:
        """按事件 ID 返回完整原文，不做内容截断。

        旧会话数据可能带分离的 full 字段（旧版安全截断的产物），
        一并返回保证可读；新数据 message 即全文。
        """
        for r in self.rounds:
            for e in r["events"]:
                if e["id"] == event_id:
                    message = e["message"]
                    parts = [f"[{event_id}] {e['type']}"]
                    if message.get("tool_calls"):
                        parts.append(
                            "调用: "
                            + json.dumps(message["tool_calls"], ensure_ascii=False)
                        )
                    parts.append(message.get("content") or "")
                    if e.get("full"):
                        parts.append("[完整原文]\n" + e["full"])
                    return "\n".join(parts)
        return f"未找到事件: {event_id}"

    @staticmethod
    def _loads_lenient(raw: str) -> tuple[Optional[dict], str]:
        """尽量解析模型给出的 JSON，返回 (状态字典, 生效策略名)。

        逐个试候选（最小干预优先，见 `_json_candidates`），每档再试
        严格/宽松两种解析——宽松档 `strict=False` 容忍字符串里的裸控制
        字符（长中文段落夹裸换行是常见畸形）。

        诚实边界：**不保证能修**（正文里"引号紧跟逗号"这类真歧义修不了），
        但**保证不会误判成功**——解析通过才算数，这就是校验。修不了时
        返回 (None, "")，由调用方带诊断重发（§11.5）。
        策略名回传供留痕：能看出模型产物有多脏、哪种修复在起作用。
        """
        if not (raw or "").strip():
            return None, ""
        for name, text in _json_candidates(raw):
            for strict in (True, False):
                try:
                    state = json.loads(text, strict=strict)
                except json.JSONDecodeError:
                    continue
                if isinstance(state, dict):
                    return state, name
        return None, ""

    @staticmethod
    def _extract_org_state(content: str, ordered: list) -> tuple[Optional[dict], str]:
        """从整理响应提取产物：优先 submit_organization 的调用参数，
        回退消息正文 JSON（自由文本输出兼容，主路径是工具出口）。

        返回 (状态字典或 None, 生效策略名)；策略名非"原样"即说明模型产物
        被自动修复过（供留痕与"要不要加提示词护栏"的决策依据）。
        """
        for tc in ordered or []:
            if tc.get("name") != "submit_organization":
                continue
            state, strategy = _MaintenanceMixin._loads_lenient(
                tc.get("arguments") or ""
            )
            if isinstance(state, dict):
                return state, strategy
        state = _MaintenanceMixin._parse_state_json(content)
        return (state, "正文 JSON" if isinstance(state, dict) else "")

    @staticmethod
    def _org_repair_messages(
        messages: list[dict], content: str, ordered: list, evidence: str
    ) -> Optional[list]:
        """整理阶段的"带诊断重发"输入（薄壳，通用实现见 _repair_messages）。"""
        return _MaintenanceMixin._repair_messages(
            messages, content, ordered, evidence, _ORG_SUBMIT_TOOL, _ORG_REPAIR_HINT
        )

    @staticmethod
    def _split_repair_messages(
        messages: list[dict], content: str, ordered: list, evidence: str
    ) -> Optional[list]:
        """分裂阶段的"带诊断重发"输入（与 org 对称，2026-09-11 补齐）。"""
        return _MaintenanceMixin._repair_messages(
            messages, content, ordered, evidence,
            _SPLIT_SUBMIT_TOOL, _SPLIT_REPAIR_HINT,
        )

    @staticmethod
    def _repair_messages(
        messages: list[dict], content: str, ordered: list, evidence: str,
        tool_name: str, hint: str,
    ) -> Optional[list]:
        """构造"带诊断的重发"输入：把失败原因回给模型，只让它重发修正后的提交。

        两条路（取决于上一跳有没有工具调用可回复）：
          * 有该工具的调用（主路）→ 重建 assistant(tool_call) + tool 结果
            （内容 = 失败现场 + 修正要求）。严格端点要求 tool 消息必须紧跟
            其 assistant 调用，不能插别的消息。
          * 模型压根没调工具（跑偏/只写正文）→ assistant 正文 + user 追问
            （没有 tool_call_id 可回，只能走 user 角色）。

        messages 一字不动，只在其后追加——这次调用因此骑满前缀缓存。
        org/split 两阶段共用本实现（2026-09-11 泛化：分裂此前只有留痕、
        无恢复路径，与 org 不对称）。
        """
        fixed = list(messages)
        submit = next(
            (tc for tc in (ordered or []) if tc.get("name") == tool_name),
            None,
        )
        if submit is not None:
            call_id = submit.get("id") or "repair"
            fixed.append({
                "role": "assistant",
                "content": content or None,
                "tool_calls": [{
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": tool_name,
                        "arguments": submit.get("arguments") or "{}",
                    },
                }],
            })
            fixed.append({
                "role": "tool",
                "tool_call_id": call_id,
                "content": hint.format(evidence=evidence),
            })
            return fixed
        if content:
            fixed.append({"role": "assistant", "content": content})
        fixed.append({
            "role": "user",
            "content": (
                f"你刚才没有调用 {tool_name} 提交产物（它是唯一出口，"
                "正文里的说明不会被采纳）。\n"
                f"诊断：{evidence}\n"
                + hint.format(evidence="（同上）")
            ),
        })
        return fixed

    @staticmethod
    def _foreign_tool_calls(ordered: list, submit_name: str) -> list[str]:
        """维护调用里出现的**非出口**工具名（漂移的观测口径）。

        恒定 tools 数组下模型偶尔仍会调工作工具——维护阶段不执行任何工具，
        这些调用无害（产物只从 submit_* 取），但留痕能回答"恒定数组是否
        真的引来漂移"这个复议问题。
        """
        return [
            str(tc.get("name") or "")
            for tc in (ordered or [])
            if tc.get("name") and tc.get("name") != submit_name
        ]

    @staticmethod
    def _org_failure_evidence(content: str, ordered: list) -> str:
        """整理失败现场（零 LLM）：把"为什么没产物"写清楚，进 history。

        为什么必须有它（worklog-20260911.md §11）：19:43 那次失败只留下
        `org=False` 一行，产物与原因都没留——事后只能靠重跑一次（213K
        token 的输入）才查出来是 JSON 第 5069 字符处未转义引号。分裂阶段
        早有同类留痕（"分裂无产物：失败现场留痕"），整理路漏了。
        """
        for tc in ordered or []:
            if tc.get("name") != "submit_organization":
                continue
            args = tc.get("arguments") or ""
            try:
                json.loads(args, strict=False)
            except json.JSONDecodeError as error:
                pos = getattr(error, "pos", 0) or 0
                window = args[max(0, pos - 80):pos + 80].replace("\n", "\\n")
                return (
                    f"submit_organization 参数 {len(args):,} 字符，JSON 解析失败"
                    f"（位置 {pos:,}：{error.msg}）；现场 …{window}…"
                )
            state = _MaintenanceMixin._loads_lenient(args)[0]
            items = (state or {}).get("rounds") if isinstance(state, dict) else None
            if isinstance(items, list):
                return (
                    f"submit_organization 参数 {len(args):,} 字符、JSON 合法，"
                    f"但 rounds 有 {len(items)} 项却无法与批次轮匹配（seq 缺失/畸形）"
                )
            return (
                f"submit_organization 参数 {len(args):,} 字符，JSON 合法但 "
                f"rounds 字段缺失或非数组（值是 {type(items).__name__}）"
            )
        return f"无 submit_organization 调用；正文 {len(content or ''):,} 字符"

    @staticmethod
    def _parse_state_json(text: str) -> Optional[dict]:
        """从整理输出里解析 JSON；容忍围栏与前后说明文字。"""
        raw = (text or "").strip()
        if not raw:
            return None
        if raw.startswith("```"):
            raw = raw.strip("`")
        start, end = raw.find("{"), raw.rfind("}")
        if start == -1 or end <= start:
            return None
        try:
            state = json.loads(raw[start:end + 1])
        except json.JSONDecodeError:
            return None
        return state if isinstance(state, dict) else None

    def _baseline_context_estimate(self) -> int:
        """下一次请求的上下文体量估算（窗口占用口径）。

        摘要 + 未压缩轮次的全部事件（正文与工具调用参数）。
        水位曾误用累计计费口径——每步重复计整段 prompt，成倍虚增
        （实测真实上下文 3 万 tok、账本 58 万，提前触发压缩）。
        """
        parts = []
        if self.task is not None and self.task.baseline_summary:
            parts.append(self.task.baseline_summary)
        for r in self.rounds:
            if r.get("compacted"):
                continue
            for e in r["events"]:
                m = e["message"]
                parts.append(str(m.get("content") or ""))
                for tc in m.get("tool_calls") or []:
                    fn = tc.get("function") or {}
                    parts.append(str(fn.get("arguments") or ""))
        return tokens.estimate("\n".join(parts))

    def _baseline_accounting(self) -> None:
        """baseline：真实上下文体量达 80% × 窗口时触发阈值压缩。

        水位 = _baseline_context_estimate（下一次请求的体量），与
        managed 的窗口保底同口径；触发后压缩较早轮次为摘要，水位
        下次检查时自然反映压缩后的体量。baseline_prompt_used 只做
        计费口径的成本记录，不参与触发。
        """
        self._baseline_prompt_used += self.last_stats["prompt_tokens"]
        if self.task is not None:
            self.task.baseline_prompt_used = self._baseline_prompt_used
        threshold = self.context_limit * _COMPRESS_THRESHOLD
        if self._baseline_context_estimate() < threshold:
            return
        older = [r for r in self.rounds if not r.get("compacted")][:-2]
        if len(older) < 1:
            return  # 保留最近 2 轮原文；没有可压缩的历史就等下一轮
        self._emit_status("历史接近窗口上限，正在压缩较早的对话…")
        transcript = "\n\n".join(
            f"[R{r['seq']}] " + truncate.render_round_events(r) for r in older
        )
        summary, _ordered, _usage = self._stream_call(
            [{
                "role": "user",
                "content": (
                    "以下是长会话较早阶段的对话记录（截断索引形式）。"
                    "请压缩成一段高密度摘要，保留：任务相关结论、重要决策、"
                    "已尝试方案与结果、未解决的问题。省略寒暄与重复输出。"
                    "直接输出摘要正文。\n\n" + transcript
                ),
            }],
            purpose="compaction",
            extra_body={"thinking": {"type": "disabled"}},
        )
        if self.task is not None:
            prior = self.task.baseline_summary
            self.task.baseline_summary = (
                (prior + "\n\n" if prior else "") + summary.strip()
            )
        for r in older:
            r["compacted"] = True
