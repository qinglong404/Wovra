"""Agent 账本类工具：todo（大步/小步）、notify/consult（跨 agent 通信）、
route_to/switch_view（路由与转交）、submit_organization / submit_domains
（维护管线的提交守卫）。
"""
from typing import Optional

from ..registry import MAIN_AGENT_ID
from .support import _MAX_ROUTE_HOPS


class _LedgerMixin:
    def submit_organization(
        self, rounds: Optional[list] = None, state_patch: Optional[dict] = None
    ) -> str:
        """[守卫] 整理产出的提交工具。

        整理阶段（_organize_rounds）捕获其 tool_call 参数直接解析，不经
        工具执行；这里的方法体只在**工作对话误调用**时跑到——返回说明
        文本，不产生任何副作用。
        """
        return (
            "submit_organization 仅由后台整理流程消费（整理阶段捕获其调用参数，"
            "不经过工具执行）。当前处于工作对话，本次调用被忽略，无副作用。"
        )

    def submit_domains(
        self, domains: Optional[list] = None,
        unassigned: Optional[dict] = None,
        split_assessment: Optional[dict] = None,
    ) -> str:
        """[守卫] 分裂分析的提交工具。

        整理阶段（_split_rounds）捕获其 tool_call 参数直接解析，不经
        工具执行；工作对话误调用只返回说明文本，无副作用。
        """
        return (
            "submit_domains 仅由后台分裂分析消费（分析阶段捕获其调用参数，"
            "不经过工具执行）。当前处于工作对话，本次调用被忽略，无副作用。"
        )

    def todo(
        self,
        action: str,
        goal: str = "",
        acceptance: Optional[list] = None,
        text: str = "",
        evidence: str = "",
        reason: str = "",
    ) -> str:
        """大步/小步计划账本（深度恒 1 的滚动计划）。

        两层是两个维度（2026-09-11 用户拍板）：大步 = 阶段（最小可行 →
        逐步增加功能），小步 = 阶段内的工作拆解——阶段内工作直接做仍然
        复杂，必须拆小步推进。与"平铺 todo（一个任务拆几步）"的本质
        区别即在此，故 verify 设结构闸门：从未拆过小步、或有未完成小步
        未交代 → 拒绝（E/F 组实测 8 个阶段 0 次小步，模型会直接跳过）。

        人工验收两型（2026-09-08 用户拍板）：阻塞型 = 不验收进行不下去，
        停轮等反馈（开放轮语义，\\c 续跑）；非阻塞型（美观等主观项）=
        defer_check 挂起继续干，大步收尾一次性呈交，未决转 experiments
        不搁置。
        """
        if self.task is None:
            return "todo：当前无任务绑定。"
        todo = self.task.todo or {}
        milestone = todo.get("milestone")
        steps = todo.get("steps") or []
        deferred = milestone.get("deferred") or [] if milestone else []

        if action == "start_milestone":
            if milestone:
                return (
                    f"已有开启中的大步：{milestone['goal']}（深度恒 1："
                    "先 verify_milestone 或 drop_milestone，再开新大步）"
                )
            if not goal.strip() or not (acceptance or []):
                return "start_milestone 需要 goal 与 acceptance（验收标准必填——入口是证据不是自述）"
            if len(acceptance) > 3:
                return (
                    f"acceptance 共 {len(acceptance)} 条，超过 3 条硬上限（1-3 条）"
                    "——一个大步只承载一次可验收增量：里程碑驱动轮下验收即轮"
                    "边界，大步越宽轮越晚闭合、整理批次越大（F 组实测 4-6 条"
                    "打包的大步把上下文堆到 325K）。把超出的验收标准拆成下一"
                    "大步，重试 start_milestone。"
                )
            todo["milestone"] = {
                "goal": goal.strip(),
                "acceptance": [str(a) for a in acceptance],
                "started_seq": (
                    self.current_round["seq"]
                    if self.current_round is not None
                    else len(self.rounds) + 1
                ),
                "deferred": [],
                # 是否拆过小步（结构闸门的判据）：verify 前必须为 True。
                # 与 steps 是否为空分开——全部完成后 steps 会被清空/删除，
                # 用 planned 记录"规划过"这一事实，模型 add→drop 全清后
                # 仍可验收（有据可查），但从未拆过会被拒。
                "planned": False,
            }
            todo["steps"] = []
        elif action == "add_step":
            if not milestone:
                return "无开启中的大步——先 start_milestone。"
            if not text.strip():
                return "add_step 需要 text。"
            steps.append({"text": text.strip(), "done": False})
            todo["steps"] = steps
            todo["milestone"]["planned"] = True
        elif action in ("check_step", "drop_step"):
            if not milestone:
                return "无开启中的大步。"
            hit = next((s for s in steps if s["text"] == text.strip()), None)
            if hit is None:
                listing = "\n".join(
                    f"  [{'x' if s['done'] else ' '}] {s['text']}" for s in steps
                ) or "  （空）"
                return f"未找到小步：{text.strip()}\n当前小步：\n{listing}"
            if action == "check_step":
                hit["done"] = True
            else:
                steps.remove(hit)
            todo["steps"] = steps
        elif action == "defer_check":
            if not milestone:
                return "无开启中的大步。"
            if not text.strip():
                return "defer_check 需要 text（待人工验收项）。"
            deferred.append(text.strip())
            todo["milestone"]["deferred"] = deferred
            return (
                f"已挂起人工验收（非阻塞）：{text.strip()}\n"
                f"本大步累积 {len(deferred)} 项，将在 verify_milestone 时一次性呈交；"
                "期间继续工作。"
            )
        elif action == "verify_milestone":
            if not milestone:
                return "无开启中的大步。"
            if not evidence.strip():
                return (
                    "verify_milestone 需要 evidence（验收证据：测试输出/人工确认）"
                    "——禁止自述完成。"
                )
            # 结构闸门（2026-09-11 用户拍板）：大步 = 阶段、小步 = 阶段内
            # 的拆解——阶段内的活直接做仍然复杂，必须先拆。E/F 组实测
            # 8 个阶段 0 次小步：不加门模型会跳过拆解直接闷头做。
            # 判据用 planned（拆过小步这件事）而非 steps 非空——后者在
            # 全部完成/作废后为空，会误伤正常验收。
            if not milestone.get("planned"):
                return (
                    "verify 被拒：本大步还没有拆过小步。大步 = 阶段（最小可行"
                    " → 逐步增加功能），阶段内的工作直接做仍然复杂——先用 "
                    "add_step 拆成能逐步完成、逐步自证的工作项，全部完成后再"
                    "验收（账本要能说明这个阶段做了什么，哪怕只有一条）。"
                )
            undone = [s["text"] for s in steps if not s["done"]]
            if undone:
                listing = "\n".join(f"  [ ] {t}" for t in undone)
                return (
                    f"verify 被拒：还有 {len(undone)} 条未完成小步：\n{listing}\n"
                    "先 check_step 完成它们，或 drop_step 说明为什么不用做了"
                    "（计划可证伪，废弃留痕），再验收。"
                )
            entry = f"[大步] {milestone['goal']}（验收：{evidence.strip()}）"
            self.task.apply_state_patch({"completed": [entry]})
            if deferred:
                # 非阻塞人工验收未决项不搁置：转 experiments（人当传感器
                # 通道），会话收尾一次性呈交
                self.task.apply_state_patch({
                    "experiments": [f"[待人工验收] {t}" for t in deferred]
                })
            todo.setdefault("history", []).append({
                "goal": milestone["goal"],
                "evidence": evidence.strip(),
                "started_seq": milestone.get("started_seq"),
                "closed_seq": len(self.rounds),
            })
            tail_note = (
                f"另有 {len(deferred)} 项非阻塞人工验收已转入待办实验"
                "（未验收不搁置）" if deferred else ""
            )
            todo["milestone"] = None
            todo["steps"] = []
            self.task.todo = todo
            self.task.save()
            # 里程碑驱动轮（2026-09-08 用户拍板）：大步验收 = 检查点 =
            # 轮边界——闭合当前轮并开新轮续写同一回合。D 组实证：一个
            # 229 步巨型轮跑完全程无闭合，水位机制全场未出力。闭合触发
            # 水位检查。说明文本放新轮 user_input 与工具结果（不在
            # assistant tool_call 与 tool 消息之间插事件——严格端点会拒）。
            if self.current_round is not None:
                closed_seq = self.current_round["seq"]
                checkpoint_note = (
                    "[运行时] 大步验收通过，轮次在此闭合"
                    "（里程碑驱动轮：检查点 = 轮边界）。"
                )
                self.close_round()
                if self._open_or_reuse_round(checkpoint_note):
                    self._promote_org_results()
                    # 渐近归属 + 本轮判定（顺序不可颠倒：先归位、再干活）
                    self._settle_and_route(checkpoint_note)
                self._persist_rounds()
                # 轮边界必须在窗口里可见——否则用户体感"一轮"与账本的
                # 多轮对不上（F 组实测：5 次里程碑闭合全程静默）
                if self.on_progress:
                    self.on_progress(
                        f"🏁 大步『{milestone['goal']}』验收闭合——轮次 R{closed_seq} 归档，开新轮续写"
                    )
            return (
                f"大步已验收：{milestone['goal']}\n证据：{evidence.strip()}\n"
                + (tail_note + "\n" if tail_note else "")
                + "轮次已在此闭合并开启新轮（里程碑驱动轮）。"
                "现在可以 start_milestone 写下一大步。"
            )
        elif action == "drop_milestone":
            if not milestone:
                return "无开启中的大步。"
            if not reason.strip():
                return "drop_milestone 需要 reason（作废原因——计划可证伪，留死亡原因）。"
            todo.setdefault("history", []).append({
                "goal": milestone["goal"],
                "evidence": f"作废：{reason.strip()}",
                "started_seq": milestone.get("started_seq"),
                "closed_seq": len(self.rounds),
            })
            if deferred:
                self.task.apply_state_patch({
                    "experiments": [f"[待人工验收·大步作废遗留] {t}" for t in deferred]
                })
            todo["milestone"] = None
            todo["steps"] = []
        elif action == "show":
            if not milestone:
                hist = todo.get("history") or []
                return "无开启中的大步。" + (
                    f"\n已验收 {len(hist)} 个大步。" if hist else ""
                )
            lines = [
                f"当前大步：{milestone['goal']}（自 R{milestone['started_seq']}）",
                "验收标准：\n" + "\n".join(f"  - {a}" for a in milestone["acceptance"]),
            ]
            if steps:
                lines.append("小步：\n" + "\n".join(
                    f"  [{'x' if s['done'] else ' '}] {s['text']}" for s in steps
                ))
            if deferred:
                lines.append("挂起人工验收：\n" + "\n".join(f"  - {t}" for t in deferred))
            return "\n".join(lines)
        else:
            return f"未知动作: {action}"

        self.task.todo = todo
        self.task.save()
        cur = todo.get("milestone")
        if cur:
            done_n = sum(1 for s in todo.get("steps") or [] if s["done"])
            return f"OK：{action}（{cur['goal']}｜小步 {done_n}/{len(todo['steps'])}）"
        return f"OK：{action}"

    def _registry_entry(self, ref: str) -> Optional[dict]:
        """按 id 或名称查注册表条目。"""
        registry = (self.task.registry if self.task is not None else None) or []
        for entry in registry:
            if entry.get("id") == ref or entry.get("name") == ref:
                return entry
        return None

    def list_agents(self) -> str:
        """拉取全部 agent 的职责划分（注册表机械渲染，零 LLM）。

        隔离生效后，这是唯一的跨 agent 公共信息面——主 agent 与任何子
        agent 都只看得到"谁负责什么"，看不到彼此的内容。
        """
        if self.task is None:
            return "list_agents：当前无任务绑定。"
        from ..routing import responsibility_lines

        lines = responsibility_lines(self.task.registry)
        if not lines:
            return "注册表为空（尚未分裂）。当前只有主 agent 承担全部工作。"
        return "[agent 职责表]\n" + "\n".join(lines)

    def route_to(self, agent: str, reason: str) -> str:
        """回合内转交（主 agent 的本职动作，2026-09-12 用户拍板）。

        与 `switch_view` 的区别是**生效时刻**：switch_view 只管下一轮，
        route_to 让目标**在同一个用户回合内**接手把活干完——用户不必等
        两个回合才看到结果，主 agent 也不必替目标复述一遍。

        工具方法体只登记意图（`_pending_route`），实际换视图由 `_work_loop`
        在**工具批次跑完后**执行：批次执行期间改装配会让同批里后面的工具
        看到错乱的上下文（读 A 域文件却按 B 域视图理解）。

        跳数上限（`_MAX_ROUTE_HOPS`）记在轮上：两个域互相踢皮球的链必须
        收口，否则烧光一整轮步数还是没人干活。到顶后拒绝转交，要求就地
        处理或交回用户——宁可让用户看见"没人认领"，也不空转。
        """
        if self.task is None:
            return "route_to：当前无任务绑定。"
        entry = self._registry_entry(agent)
        if entry is None:
            known = "、".join(
                f"{e.get('id')}({e.get('name')})" for e in (self.task.registry or [])
            )
            return f"未找到 agent：{agent}。现存：{known}"
        target = str(entry.get("name") or entry.get("id"))
        if str(entry.get("id")) == MAIN_AGENT_ID or target == MAIN_AGENT_ID:
            return (
                "route_to：目标就是主 agent（你自己）——没有可转的对象，"
                "这一轮直接自己处理。"
            )
        current = self.current_round or {}
        if target == str(current.get("active_view") or ""):
            return f"route_to：本轮已经由 {target} 接手，不要再转给自己。"
        hops = int(current.get("route_hops") or 0)
        if hops >= _MAX_ROUTE_HOPS:
            return (
                f"route_to：本回合已转交 {hops} 次（上限 {_MAX_ROUTE_HOPS}），"
                "不再转交——就地处理，或把情况说明给用户请人指定归属。"
            )
        note = str(reason or "").strip()
        self._pending_route = target
        if self.current_round is not None:
            # 本轮转交说明：接手方装配时用它替代被剥掉的路由步骤（见
            # assembly._strip_router_steps）——谁是上一手、为什么转来。
            self.current_round["route_handoff"] = {
                "from": str(current.get("active_view") or "").strip() or "主 agent",
                "to": target,
                "reason": note,
            }
        entry.setdefault("inbox", []).append({
            "from": f"{MAIN_AGENT_ID}（主agent·路由）",
            "message": f"[转交] {note or '（未给理由）'}",
        })
        self.task.save()
        if self.on_progress:
            self.on_progress(f"🔀 本回合转交 → {entry.get('name')}：{note[:60]}")
        return (
            f"已转交 {entry.get('id')}（{target}）：它在本回合内直接接手"
            "并把结果给用户。**就此停手**——不要再自己动手、不要复述用户的话、"
            "不要写方案或解释；下一步就是它干活。"
        )

    def switch_view(self, agent: str, reason: str) -> str:
        """显式转交：让**下一轮**由目标 agent 接手（本轮上下文不改写）。

        与 notify 的分工：notify 是往对方收件箱塞一条消息（对方激活时
        收到），switch_view 是连"下一轮归谁"一起定下来——路由的最高优先
        判据（`routing.route` 的第一步）。走轮开启时刻这一个切换点，
        故本轮装配纹丝不动（缓存前缀与连贯性都不受影响）。
        """
        if self.task is None:
            return "switch_view：当前无任务绑定。"
        entry = self._registry_entry(agent)
        if entry is None:
            known = "、".join(
                f"{e.get('id')}({e.get('name')})" for e in (self.task.registry or [])
            )
            return f"未找到 agent：{agent}。现存：{known}"
        note = str(reason or "").strip()
        self.task.pending_view = str(entry.get("name") or entry.get("id"))
        entry.setdefault("inbox", []).append({
            "from": "上一轮接手方", "message": f"[转交] {note or '（未给理由）'}",
        })
        self.task.save()
        if self.on_progress:
            self.on_progress(f"🔀 转交 → {entry.get('name')}：{note[:60]}")
        return (
            f"已转交：下一轮起由 {entry.get('id')}（{entry.get('name')}）接活"
            "（本轮上下文不改写）。交接说明已放进它的收件箱。"
        )

    def notify(self, agent: str, message: str) -> str:
        """单向通信（机制五）：转交/通知/交接，只发不等——落目标收件箱，
        对方下次被激活（consult/路由）时送达。"""
        if self.task is None:
            return "notify：当前无任务绑定。"
        entry = self._registry_entry(agent)
        if entry is None:
            known = ", ".join(
                f"{e.get('id')}({e.get('name')})" for e in (self.task.registry or [])
            )
            return f"未找到 agent：{agent}。现存：{known}"
        entry.setdefault("inbox", []).append({
            "from": "主agent", "message": message.strip(),
        })
        self.task.save()
        if self.on_progress:
            self.on_progress(f"📨 单向 → {entry.get('name')}：{message.strip()[:60]}")
        return f"已单向送达 {entry.get('name')} 的收件箱（只发不等，对方激活时收到）。"

    def consult(self, agent: str, question: str) -> str:
        """双向通信（机制五）：发并等回——切到目标职责视角回答一次，
        回复打标签直接流式进用户窗口（不回路由主 agent 转述），同时
        返回给调用方。目标收件箱随激活送达。"""
        if self.task is None:
            return "consult：当前无任务绑定。"
        entry = self._registry_entry(agent)
        if entry is None:
            known = ", ".join(
                f"{e.get('id')}({e.get('name')})" for e in (self.task.registry or [])
            )
            return f"未找到 agent：{agent}。现存：{known}"
        if entry.get("id") == MAIN_AGENT_ID:
            return "不要 consult 主 agent（那就是你自己）——需要用户输入请用 ask_user。"

        system = (
            f"你是 {entry.get('name')}（{entry.get('id')}）——"
            f"{entry.get('description', '')}。"
            f"所有权文件域：{', '.join(entry.get('file_domains') or []) or '未划定'}。"
            "主对话正就以下问题与你对齐：用你的职责视角回答，只答职责内"
            "的内容，简明扼要，不要客套。"
        )
        msgs: list[dict] = [{"role": "system", "content": system}]
        for item in entry.get("inbox") or []:
            msgs.append({
                "role": "user",
                "content": f"[收件箱·来自{item.get('from')}] {item.get('message')}",
            })
        if entry.get("inbox"):
            entry["inbox"] = []  # 已送达
        state = self.task.get_state()
        if state.goal or state.current_status:
            msgs.append({
                "role": "user",
                "content": f"[任务背景] 目标：{state.goal}；现状：{state.current_status}",
            })
        msgs.append({"role": "user", "content": question.strip()})

        # 子 agent 流式展示：思考沿用全局单行；回答打标签直达用户窗口
        base = getattr(self, "_stream_cbs", None) or {}
        think_cb, answer_cb = base.get("thinking"), base.get("answer")
        label = f"[{entry.get('name')}]"
        first = [True]

        def sub_answer(text: str) -> None:
            if answer_cb:
                if first[0]:
                    answer_cb("\n" + label + " ")
                    first[0] = False
                answer_cb(text)

        reply, _ordered, _usage = self._stream_call(
            msgs, tools=None, purpose="working",
            on_thinking=think_cb, on_answer_delta=sub_answer,
        )
        self.task.save()
        return f"{entry.get('name')} 的回复：{reply.strip()}"
