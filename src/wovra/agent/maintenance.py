"""Agent 维护管线：水位批量整理（organization）、分裂分析（split）、
暂存与 promote、baseline 阈值压缩。整理 = 一次追加式对话，分裂是它的
纯追加延续。
"""
import json
import threading
import time
from typing import Optional
from .. import blocks as blocks_module
from .. import lifecycle as lifecycle_module
from .. import tokens as tokens
from .. import truncate as truncate
from .support import (
    MODE_MANAGED,
    _COMPRESS_THRESHOLD,
    _clip_quote,
)
from .prompts import (
    _ORG_META_INFO,
    _ORG_TAG_INSTRUCTIONS,
    _SPLIT_INSTRUCTIONS,
)


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
        batch = unorganized
        for r in batch:
            r["org_state"] = "pending"
            self._org_inflight.add(r["seq"])
        if self.async_organization:
            # 快照在入队瞬间取：它就是"活前缀"——维护调用原样追加指令，
            # 生产环境里这次调用的输入端骑满前缀缓存
            self._org_queue.put((batch, self._assemble_messages()))
            self._ensure_worker()
        else:
            # 同步模式（run 命令/测试）：立即整理，结果随轮次落盘
            try:
                self._parallel_maintenance(batch, self._assemble_messages())
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
            batch = unorganized
            for r in batch:
                r["org_state"] = "pending"
                self._org_inflight.add(r["seq"])
            try:
                org_ok, _split_ok = self._parallel_maintenance(
                    batch, self._assemble_messages()
                )
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
            + _ORG_TAG_INSTRUCTIONS
            + "\n完成后调用 submit_organization 工具提交结果（唯一出口，不要"
            "在正文中输出 JSON）。字段语义以工具定义为准；块 ID 逐字取自"
            "[分块地图]，每个块一条、一个不落、与地图同序。"
        )
        messages = list(base_messages or self._org_fallback_base(rounds))
        messages.append({"role": "user", "content": instruction})

        # org 工具收窄为只留 submit_organization（2026-09-10）：模型会把
        # 整理指令当普通工作对话乱调工具（实测正文说"先重建 HTML 前端"
        # 然后去调 write_file）——收窄工具集是硬约束，比提示词可靠。
        # 牺牲 org 路的前缀缓存（维护调用低频，可接受）。
        org_tools = [
            s for s in self._schemas
            if s.get("function", {}).get("name") == "submit_organization"
        ]
        content, ordered, _usage = self._stream_call(
            messages, tools=org_tools, purpose="organization",
        )
        state = self._extract_org_state(content, ordered)
        staged = self._stage_org_state(
            state, rounds, round_blocks, merged_groups
        )
        if staged == 0:
            # 无可用产物：保持 Runtime 视图（org_state=failed，回入水位
            # 等下次触发），原始层永远不受影响。2026-09-10 用户拍板：
            # 不重试——重试只是再付一遍完整生成，不能确定解决失败
            # （实测两次重试同因失败：输出预算/模型行为不因重试改变）。
            for r in rounds:
                r.pop("pending_org", None)
                r["org_state"] = "failed"
            self._persist_rounds()
            return False, None
        for r in rounds:
            r["org_state"] = "done"
        # 代次打标：最近 3 批视图保留，更早的按文件状态折叠（2026-09-10）
        self._org_generation += 1
        for r in rounds:
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

        # 与 org 同策略：只留 submit_domains（收窄工具集是硬约束）
        # 开思考（不传 thinking disabled）：语义判断需要推理，实测
        # 不开思考不调工具、质量低（用户拍板）。
        split_tools = [
            s for s in self._schemas
            if s.get("function", {}).get("name") == "submit_domains"
        ]
        content, ordered, _usage = self._stream_call(
            messages, tools=split_tools, purpose="split",
        )
        product = self._extract_domains(content, ordered)
        if product is None:
            # 与 org 同策略：不重试（2026-09-10 用户拍板）。但不静默落空
            # （2026-09-11 实测：submit_domains 参数截断/解析失败 → 分裂
            # 无任何痕迹）——落一个保守的"不可分"判定并留痕，账本可查。
            pending = rounds[0].setdefault("pending_org", {})
            pending["split_assessment"] = {
                "splittable": False,
                "reason": "分裂分析无可用产物（submit_domains 参数解析失败/"
                          "截断），保守按不可分处理，不重试",
            }
            if self.task is not None:
                self.task.record(
                    "maintenance",
                    f"split：无可用产物（{len(rounds)} 轮批次），"
                    "已按不可分落档",
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
        只付自己的指令 ~1.5K（实测 org cached=83,968/命中 95%，tools
        收窄不影响 messages 前缀缓存）。**org 失败则 split 跳过**（分裂
        依赖整理质量，失败批次不产出）。
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
            # 分裂分析产物（Level 0：只分析不分裂，落档待查）
            if pending.get("domains"):
                r["domains"] = pending["domains"]
            if pending.get("unassigned"):
                r["unassigned"] = pending["unassigned"]
            if pending.get("split_assessment"):
                r["split_assessment"] = pending["split_assessment"]
            patch = pending.get("state_patch")
            if patch and self.task is not None:
                self.task.apply_state_patch(patch)
        if changed:
            self._persist_rounds()

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
    def _extract_org_state(content: str, ordered: list) -> Optional[dict]:
        """从整理响应提取产物：优先 submit_organization 的调用参数，
        回退消息正文 JSON（自由文本输出兼容，主路径是工具出口）。"""
        for tc in ordered or []:
            if tc.get("name") != "submit_organization":
                continue
            try:
                state = json.loads(tc.get("arguments") or "{}")
            except json.JSONDecodeError:
                continue
            if isinstance(state, dict):
                return state
        return _MaintenanceMixin._parse_state_json(content)

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
