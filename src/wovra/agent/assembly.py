"""Agent 上下文装配与展开：每步装配、紧凑/折叠视图渲染、文件地图、
expand_history 两级展开、紧急折叠。
"""
import json
import re
from typing import Optional
from .. import blocks as blocks_module
from .. import lifecycle as lifecycle_module
from .. import tokens as tokens
from .. import truncate as truncate
from .. import views as views_module
from ..task import _MODEL_SIDE_SECTIONS
from .support import (
    MODE_BASELINE,
    _STATE_RENDER_BUDGET,
    _runtime_reminder,
)


class _AssemblyMixin:
    def _assemble_messages(self) -> list[dict]:
        """装配本轮请求的上下文，并记录体量估算（终端展示与窗口保底同口径）。"""
        msgs = self._assemble_messages_impl()
        self.last_context_estimate = self._estimate_messages(msgs)
        return msgs

    def _assemble_messages_impl(self) -> list[dict]:
        """按变化频率排序装配上下文（缓存友好布局）。

        [1] system 人设（静态）
        [2] 历史轮次视图（少变：闭合时成形，之后不可变）
        [3] Task State + 降档轮次的一行索引 + 文件地图（每轮变——放尾部）
        [4] 当前 Round 事件（追加式全量，轮内赦免）
        """
        past = [r for r in self.rounds if r is not self.current_round]

        if self.context_mode == MODE_BASELINE:
            msgs: list[dict] = []
            persona = self.system_prompt
            if self.task is not None and self.task.baseline_summary:
                # 阈值压缩产生的历史摘要（市面惯例：摘要 + 最近几轮原文）
                persona = (persona + "\n\n[历史压缩摘要]\n" + self.task.baseline_summary).strip()
            if persona:
                msgs.append({"role": "system", "content": persona})
            for r in past:
                if r.get("compacted"):
                    continue  # 已并入压缩摘要
                msgs.extend(e["message"] for e in r["events"])
            msgs.extend(self._current_round_messages())
            return msgs

        # ---- 视图分化（Level 1 第二/三步，2026-09-12）----------------
        # 开关（默认关）打开**且**本轮有非主 agent 的 active_view 时，走
        # 视图装配：四段布局 [system 人设 + 全局职责表] → [本视图历史] →
        # [本视图身份与域卡] → [运行时信封]。隔离第一：非本视图的轮整轮
        # 不出现（不留轮号、文件名、块 ID），故每个视图各自积累长链。
        # 开关关闭或缺省（= 主 agent）时，下面的旧路径**逐字节不变**。
        view_name = self._active_view()
        if view_name and view_name != views_module.MAIN_AGENT_ID:
            msgs = self._assemble_view_messages(view_name, past)
            if msgs is not None:
                return msgs

        # ---- managed：未整理全量，已整理紧凑视图 --------------------
        # 前缀纪律（2026-09-07 用户拍板）：**只有整理生效才破坏前缀**。
        # 未整理轮次原文全量在上下文里；已整理轮次渲染为紧凑视图——
        # 视图替换只发生在 promotion（新轮开启）那一刻，除此之外装配
        # 严格追加。V2 三档滑窗废除：它每轮在装配中部改写历史，
        # 实测把命中率从 83.7% 砸到 49.9%（R7→R8）。
        state_render = ""
        if self.task is not None:
            # 传字符预算：TaskState 的 7 个列表上限合计 1400 条且每轮都进
            # 上下文，无预算即为无界常驻负担（2026-09-11 机制评审）
            # 传 sections：`completed` 退出模型侧（2026-09-11 用户拍板，
            # 见 task._MODEL_SIDE_SECTIONS 注释）——它唯一无限增长且是
            # verify 验收证据的副本（实测占条目文本 62.7%），而 R16 的
            # 按节预算早把它压到 143 tok；人视图/report 不传此参照旧全量。
            state_render = self.task.get_state().render(
                _STATE_RENDER_BUDGET, sections=_MODEL_SIDE_SECTIONS
            )

        msgs: list[dict] = []
        if self.system_prompt:
            msgs.append({"role": "system", "content": self.system_prompt})

        view_msgs: list[dict] = []
        organized_rounds: list[dict] = []
        # 折叠判定（2026-09-10 用户拍板）：视图只保留最近 3 批整理
        # （org_generation 最大的 3 代）；更早的轮按文件当前状态折叠——
        # ledger 现场重算（含当前轮），零 LLM、状态不过期
        ledger = lifecycle_module.FileLedger()
        for rr in past:
            ledger.update(rr, blocks=blocks_module.segment_round_by_file(rr))
        if self.current_round is not None:
            ledger.update(
                self.current_round,
                blocks=blocks_module.segment_round_by_file(self.current_round),
            )
        done_gens = sorted(
            {r.get("org_generation", 1) for r in past
             if r.get("org_state") == "done"}
        )
        keep_min = done_gens[-1] - 2 if done_gens else 0
        for r in past:
            if r.get("org_state") == "done":
                # 已整理：紧凑视图（原文+意图+精修索引；细节可 expand_history 取回）
                organized_rounds.append(r)
                if r.get("org_generation", 1) >= keep_min:
                    compact = self._render_compact(r)
                else:
                    compact = self._render_collapsed(r, ledger)
                if compact is None:
                    continue  # 合并组非组首轮：由组首轮合并显示
                view_msgs.append({"role": "user", "content": r["user_input"]["original"]})
                view_msgs.append({"role": "assistant", "content": compact})
            else:
                # 未整理：原文全量——分辨率损失只允许来自整理，不来自装配
                view_msgs.extend(e["message"] for e in r["events"])

        block = []
        if state_render:
            block.append(state_render)
        file_map = self._file_map_lines(organized_rounds)
        if file_map:
            block.append(
                "[历史涉及文件]（这些轮次的原文已整理收纳，修改前先 read_file "
                "获取现状，通读时用大 num_lines 一次读完）"
            )
            block += file_map
        todo_lines = self._todo_tail_lines()
        if todo_lines:
            block = todo_lines + block

        msgs.extend(view_msgs)
        msgs.extend(self._current_round_messages())
        # 信封绝对尾部（2026-09-08 用户拍板，D 组实证）：todo/TaskState/
        # 文件地图是高频变化状态，放在当前轮事件之前时每次变化都作废其
        # 后全部前缀——D 段1 26 次 todo 变化把命中率砸到 77.5%（段2/3
        # 零变化 99.2-99.5%）。放尾部后状态变化只作废信封本身（~1-2K），
        # 事件追加与状态变化互不破坏；跨轮边界时前缀还能保留到整段历史
        # 末尾。位置仍在响应之前，信息与时机不变，纯缓存布局修正。
        if block:
            # 运行时专属通道（zcode-borrowings.md 1.1）：机制信息用
            # <runtime-reminder> 信封注入，与用户发言语义分离——模型
            # 分得清"用户要的"和"机制给的"（系统提示词里声明该约定）
            msgs.append(_runtime_reminder("\n\n".join(block)))
        return msgs

    def _active_view(self) -> str:
        """本轮生效的视图名（无标记或开关关闭时返回主 agent）。

        缺省 = 主 agent = 今天的装配——这是"步 1 行为零变化"的实现点。
        """
        from .. import routing as routing_module

        if not routing_module.active_view_enabled():
            return views_module.MAIN_AGENT_ID
        if self.current_round is None:
            return views_module.MAIN_AGENT_ID
        name = str(self.current_round.get("active_view") or "").strip()
        return name or views_module.MAIN_AGENT_ID

    def _assemble_view_messages(
        self, view_name: str, past: list[dict]
    ) -> Optional[list[dict]]:
        """按视图装配（纯函数派生，零 LLM）。返回 None 表示降级回旧路径。

        这是"视图物化"的读取侧：视图字节不单独落盘，而是**从材料层确定性
        派生**（同一份 rounds+registry 必得同一串字节，故可随时重建、不会
        损坏），随轮持久化的只有 `active_view` 这一个标记。冻结性来自材料
        的性质——本视图只含自己名下的块，别的域整理生效不会改到它的字节。
        """
        from .. import routing as routing_module

        if self.task is None:
            return None
        domains = views_module.latest_domains(past)
        if not domains:
            return None  # 还没有分裂产物：无机可用，降级回全量装配
        index = views_module.block_index(past)
        owners = views_module.ownership(domains, index)
        hits = views_module.view_blocks_by_round(past, domains, view_name, index, owners)

        msgs: list[dict] = []
        resp = routing_module.responsibility_lines(self.task.registry)
        head = self.system_prompt
        if resp:
            head = (head + "\n\n[全局职责表]（跨 agent 唯一公共信息；"
                    "路由与转交都以此为依据）\n" + "\n".join(resp)).strip()
        if head:
            msgs.append({"role": "system", "content": head})

        # [2] 本视图历史：命中轮才成段（轮头 + 用户块 → user；本域块 → assistant）
        # 分档基准取自**本视图命中轮**（不是全局最新代次）：别的域整理不会
        # 移动这条线，故本视图字节不因他人整理而漂（plan §2/§5.1 不变量）。
        keep_min = views_module.view_keep_min(
            rec["round"] for rec in hits.values()
        )
        for seq in sorted(hits):
            rec = hits[seq]
            r = rec["round"]
            msgs.append({
                "role": "user",
                "content": "\n".join(self._view_round_head(r, rec, index)),
            })
            msgs.append({
                "role": "assistant",
                "content": "\n".join(
                    self._view_round_detail(r, rec, index, keep_min)
                ),
            })
        if not hits:
            msgs.append({
                "role": "user",
                "content": "[本视图历史]（无——本域尚无命中轮）",
            })

        # [3] 身份与域卡
        identity = routing_module.identity_card(view_name, self.task.registry)
        if identity:
            msgs.append({"role": "user", "content": "\n".join(identity)})

        # [4] 运行时信封（绝对尾部）
        block: list[str] = []
        lines = self._todo_tail_lines()
        if lines:
            block = list(lines)
        state = self.task.get_state()
        node = next(
            (d for d in domains if str(d.get("name")) == view_name), None
        )
        sharded = views_module.slice_state(
            state, (node or {}).get("file_domains") or []
        )
        if sharded:
            labels = {
                "goal": "目标", "current_status": "现状", "escalations": "决策升级",
                "experiments": "待办实验", "decisions": "已决策",
                "known_issues": "已知问题", "open_questions": "待解决问题",
            }
            block.append("[本域账本]（按文件域切片；全局节永不切）")
            for field, items in sharded.items():
                block.append(
                    f"{labels.get(field, field)}：" + "；".join(str(x) for x in items)
                )
        if block:
            msgs.append(_runtime_reminder("\n\n".join(block)))
        msgs.extend(self._current_round_messages())
        return msgs

    def _view_round_head(self, r: dict, rec: dict, index: dict) -> list[str]:
        """本视图里一轮的轮头（用户原文/意图/约束）+ 该轮用户块。"""
        lines: list[str] = []
        ui = r.get("user_input") or {}
        anchor = r.get("merged_anchor")
        lines.append(f"[{anchor}]" if anchor else f"[R{r.get('seq')}]")
        if ui.get("original"):
            lines.append(f"👤 用户: \"{ui['original']}\"")
        if ui.get("normalized"):
            lines.append(f"🎯 意图: {ui['normalized']}")
        if ui.get("key_constraints"):
            lines.append(f"📌 关键约束: {ui['key_constraints']}")
        for bid in rec.get("user_ids") or []:
            item = index.get(bid)
            if item is None:
                continue
            text = self._block_user_text(item)
            lines.append(f"▸ {bid}（用户补充输入）: {text}" if text else f"▸ {bid}（用户补充输入）")
        return lines

    def _view_round_detail(
        self, r: dict, rec: dict, index: dict, keep_min: Optional[int] = None
    ) -> list[str]:
        """本域块：已整理的给一行全分辨率描述，未整理的给原文（可 expand 取回）。

        分辨率损失只允许来自整理，不来自装配——未整理的块在这里直接摊开
        原文，与今天的装配口径一致。

        **分档（2026-09-12）**：早于 `keep_min`（本视图自己最近 3 批整理）
        的轮只给「文件名清单 + N 块已折叠」，与装配二级折叠同口径。折叠只改
        渲染文本、不改归属——块 ID 仍在 `own_ids` 里，按轮号 `expand_history`
        可展开本轮取回原文。
        """
        summaries = r.get("block_summaries") or {}
        if views_module.is_folded(r, keep_min):
            return views_module.folded_block_lines(r, rec, index)
        lines: list[str] = []
        for bid in rec.get("own_ids") or []:
            item = index.get(bid)
            if item is None:
                continue
            if summaries.get(bid):
                lines.append(f"▸ {bid}: {summaries[bid]}")
            else:
                lines.append(self._expand_block(bid))
        return lines or ["（本视图无本域块）"]

    def _block_user_text(self, item: dict) -> str:
        """块内的用户输入原文（user 块用；取块自带 events 的内容）。"""
        r = item.get("round") or {}
        by_id = {e.get("id"): e for e in (r.get("events") or [])}
        block = item["block"]
        chosen = block.get("events") or [
            e.get("id") for e in (r.get("events") or [])
        ][block.get("start", 0): block.get("end", 0) + 1]
        parts: list[str] = []
        for eid in chosen:
            e = by_id.get(eid)
            if not e:
                continue
            body = str((e.get("message") or {}).get("content") or "").strip()
            if body:
                parts.append(body)
        return " ".join(parts).strip()

    def _todo_tail_lines(self) -> list[str]:
        """当前大步/小步进度的尾部展示（跨轮续跑的工作记忆；空则不占位）。"""
        todo = (self.task.todo or {}) if self.task is not None else {}
        milestone = todo.get("milestone")
        if not milestone:
            return []
        steps = todo.get("steps") or []
        done_n = sum(1 for s in steps if s["done"])
        lines = [f"[当前大步] {milestone['goal']}（小步 {done_n}/{len(steps)}）"]
        if not milestone.get("planned"):
            # 结构闸门是拒绝式反馈，但模型看不到"还没拆"这件事——尾部
            # 用一行提醒把它变成可操作项，避免 verify 时才发现被拒
            lines.append("[待办] 本大步尚未拆小步——先 add_step 拆出阶段内工作项")
        deferred = milestone.get("deferred") or []
        if deferred:
            lines.append(f"[挂起人工验收] {len(deferred)} 项，大步收尾一次性呈交")
        return lines

    def _milestone_map_lines(self, rounds: list[dict]) -> list[str]:
        """轮 ↔ 大步映射（整理/分裂指令的现状归属辅助）。

        大步 = 可验收单元（todo.history 已验收 + 在飞 milestone）。同一
        大步的轮通常同属一个功能域，映射帮整理/分裂分析判断现状归属；
        历史条目缺 started_seq（迁移前的旧数据）时按"上一个大步闭合轮
        +1"推断，保证区间无重叠。无大步记录返回空列表。
        """
        todo = (self.task.todo or {}) if self.task is not None else {}
        spans: list[tuple[int, Optional[int], str, str]] = []
        prev_end = 0
        for e in todo.get("history") or []:
            end = e.get("closed_seq")
            if end is None:
                continue
            start = e.get("started_seq")
            if not start or start <= prev_end:
                start = prev_end + 1
            spans.append((start, end, str(e.get("goal") or ""), "已验收"))
            prev_end = end
        m = todo.get("milestone")
        if m and m.get("started_seq"):
            start = m["started_seq"]
            if start <= prev_end:
                start = prev_end + 1
            spans.append((start, None, str(m.get("goal") or ""), "进行中"))
        if not spans:
            return []
        lines = [
            "[轮↔大步映射]（大步 = 可验收单元；同一大步的轮通常同属一个功能域，"
            "辅助现状归属判断）"
        ]
        for r in rounds:
            seq = r["seq"]
            tag = "未进入大步"
            for start, end, goal, status in spans:
                if (end is None and seq >= start) or (
                    end is not None and start <= seq <= end
                ):
                    tag = f"大步『{goal[:36]}』（{status}）"
                    break
            lines.append(f"  R{seq} ← {tag}")
        return lines

    def _current_round_messages(self) -> list[dict]:
        """轮内赦免：当前 Round 事件全量进入上下文，不做任何内容截断。

        执行期截断曾两次被实测证明适得其反：折叠诱发"读 → 失忆 →
        重读"死循环（39 次 read_file 烧穿 40 步上限）；2000 字符
        安全截断把模型刚读到的文件内容挡在上下文外。唯一的例外是
        模型窗口本身：估算超过 context_limit 时，把最老的事件折叠
        为索引行直到回线——最后手段，正常任务永远碰不到。
        """
        # 没有进行中的轮：当前轮消息为空。**不能**返回残留的
        # self.messages——close_round 会把 current_round 置空但不清理
        # self.messages（它要到下一轮开启才重置），于是"刚闭合的那一轮"
        # 会同时以事件（历史）和残留消息（当前轮）出现两遍。实测
        # （worklog-20260911.md §11）：整理快照因此 497 条而非 422 条，
        # 每批白烧 ~18K tok，且组织器把最后一轮看两遍。
        if self.current_round is None:
            return []
        msgs = list(self.messages)
        events = self.current_round["events"]
        if len(events) != len(msgs):
            return msgs  # 结构对不上时不动手（宁超限，不坏数据）
        budget = int(self.context_limit * 0.9)  # 给最终回答留余量
        # 廉价预检：最坏 1 字 ≈ 1 tok（CJK），字符数不超预算必在窗内
        total_chars = sum(len(str(m.get("content") or "")) for m in msgs)
        if total_chars <= budget:
            return msgs
        sizes = [self._estimate_messages([m]) for m in msgs]
        total = sum(sizes)
        if total <= budget:
            return msgs
        # 每条索引行按 150 tok 保守计价（120 字符 CJK 的上界），宁多折不少折
        fold, kept = 0, total
        while fold < len(events) - 1 and kept + fold * 150 + 200 > budget:
            kept -= sizes[fold]
            fold += 1
        lines = [truncate.event_index_line(e) for e in events[:fold]]
        block = {"role": "user", "content": (
            f"[紧急折叠：当前轮上下文估算已超模型窗口（{self.context_limit:,} tok），"
            f"最老 {fold} 条事件折叠为索引；需要细节可用 expand_history 按事件 ID 展开]\n"
            + "\n".join(lines)
        )}
        return [block] + list(msgs[fold:])

    @staticmethod
    def _estimate_messages(msgs: list[dict]) -> int:
        """估算一组协议消息的 token 数（正文 + 工具调用参数）。"""
        total = 0
        for m in msgs:
            total += tokens.estimate(str(m.get("content") or ""))
            for call in m.get("tool_calls") or []:
                total += tokens.estimate(
                    (call.get("function") or {}).get("arguments") or ""
                )
        return total

    def _render_compact(self, r: dict) -> Optional[str]:
        """已整理轮次的紧凑视图：👤用户原文 + 🎯意图 + 📌关键约束 + 逐块细节。

        整理生效后轮次以此形态常驻上下文——它是"水位折叠"的落点，
        细节永不丢失（expand_history 按块 ID/事件 ID 取回原文）。
        2026-09-08 用户拍板：块描述承载完整细节；2026-09-09 修订为信息量
        自适应（低信息量块两三行即可）。轮首保留第一版视图的
        用户意图三行式。无块描述的旧整理轮回退到精修事件索引。
        2026-09-10 合并显示：连续纯聊天轮合并为组——非组首轮返回
        None（调用方跳过，不产生视图），组首轮头部显示 [R1-2] 锚点。
        """
        if r.get("merged_skip"):
            return None
        ui = r["user_input"]
        if r.get("merged_anchor"):
            anchor = r["merged_anchor"]
            lines = [f"[{anchor}]"]
            # 合并组：组内所有轮的用户原文都保留（R1-2 里 R2 的 👤 不能丢）
            seqs = [r["seq"]]
            seqs += [
                rr["seq"] for rr in self.rounds
                if rr.get("merged_skip") == anchor
            ]
            seqs.sort()
            for seq in seqs:
                rr = next(x for x in self.rounds if x["seq"] == seq)
                lines.append(f"👤 用户: \"{rr['user_input']['original']}\"")
        else:
            lines = [f"[R{r['seq']}]"]
            lines.append(f"👤 用户: \"{ui['original']}\"")
        if ui.get("normalized"):
            lines.append(f"🎯 意图: {ui['normalized']}")
        if ui.get("key_constraints"):
            lines.append(f"📌 关键约束: {ui['key_constraints']}")
        summaries = r.get("block_summaries") or {}
        if summaries:
            lines.append("块细节：")
            # 块结构**现场重算 v3**（segment_round_by_file）——与整理产物同源。
            # 不能用 r["blocks"]：那是 close_round 落的 v1 粗分块，两套分块器
            # 编号空间相同（R{n}-B{k}）但切法不同（v1 以写/改为截止，无写轮整轮
            # 一块；v3 按文件聚合）。按 v1 遍历 + 用 v3 键查表会**静默丢描述**：
            # 实测本会话 R1（纯读轮）v1=1 块 / v3=24 块，24 条描述只有 1 条进了
            # 上下文，另外 23 条（逐个文件读完后的知识沉淀）永不显示。
            # 这也让视图里的块 ID 与 expand_history 的重算口径一致（同 v3）。
            blocks = blocks_module.segment_round_by_file(r)
            if blocks:
                for b in blocks:
                    s = summaries.get(b["id"])
                    if s:
                        lines.append(f"▸ {b['id']}: {s}")
            else:
                for bid in sorted(summaries, key=lambda x: int(x.rsplit("-B", 1)[-1])):
                    lines.append(f"▸ {bid}: {summaries[bid]}")
        else:
            idx = self._round_index_lines(r)
            if idx:
                lines.append("事件索引：")
                lines += idx
        return "\n".join(lines)

    def _render_collapsed(self, r: dict, ledger) -> Optional[str]:
        """早期轮二级折叠视图（2026-09-12 用户拍板：只保留最近几批的细节）。

        折叠行 = 👤 原文 + 🎯 意图 + 📌 关键约束 + **涉及文件名清单**。
        块摘要**不再进折叠档**——块细节只由最近 keep_min 代（紧凑档）承载，
        更老的轮"要了再取"（expand_history 轮号 → summary 档 → full 档）。

        为什么改（2026-09-12 实测）：旧实现保留"文件仍存活"的块的完整摘要，
        而真实会话里多数文件长期 live，于是折叠档**单调膨胀**——实测本会话
        24 个折叠轮 46,782 tok，其中 80%（33,938 tok）是块摘要，把装配地板
        顶到 101,826 tok、超过 100,000 水位线：压缩刚结束就已在触发线之上，
        冷却一到期立刻再压，压了等于没压。二级折叠后折叠档降到 ~10.7K tok。

        文件状态用装配时的实时 ledger（整理时的状态会过期，新会话文件可能
        重建/删除）；已删文件标「已删」但仍列出——清单是"这轮碰过什么"的
        索引，细节缺失由状态标注补位，比整块消失好。
        """
        if r.get("merged_skip"):
            return None
        lines = [f"[R{r['seq']}]（折叠）"]
        ui = r["user_input"]
        lines.append(f"👤 用户: \"{ui['original']}\"")
        if ui.get("normalized"):
            lines.append(f"🎯 意图: {ui['normalized']}")
        if ui.get("key_constraints"):
            lines.append(f"📌 关键约束: {ui['key_constraints']}")
        # 现场重算 v3 块（不信持久化：旧轮 blocks 可能是 v1 粗分块）
        files: list[str] = []
        for b in blocks_module.segment_round_by_file(r):
            if b.get("kind") != "file":
                continue
            f = str(b.get("file") or "")
            if f and f not in files:
                files.append(f)
        if files:
            parts = [
                f"{f}（已删）" if ledger.state_of(f) == "dead" else f
                for f in files
            ]
            lines.append("涉及文件：" + "、".join(parts))
        folded = len(r.get("block_summaries") or {})
        if folded:
            lines.append(
                f"（{folded} 块细节已折叠，expand_history 可展开本轮；"
                "需要文件现状先 read_file）"
            )
        return "\n".join(lines)

    def _round_index_lines(self, r: dict) -> list[str]:
        """事件的索引行：优先用精修索引，未整理的事件用 Runtime 截断行。"""
        refined = r.get("refined_index") or {}
        out = []
        for e in r["events"]:
            line = refined.get(e["id"]) or e["truncated"]
            status = f"[{e['status']}] " if e.get("status") else ""
            out.append(f"[{e['id']}] {status}{line}")
        return out

    def _file_map_lines(self, rounds: list[dict]) -> list[str]:
        """把已整理轮次里出现过的文件整理成一张"文件地图"。

        文件内容随轮次降档后，模型曾经只能盲目分片重爬（实测一个
        轮里 39 次 read_file 重读同一文件）。地图只给"哪些文件、
        在哪些轮被写过/读过"，指引精准定位，不携带内容成本。
        """
        touched: dict[str, dict[str, list[int]]] = {}
        for r in rounds:
            for e in r.get("events", []):
                if e["type"] != "tool_call":
                    continue
                for tc in e["message"].get("tool_calls", []) or []:
                    fn = tc.get("function", {})
                    if fn.get("name") not in ("write_file", "edit_file", "read_file"):
                        continue
                    try:
                        path = json.loads(fn.get("arguments") or "{}").get("path")
                    except ValueError:
                        continue
                    if not path:
                        continue
                    info = touched.setdefault(path, {"w": [], "r": []})
                    if fn["name"] == "read_file":
                        info["r"].append(r["seq"])
                    else:
                        info["w"].append(r["seq"])
        lines = []
        for path, info in touched.items():
            parts = []
            if info["w"]:
                wrote = "R" + ",R".join(dict.fromkeys(map(str, info["w"])))
                parts.append(f"写于 {wrote}")
            if info["r"]:
                read = "R" + ",R".join(dict.fromkeys(map(str, info["r"])))
                parts.append(f"读于 {read}")
            lines.append(f"- {path}（{'；'.join(parts)}）")
        return lines

    def expand_history(self, ids: list[str] | str, level: str = "full") -> str:
        """按需展开历史：Truncated → Summary（意图+索引）→ Full 三档读取。

        ids 可为轮（"R3"）、块（"R3-B2"，取回整块原文）或事件（"R3-E02"），
        容错逗号字符串与大小写；一次可传多个，无调用次数上限。展开只是
        临时把更高分辨率的信息读进当前上下文，不修改历史。
        """
        if isinstance(ids, str):
            ids = [s.strip() for s in ids.split(",") if s.strip()]
        level = (level or "full").strip().lower()
        if level not in ("truncated", "summary", "full"):
            return f"未知级别: {level}，可选 truncated / summary / full"
        results = []
        for rid in ids:
            if "-B" in rid:
                results.append(self._expand_block(rid))
            elif "-E" in rid:
                results.append(
                    self._read_full_event(rid) if level == "full" else self._event_summary(rid)
                )
            else:
                results.append(self._expand_round(rid, level))
        return "\n\n".join(results) or "未找到任何 ID"

    def _event_summary(self, event_id: str) -> str:
        for r in self.rounds:
            refined = r.get("refined_index") or {}
            for e in r["events"]:
                if e["id"] == event_id:
                    line = refined.get(event_id) or e["truncated"]
                    return f"[{event_id}] {line}"
        return f"未找到事件: {event_id}"

    def _expand_round(self, round_id: str, level: str) -> str:
        # 合并组锚点（如 "R1-2"）：紧凑视图里整组显示为一个锚点，模型会
        # 自然地按这个 ID 展开——逐轮拼出组内全部轮次。
        group = re.match(r"^[Rr](\d+)-(\d+)$", round_id.strip())
        if group:
            lo, hi = int(group.group(1)), int(group.group(2))
            members = [r for r in self.rounds if lo <= r["seq"] <= hi]
            if not members:
                return f"未找到轮次: {round_id}"
            return "\n\n".join(
                self._expand_round(f"R{r['seq']}", level) for r in members
            )
        try:
            seq = int(round_id.lstrip("Rr"))
        except ValueError:
            return f"轮次 ID 无效: {round_id}"
        for r in self.rounds:
            if r["seq"] != seq:
                continue
            lines = [f"[R{seq}] 用户：{r['user_input']['original']}"]
            if r["user_input"].get("normalized"):
                lines.append(f"意图：{r['user_input']['normalized']}")
            if level in ("summary", "truncated"):
                summaries = r.get("block_summaries") or {}
                if summaries and level == "summary":
                    # 视图档（2026-09-10 折叠行的两级展开：行→视图→原文）
                    lines.append("块视图：")
                    for bid in sorted(
                        summaries, key=lambda x: int(x.rsplit("-B", 1)[-1])
                    ):
                        lines.append(f"▸ {bid}: {summaries[bid]}")
                else:
                    # 截断档：只给事件索引行；未整理轮无块视图也用索引
                    lines += self._round_index_lines(r)
                return "\n".join(lines)
            parts = lines
            for e in r["events"]:
                if e["type"] == "user":
                    continue
                parts.append(
                    f"--- {e['id']} ({e['type']}) ---\n"
                    + (e.get("full") or e["message"].get("content") or "")
                )
            return "\n".join(parts)
        return f"未找到轮次: {round_id}"

    def _expand_block(self, block_id: str) -> str:
        """按块 ID（如 R9-B14）取回该块全部事件的原文（紧凑视图的回放通道）。

        紧凑视图 2026-09-08 起以块描述为主索引、事件行退场，块 ID 是
        视图里唯一保留的定位锚——expand_history 必须认得它。

        块结构**现场重算 v3**（segment_round_by_file）：整理产物里的块号
        来自同一函数，而持久化的 `r["blocks"]` 是旧的 v1 编号——实测
        103 轮里 42 轮两者编号不一致（v3 按文件聚合、v1 按写操作截止），
        用持久化编号会出现"块号不存在"或取到别的块。事件按块自带的
        `events` 列表取（v3 块的 index 区间会重叠：一次并行写多文件时
        同一个 tool_call 事件属多个块）。
        """
        head = block_id.lstrip("Rr").split("-B")[0]
        try:
            seq = int(head)
        except ValueError:
            return f"块 ID 无效: {block_id}"
        for r in self.rounds:
            if r["seq"] != seq:
                continue
            block = next(
                (b for b in blocks_module.segment_round_by_file(r)
                 if b["id"] == block_id), None
            )
            if block is None:
                # 极老数据回退：仍认持久化的 v1 块
                block = next(
                    (b for b in r.get("blocks") or [] if b["id"] == block_id),
                    None,
                )
            if block is None:
                return f"未找到块: {block_id}（该轮无分块结构或块号不存在）"
            target = block.get("file") or ", ".join(block.get("wrote_files") or [])
            parts = [
                f"[{block_id}] {block['start_event']}~{block['end_event']}"
                f"（{target or '无文件'}）"
            ]
            if block.get("events"):
                by_id = {e["id"]: e for e in r["events"]}
                selected = [by_id[i] for i in block["events"] if i in by_id]
            else:
                selected = r["events"][block["start"]:block["end"] + 1]
            for e in selected:
                message = e["message"]
                body = message.get("content") or ""
                if message.get("tool_calls"):
                    calls = json.dumps(message["tool_calls"], ensure_ascii=False)
                    body = f"调用: {calls}" + (f"\n{body}" if body else "")
                parts.append(f"--- {e['id']} ({e['type']}) ---\n{body}")
            return "\n".join(parts)
        return f"未找到块: {block_id}"
