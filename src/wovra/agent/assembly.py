"""Agent 上下文装配与展开：每步装配、紧凑/折叠视图渲染、文件地图、
expand_history 两级展开、紧急折叠。
"""
import json
import re
from typing import Optional
from .. import blocks as blocks_module
from .. import lifecycle as lifecycle_module
from .. import routing as routing_module
from .. import tokens as tokens
from .. import truncate as truncate
from .. import views as views_module
from ..task import _MODEL_SIDE_SECTIONS
from ..tools import eyes as eyes_module
from .support import (
    MODE_BASELINE,
    _STATE_RENDER_BUDGET,
    _runtime_reminder,
)


def _content_tokens(content) -> int:
    """按形态估一条消息内容的 token：文本走分词器，图片按固定口径折算。

    2026-09-13（眼睛）：content 可能是 parts 列表（图片注入）。图片若走
    文本口径，base64 长度会算出天文数字 → 水位误判 → 触发紧急折叠把
    正常历史折掉。故图片一律记 `eyes.TOKENS_PER_IMAGE`。
    """
    if isinstance(content, list):
        total = 0
        for part in content:
            if isinstance(part, dict) and part.get("type") == "image_url":
                total += eyes_module.TOKENS_PER_IMAGE
            else:
                total += tokens.estimate(eyes_module.part_text(part))
        return total
    return tokens.estimate(str(content or ""))


def _content_chars(content) -> int:
    """内容的字符数（图片按其文字说明计——不是 base64 长度）。"""
    return len(eyes_module.content_to_text(content))



