"""Agent 上下文装配与展开：每步装配、紧凑/折叠视图渲染、文件地图、
expand_history 两级展开、紧急折叠。
"""
import json
from typing import Optional
from .. import blocks as blocks_module
from .. import lifecycle as lifecycle_module
from .. import tokens as tokens
from .. import truncate as truncate
from .support import (
    MODE_BASELINE,
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

        # ---- managed：未整理全量，已整理紧凑视图 --------------------
        # 前缀纪律（2026-09-07 用户拍板）：**只有整理生效才破坏前缀**。
        # 未整理轮次原文全量在上下文里；已整理轮次渲染为紧凑视图——
        # 视图替换只发生在 promotion（新轮开启）那一刻，除此之外装配
        # 严格追加。V2 三档滑窗废除：它每轮在装配中部改写历史，
        # 实测把命中率从 83.7% 砸到 49.9%（R7→R8）。
        state_render = ""
        if self.task is not None:
            state_render = self.task.get_state().render()

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
                "获取现状，通读时按 num_lines=400 连续分段）"
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
        msgs = list(self.messages)
        if self.current_round is None:
            return msgs
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
            blocks = r.get("blocks") or []
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
        """早期轮折叠视图（2026-09-10 用户拍板：视图只保留最近 3 批整理）。

        折叠行 = 👤 原文 + 🎯 意图 + 当前仍 LIVE 的文件的块摘要；其余块
        折叠成一行说明（防止模型误以为该轮只有这些块）。文件状态用装配
        时的实时 ledger（整理时的状态会过期，新会话文件可能重建/删除）。
        两级展开：expand_history 轮号 → 视图（summary 档）→ 原文（full 档）。
        """
        if r.get("merged_skip"):
            return None
        lines = [f"[R{r['seq']}]（折叠）"]
        ui = r["user_input"]
        lines.append(f"👤 用户: \"{ui['original']}\"")
        if ui.get("normalized"):
            lines.append(f"🎯 意图: {ui['normalized']}")
        summaries = r.get("block_summaries") or {}
        # 现场重算 v3 块（不信持久化 v2：旧轮 blocks 是 kind=work，无 file）
        v3_by_id = {b["id"]: b for b in blocks_module.segment_round_by_file(r)}
        kept = []
        for bid, s in summaries.items():
            b = v3_by_id.get(bid)
            if b is None or b.get("kind") != "file":
                continue
            if ledger.state_of(b.get("file", "")) == "live":
                kept.append(f"▸ {bid}: {s}")
        if kept:
            lines.append("（以下块涉及的文件当前仍存活）")
            lines += kept
        folded = len(summaries) - len(kept)
        if folded > 0:
            lines.append(
                f"（其余 {folded} 块已折叠，expand_history 可按轮/块展开）"
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

    @staticmethod
    def _head_text(text: str, limit: int) -> str:
        text = " ".join((text or "").split())
        return text if len(text) <= limit else text[:limit] + "…"

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
        try:
            seq = int(round_id.lstrip("Rr"))
        except ValueError:
            return f"轮次 ID 无效: {round_id}"
        for r in self.rounds:
            if r["seq"] != seq:
                continue
            if level in ("summary", "truncated"):
                lines = [f"[R{seq}] 用户：{r['user_input']['original']}"]
                if r["user_input"].get("normalized"):
                    lines.append(f"意图：{r['user_input']['normalized']}")
                summaries = r.get("block_summaries") or {}
                if summaries:
                    # 视图优先（2026-09-10 折叠行的两级展开：行→视图→原文）
                    lines.append("块视图：")
                    for bid in sorted(
                        summaries, key=lambda x: int(x.rsplit("-B", 1)[-1])
                    ):
                        lines.append(f"▸ {bid}: {summaries[bid]}")
                else:
                    lines += self._round_index_lines(r)
                return "\n".join(lines)
            parts = [f"[R{seq}] 用户：{r['user_input']['original']}"]
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
                (b for b in r.get("blocks") or [] if b["id"] == block_id), None
            )
            if block is None:
                return f"未找到块: {block_id}（该轮无分块结构或块号不存在）"
            parts = [
                f"[{block_id}] {block['start_event']}~{block['end_event']}"
                f"（写: {', '.join(block['wrote_files']) or '无'}）"
            ]
            for i in range(block["start"], block["end"] + 1):
                e = r["events"][i]
                message = e["message"]
                body = message.get("content") or ""
                if message.get("tool_calls"):
                    calls = json.dumps(message["tool_calls"], ensure_ascii=False)
                    body = f"调用: {calls}" + (f"\n{body}" if body else "")
                parts.append(f"--- {e['id']} ({e['type']}) ---\n{body}")
            return "\n".join(parts)
        return f"未找到块: {block_id}"