class _AssemblyMixin:
    def _assemble_messages(self) -> list[dict]:
        """装配本轮请求的上下文，并记录体量估算（终端展示与窗口保底同口径）。"""
        msgs = self._assemble_messages_impl()
        self.last_context_estimate = self._estimate_messages(msgs)
        # 3a：per-agent 上下文账（每个 agent 自己的体量与窗口占比）——记的是
        # **本轮真实生效的那个视图**（开关关掉时 = 主 agent）。
        self._touch_view_context(self._active_view(), self.last_context_estimate)
        return msgs

    def _assemble_full_messages(self) -> list[dict]:
        """**全量材料装配**（不看 active_view）：维护管线专用（2026-09-12）。

        整理/分裂是**全局**作业——它要处理的正是"所有未整理轮的全部材料"，
        而视图装配（`_assemble_view_messages`）只给得出某一个域那一份料。
        §41 让主 agent 也走视图装配之后，`_maint_snapshot` 继续复用
        `_assemble_messages()` 就等于把维护输入换成了"主 agent 那一桶"：实测
        同一会话 9 条消息 vs 全量 188 条，模型看不到料 → 整理连续失败 → 7 轮
        原文永远挂着、随后以全分辨率灌进各视图（worklog §44，根因起点）。

        代价知情：维护调用不再骑本轮工作调用的前缀缓存（视图分化开启时二者
        前缀本就不同），这笔"一次前缀断裂"已在 `economics.PREFIX_BREAK_TOKENS`
        定价，且它换回的是"整理真的能看到材料"。

        不更新 `last_context_estimate`：水位触发判定继续用"本轮真实装配体量"，
        维护输入口径不参与水位判定（两个口径不可混用，见 worklog §44.3-6）。
        """
        return self._assemble_messages_impl(force_full=True)

    def _assemble_messages_impl(self, *, force_full: bool = False) -> list[dict]:
        """按变化频率排序装配上下文（缓存友好布局）。

        [1] system 人设（静态）
        [2] 历史轮次视图（少变：闭合时成形，之后不可变）
        [3] Task State + 降档轮次的一行索引 + 文件地图（每轮变——放尾部）
        [4] 当前 Round 事件（追加式全量，轮内赦免）

        `force_full=True` 时跳过视图分化分支（见 `_assemble_full_messages`）。
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

        # ---- 视图分化（Level 1 第二/三步）----------------------------
        # 路由到谁，装配就只给谁那份料——**主 agent 不例外**（2026-09-12
        # 用户拍板：「主 agent 就吃自己那份料、基本说不了一句话」）。旧行为
        # （主 agent 拿全量）是"隔离只对子域生效"的不对称态：实测落主 agent
        # 的轮占 69%，这批轮一点不瘦身（worklog §38.4-2 / §40.4 待办甲）。
        # 开关显式关掉（WOVRA_ACTIVE_VIEW=0）时走下面旧路径、逐字节不变。
        view_name = self._active_view()
        if view_name and not force_full and routing_module.active_view_enabled():
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
        # 协议补缝状态：上一已整理轮的收尾 tool_call 原文（见循环内注释）
        prev_compact_tail: Optional[dict] = None
        prev_was_compact = False
        for r in past:
            evs = r.get("events") or []
            if r.get("org_state") == "done":
                # 已整理：紧凑视图（原文+意图+精修索引；细节可 expand_history 取回）
                organized_rounds.append(r)
                if r.get("org_generation", 1) >= keep_min:
                    compact = self._render_compact(r)
                else:
                    compact = self._render_collapsed(r, ledger)
                if compact is not None:
                    view_msgs.append({"role": "user", "content": r["user_input"]["original"]})
                    view_msgs.append({"role": "assistant", "content": compact})
                prev_was_compact = True
                prev_compact_tail = (
                    evs[-1]["message"]
                    if evs and evs[-1]["message"].get("role") == "assistant"
                    and evs[-1]["message"].get("tool_calls")
                    else None
                )
            else:
                # 未整理：原文全量——分辨率损失只允许来自整理，不来自装配
                #
                # 协议补缝（2026-09-12 实测 400）：里程碑检查点把轮闭在
                # "tool_call 已发出、结果落进下一轮"的中间态。前一轮若已
                # 整理成紧凑视图（tool_calls 不在流里）而本轮原样回放且以
                # tool 消息开头，严格端点（DeepSeek）直接 400——补上前一轮
                # 的收尾 tool_call 原文恢复配对；取不到时丢弃开头的孤儿
                # tool 消息（协议优先，原文仍在 Full 存档可 expand 取回）。
                if (prev_was_compact and evs
                        and evs[0]["message"].get("role") == "tool"):
                    if prev_compact_tail is not None:
                        view_msgs.append(prev_compact_tail)
                    else:
                        while evs and evs[0]["message"].get("role") == "tool":
                            evs = evs[1:]
                view_msgs.extend(e["message"] for e in evs)
                prev_was_compact = False
                prev_compact_tail = None

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
        # 当前轮与上一已整理轮之间的同一处补缝（检查点把 tool_call 留在
        # 上轮、结果落在本轮开头的形态）
        cur_msgs = self._current_round_messages()
        if (prev_was_compact and cur_msgs
                and cur_msgs[0].get("role") == "tool"):
            if prev_compact_tail is not None:
                msgs.append(prev_compact_tail)
            else:
                while cur_msgs and cur_msgs[0].get("role") == "tool":
                    cur_msgs = cur_msgs[1:]
        msgs.extend(cur_msgs)
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
        eye = self._eye_image_message()
        if eye is not None:
            # 图片放在**绝对尾部**（在运行时信封之后）：信封每轮变，图片
            # 天然只能在尾部；且它必须在最后一条 user 消息里，模型才把
            # 它当"刚看到的东西"。不放前面 = 不破坏任何已缓存前缀。
            msgs.append(eye)
        return msgs

    def _take_thread_lines(self, view_name: str) -> list[str]:
        """取走并返回**发给本视图的传话**（收件箱投递，送达即清）。

        用户口径（2026-09-12）：传话就像聊天软件——接手方只看到"谁交来的 +
        内容"，看不到对方的历史。故这里只投递收件箱里发给我的条目，并把它
        渲染成一段显式的线程说明，摆在运行时信封的最前面。
        """
        if self.task is None:
            return []
        entry = self._registry_entry_for(view_name)
        if entry is None:
            return []
        pending = [it for it in (entry.get("inbox") or []) if isinstance(it, dict)]
        if not pending:
            return []
        entry["inbox"] = []
        out = ["[传话]（别人交给你的消息，只看到内容、看不到对方的历史）"]
        for item in pending[-8:]:
            out.append(f"- 来自 {item.get('from') or '?'}：{item.get('message') or ''}")
        return out

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
            if r.get("merged_skip"):
                # 合并组成员：组首（anchor）已把组内用户原文合并显示，
                # 成员轮不再单独成段（2026-09-14 修：此前 [R1-2] 后面又跟
                # 一个 [R2]，看起来既重复又乱）
                continue
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
        if str(view_name) == views_module.MAIN_AGENT_ID:
            # 主 agent 起手：把规则层的起点建议摆给它（2026-09-12 用户口径：
            # 每轮都是"主 agent 先触发 → 路由原话 → 子 agent 回复"）。
            block.extend(self._route_hint_lines())
        lines = self._todo_tail_lines()
        if lines:
            # 前置而不是**覆盖**（2026-09-12 修）：视图路径这里原写作
            # `block = list(lines)`，于是只要有待办尾巴就把上面那条
            # `[路由建议]` 丢掉——主 agent 明明有建议却看不到它，与全量
            # 装配路径（`todo_lines + block`）不一致。
            block = list(lines) + block
        # **传话 / 对齐线程**（2026-09-12 用户口径：像聊天软件）：只把"发给我的"
        # 消息摆进来，看不到对方历史；送达即清（收件箱是投递箱，`task.chat`
        # 才是那条公开线程）。放在运行时信封里（绝对尾部），故投递只作废信封
        # 本身，历史前缀不动。
        thread_lines = self._take_thread_lines(view_name)
        if thread_lines:
            block = thread_lines + block
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
        # 整条装配即将收尾、图片放最尾——视图路径的图片暂时不能放这里：
        # 下面还要补协议缝（prev_tail / cur_msgs），会把 user 图片挤到中间。
        # 故把图片留到 return 前追加（见本函数末尾）。
        # 协议补缝（与主装配同款）：检查点轮边界的 tool_call 留在上一轮、
        # 结果落在本轮开头，而视图历史全是 user/assistant 文本对——补上
        # 上一轮的收尾 tool_call 原文，避免 tool 消息悬空被严格端点 400
        cur_msgs = self._current_round_messages()
        handoff: Optional[dict] = None
        if view_name != views_module.MAIN_AGENT_ID:
            # 接手方看到的当前轮里不能留"路由器那一步"（2026-09-12 实测缺陷，
            # 见 `_strip_router_steps`）——那一步的回执会被接手方当成本轮答复。
            cur_msgs, handoff = self._strip_router_steps(cur_msgs)
        prev_tail = None
        if past:
            last_evs = past[-1].get("events") or []
            if (last_evs and last_evs[-1]["message"].get("role") == "assistant"
                    and last_evs[-1]["message"].get("tool_calls")):
                prev_tail = last_evs[-1]["message"]
        if cur_msgs and cur_msgs[0].get("role") == "tool":
            if prev_tail is not None:
                msgs.append(prev_tail)
            else:
                while cur_msgs and cur_msgs[0].get("role") == "tool":
                    cur_msgs = cur_msgs[1:]
        msgs.extend(cur_msgs)
        if handoff is not None:
            # 转交说明放在绝对尾部（runtime-reminder 通道）：接手方因此知道
            # "这一轮为什么在我手上、用户原话在哪、要直接干活"。
            msgs.append(
                _runtime_reminder("\n".join(self._handoff_lines(view_name, handoff)))
            )
        # 图片注入：必须最后一条（协议缝与转交说明都已就位），见 `_eye_image_message`
        eye = self._eye_image_message()
        if eye is not None:
            msgs.append(eye)
        return msgs

    def _strip_router_steps(
        self, msgs: list[dict]
    ) -> tuple[list[dict], Optional[dict]]:
        """接手方视图：剥掉本轮里"路由动作"那几步（2026-09-12 实测缺陷修复）。

        现象（R10 现场，用户报"转交后本轮只回个转交给谁，然后没了"）：主 agent
        调 `route_to` 之后视图确实换了、接手方也确实被调用了，但接手方看到的
        当前轮历史里仍留着主 agent 那次 `route_to` 调用**连它的回执**
        （"已转交 ▨▨……就此停手"）。接手方把回执当成该说的话，本轮只回一句
        "已转交 ▨▨"就闭合——用户的问题得等他下一次提问才被回答。

        路由动作是**路由器那一层的私有动作**，对目标域没有信息价值：用户原话
        已在消息里，转交理由另以运行时信封补上（`_handoff_lines`）。故整条剥掉。
        只在结构干净时动手——同一条 assistant 消息里若还夹着别的工具调用
        （混批），保留原样：宁多留一条，不坏协议结构。

        返回（消息列表, 转交说明 dict 或 None）。
        """
        keep: list[dict] = []
        dropped = 0
        pending_tool = 0
        for m in msgs:
            if pending_tool > 0 and m.get("role") == "tool":
                pending_tool -= 1
                dropped += 1
                continue
            pending_tool = 0
            names = [
                (c.get("function") or {}).get("name")
                for c in (m.get("tool_calls") or [])
            ]
            if names == ["route_to"]:
                pending_tool = 1
                dropped += 1
                continue
            keep.append(m)
        if not dropped:
            return keep, None
        rec = dict((self.current_round or {}).get("route_handoff") or {})
        rec.setdefault("to", "")
        rec.setdefault("reason", "")
        return keep, rec

    def _handoff_lines(self, view_name: str, handoff: dict) -> list[str]:
        """接手方尾部的转交说明（替代被剥掉的路由步骤）。"""
        src = str(handoff.get("from") or "").strip() or "主 agent"
        reason = str(handoff.get("reason") or "").strip()
        head = f"[回合内转交] 本轮由 {src} 转交给你（{view_name}）"
        if reason:
            head += f"——理由：{reason}"
        return [
            head,
            "上一条用户消息就是用户原话（原封转来，未经过转述）——请直接动手把"
            "这一轮干完、把结果回答给用户。不要把这条说明或转交过程复述给用户"
            "（「已转交 ▨▨」之类的话对用户没有信息量），也不要再转回主 agent。",
        ]

    def _view_round_head(self, r: dict, rec: dict, index: dict) -> list[str]:
        """本视图里一轮的轮头（用户原文/意图/约束）+ 该轮用户块。"""
        lines: list[str] = []
        ui = r.get("user_input") or {}
        anchor = r.get("merged_anchor")
        lines.append(f"[{anchor}]" if anchor else f"[R{r.get('seq')}]")
        if ui.get("original"):
            lines.append(f"👤 用户: \"{ui['original']}\"")
        if anchor:
            # 合并组：**组内每一轮的用户原话都保留**（与紧凑视图同口径，
            # 2026-09-14 修：视图路径此前只渲染组首那条，组内其余用户原文丢）
            members = sorted(
                (m for m in self.rounds if m.get("merged_skip") == anchor),
                key=lambda m: int(m.get("seq") or 0),
            )
            for m in members:
                text = str((m.get("user_input") or {}).get("original") or "")
                if text:
                    lines.append(f"👤 用户: \"{text}\"")
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
        # 合并组组首：组的块描述键是 "R1-2-B1" 这种**组 ID**，不在块索引里——
        # 直接按组前缀取（2026-09-14 修：此前只按 own_ids 找，组的合并描述
        # 永远渲染不出来，只剩每轮各自的 digest 兜底行）
        if r.get("merged_anchor"):
            grp = str(r["merged_anchor"])
            group_lines = [f"▸ {bid}: {txt}" for bid, txt in summaries.items()
                           if str(bid).startswith(grp + "-") and txt]
            if group_lines:
                return group_lines
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
        """当前阶段 / 工作项进度的尾部展示（跨轮续跑的工作记忆；空则不占位）。

        词表见 worklog §53：大步 → **阶段**（M{n}），小步 → **工作项**；
        "步"从此只指执行步数。
        """
        todo = (self.task.todo or {}) if self.task is not None else {}
        milestone = todo.get("milestone")
        if not milestone:
            return []
        steps = todo.get("steps") or []
        done_n = sum(1 for s in steps if s["done"])
        stage_id = f"{milestone.get('id')} " if milestone.get("id") else ""
        lines = [
            f"[当前阶段] {stage_id}{milestone['goal']}"
            f"（工作项 {done_n}/{len(steps)}）"
        ]
        if not milestone.get("planned"):
            # 结构闸门是拒绝式反馈，但模型看不到"还没拆"这件事——尾部
            # 用一行提醒把它变成可操作项，避免 verify 时才发现被拒
            lines.append(
                "[待办] 本阶段尚未拆工作项——先 add_item 拆出阶段内的工作项"
            )
        deferred = milestone.get("deferred") or []
        if deferred:
            lines.append(
                f"[挂起人工验收] {len(deferred)} 项，阶段收尾一次性呈交"
            )
        return lines

    def _milestone_map_lines(self, rounds: list[dict]) -> list[str]:
        """轮 ↔ 阶段映射（整理/分裂指令的现状归属辅助）。

        阶段 = 可验收单元（`todo.history` 已验收 + 进行中的 `milestone`），
        带 M{n} 编号与 `{round, event, block}` 锚点。同一阶段的轮通常同属一个
        功能域，映射帮整理/分裂分析判断现状归属；历史条目缺 started_seq
        （迁移前的旧数据）时按"上一阶段闭合轮 +1"推断，保证区间无重叠。
        无阶段记录返回空列表。
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
            label = f"{e.get('id')} " if e.get("id") else ""
            spans.append((start, end, label + str(e.get("goal") or ""), "已验收"))
            prev_end = end
        m = todo.get("milestone")
        if m and m.get("started_seq"):
            start = m["started_seq"]
            if start <= prev_end:
                start = prev_end + 1
            label = f"{m.get('id')} " if m.get("id") else ""
            spans.append((start, None, label + str(m.get("goal") or ""), "进行中"))
        if not spans:
            return []
        lines = [
            "[轮↔阶段映射]（阶段 = 可验收单元，带 M 编号；同一阶段的轮通常同属"
            "一个功能域，辅助现状归属判断）"
        ]
        for r in rounds:
            seq = r["seq"]
            tag = "未进入任何阶段"
            for start, end, goal, status in spans:
                if (end is None and seq >= start) or (
                    end is not None and start <= seq <= end
                ):
                    tag = f"阶段『{goal[:36]}』（{status}）"
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
        # 悬空尾兜底（工具卡即时落盘的伴生保护）：末尾若挂着没等到结果的
        # tool_calls，补合成 tool 结果——严格端点见到无配对的 tool_call 必
        # 400。仅限"重载遗留"的悬空尾：本进程工具批次执行中不修——维护
        # 闸门正是靠"尾部未回复"识别在跑状态并推迟维护，修了会把维护调用
        # 插进正在执行的工具批次中间
        if not getattr(self, "_tools_running", False):
            answered: set = set()
            j = len(msgs) - 1
            while j >= 0 and msgs[j].get("role") == "tool":
                answered.add(msgs[j].get("tool_call_id"))
                j -= 1
            if j >= 0 and msgs[j].get("tool_calls"):
                for tc in msgs[j]["tool_calls"]:
                    if tc.get("id") not in answered:
                        msgs.append({"role": "tool", "tool_call_id": tc["id"],
                                     "content": "（会话在工具执行中被中断，无结果返回）"})
        budget = int(self.context_limit * 0.9)  # 给最终回答留余量
        # 廉价预检：最坏 1 字 ≈ 1 tok（CJK），字符数不超预算必在窗内。
        # 图片走 `_content_chars`（只算文字说明）：它的 base64 长度不是
        # 上下文成本（真实成本按 TOKENS_PER_IMAGE 计），按字符算会误判超窗
        # 而触发紧急折叠——把正常历史折掉，为了几张图。
        total_chars = sum(_content_chars(m.get("content")) for m in msgs)
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
        """估算一组协议消息的 token 数（正文 + 工具调用参数 + 图片）。"""
        total = 0
        for m in msgs:
            total += _content_tokens(m.get("content"))
            for call in m.get("tool_calls") or []:
                total += tokens.estimate(
                    (call.get("function") or {}).get("arguments") or ""
                )
        return total

    def _eye_image_message(self) -> Optional[dict]:
        """当前轮 view_image 引用的图片 → 一条尾部 user 消息（parts 列表）。

        设计依据（2026-09-13 `scripts/probe_vision_channel.py` 实测）：
        * user 消息的 content 数组（text + image_url）**端点接受**，模型真
          答对图像内容；tool 角色的数组 content **被端点 400 拒**
          （`param: messages.2.content`）——所以只能在装配期以 user 注入。
        * 图片不进工具结果、更不进 task.json：真实会话已 16MB 级，一张图
          base64 就是百万字符量级，塞进存档等于把任务文件撑爆。工具结果
          只留一行 `【图片】path=...` 引用（纯文本、可检索）。

        只在**当前轮**扫引用：图片是"刚截刚看"的即时材料，看过即达目的，
        历史轮不再重复注入（省的是真金白银）。重复调用 view_image 会在
        轮内按步重新注入——这正是轮内赦免的语义。
        """
        if self.current_round is None:
            return None
        parts: list[dict] = []
        seen: set[str] = set()
        for event in self.current_round.get("events") or []:
            if event.get("type") != "tool_result":
                continue
            text = event.get("message", {}).get("content")
            if not isinstance(text, str) or eyes_module.EYE_MARKER_RE.search(text) is None:
                continue
            for match in eyes_module.EYE_MARKER_RE.finditer(text):
                rel = match.group(1)
                if rel in seen:
                    continue
                seen.add(rel)
                part = eyes_module.load_image_part(rel)
                if part is not None:
                    parts.append(part)
        if not parts:
            return None
        parts = parts[-eyes_module.MAX_INJECTED_IMAGES:]
        head = (f"[图片]（view_image 送来的 {len(parts)} 张图；图在本消息之后。"
                f"图中文字若要看细节，可再看 read_file/search_files）")
        return {"role": "user", "content": [{"type": "text", "text": head}] + parts}

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
