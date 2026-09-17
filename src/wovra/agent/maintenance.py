"""Agent 维护管线：水位批量整理（organization）、分裂分析（split）、
暂存与 promote、baseline 阈值压缩。整理 = 一次追加式对话，分裂是它的
纯追加延续。
"""
import json
import os
import re
import threading
import time
from typing import Optional
from .. import blocks as blocks_module
from .. import pathmatch as pathmatch_module
from .. import economics as economics_module
from .. import lifecycle as lifecycle_module
from .. import registry as registry_module
from .. import split_lifecycle as split_lifecycle_module
from .. import views as views_module
from .. import tokens as tokens
from .. import truncate as truncate
from ..tools import safety as safety_module
from ..task import _PATCH_LIST_FIELDS
from . import note as note_module
from .support import (
    MODE_MANAGED,
    v4_enabled,
    _COMPRESS_THRESHOLD,
    _FOLD_KEEP_ROUNDS_DEFAULT,
    _NOTE_TIMEOUT_DEFAULT,
    _clip_quote,
    maint_tools,
)
from .prompts import (
    _ORG_META_INFO,
    _ORG_FORMAT_DISCIPLINE,
    _ORG_TAG_INSTRUCTIONS,
    _ROUND_NOTE_INSTRUCTION,
    _SPLIT_INSTRUCTIONS,
    _SPLIT_LIVE_INSTRUCTIONS,
)


_ORG_SUBMIT_TOOL = "submit_organization"

_SPLIT_SUBMIT_TOOL = "submit_domains"

_ROUND_NOTE_SUBMIT = "submit_round_note"



def _scan_value_end(text: str, start: int) -> int:
    """从 text[start]（应为 `{` 或 `[`）扫到配对收尾的下标；被截断（扫到结尾）返回 -1。

    字符串/转义感知——产物里带 `}` 的路径、JSON 里的引号都不会骗过它。
    """
    stack: list[str] = []
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append(ch)
        elif ch in "}]":
            if not stack:
                return -1
            stack.pop()
            if not stack:
                return i
    return -1


def _salvage_state_json(text: str) -> dict | None:
    """**截断/坏 JSON 的抢救式解析**（2026-09-15）。

    分裂/整理产物动辄一两万字符，模型输出撞端点预算就被截断 → `json.loads` 失败。
    这里**逐个元素**抠：`domains` 数组里写完的节点照收、丢掉没写完的尾巴；
    `unassigned` / `split_assessment` / `responsibilities` 按对象抠。

    ⚠ **用途已变**（2026-09-15 用户拍板"错误分裂不如不分裂"）：抢救结果**不再**
    拿去落地——残树落下去就是错的域树与错的 agent。现在它只用来**诊断与报告**
    （`_extract_domains` 数一下"抢救出几个节点"就丢弃产物、本次不分裂）。
    """
    import json as _json

    src = str(text or "")
    out: dict = {}
    for key in ("domains", "unassigned", "split_assessment", "responsibilities"):
        m = re.search(r'"' + key + r'"\s*:\s*([\[{])', src)
        if not m:
            continue
        start = m.start(1)
        end = _scan_value_end(src, start)
        if end > 0:
            try:
                out[key] = _json.loads(src[start:end + 1])
                continue
            except ValueError:
                pass
        if m.group(1) != "[":
            # 截断的**对象**：职责表（`{"节点名": "职责", …}`）走这条——逐个抠
            # 写完整的键值对，丢掉没写完的尾巴。它是产物的**最后一节**，于是
            # "被截断"只损失几条描述（描述本就有文件开头兜底），树完好无损。
            pairs: dict = {}
            for pm in re.finditer(
                    r'"((?:[^"\\]|\\.)*)"\s*:\s*"((?:[^"\\]|\\.)*)"', src[start:]):
                try:
                    pairs[_json.loads('"' + pm.group(1) + '"')] = _json.loads(
                        '"' + pm.group(2) + '"')
                except ValueError:
                    continue
            if pairs:
                out[key] = pairs
            continue
        # 截断的数组：逐个抠完整的元素
        items: list = []
        i = start + 1
        while i < len(src):
            j = src.find("{", i)
            if j < 0:
                break
            e = _scan_value_end(src, j)
            if e < 0:
                break
            try:
                items.append(_json.loads(src[j:e + 1]))
            except ValueError:
                pass
            i = e + 1
        if items:
            out[key] = items
    return out or None


class SplitCoverageError(RuntimeError):
    """分裂漏认领：有文件块没有任何域要它（用户口径 2026-09-13）。

    "现在如果分裂完主 agent 有文件，直接给我报错，不要往下进行了。"
    这类失败**必须约束住**（不只是记一笔）：产物不 promote、本批轮回入水位下批重做，
    否则轮已被整理标 `done`、分裂再也不会重跑——"主 agent 有文件"就永远留在那儿。
    """


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
        删除，触发只看水位；最老的先整理）。本函数只负责"把产物算出来并
        暂存"；**生效点是轮闭合边界**（`close_round` → `_settle_after_
        maintenance`，§50 用户口径：「分裂之后、下一轮对话之前」就落地）。
        过程对用户静默。
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
        if last_maintained and current_seq - last_maintained <= self._org_cooldown:
            # 最小间隔（2026-09-12 用户口径更正：**最少 N 轮内不触发**）。
            # 原实现是 `<`，即"距上次维护满 3 轮就放行"——只安静 2 轮
            # （R35 整理完 → R36 挡、R37 挡、R38 放行），与用户说的
            # "最少 3 轮内不触发"差一轮；且同一份代码里宽限用的是 `<=`
            # （R1-R3 豁免、R4 才放行），两个门不等式方向不一致本属笔误。
            # 改 `<=` 后 R35 整理完 → R36/R37/R38 都挡、R39 才放行。
            #
            # 口径只认"真正维护过"：done（完成）或本进程在飞（pending
            # 且 seq ∈ _org_inflight）。崩溃遗留的 pending（上个进程维护
            # 线程被中断的半程状态）不算已维护——否则间隔把它们当刚维护
            # 过，挡掉本次会话第一次补整理（F 组实证：6 轮 pending 续跑，
            # R7/R8 闭合被连挡，压缩迟迟不开始）。
            return  # 两次维护之间至少安静 _org_cooldown 轮，防高频
        if v4_enabled():
            # V4：叙事已由每轮一段话承担（``_settle_round_note``），水位只驱动
            # **结构树**——不再跑整理那一路（org 那套成为历史）。
            self._maybe_split_v4()
            return
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
            # 注：这里**不**顺手 promote——生效点是 `close_round` 边界（见
            # `_settle_after_maintenance`）：语义上"轮闭合 → 产物即时生效 →
            # 下一轮开场可直接对话"，而本函数只负责"把产物算出来并暂存"。

    def organize_backlog(self, *, force: bool = False) -> None:
        """整理未整理轮（同步）。

        两种语义（2026-09-12 分岔，为一个实测缺陷）：

        * `force=True`：**run 模式进程收尾**用——无视水位/宽限/冷却，退出前把
          账补齐（TaskState 才能跟得上下一次自主推进）。一次性进程没有"下一次
          轮闭合"，所以那时唯一的时机就是退出前。
        * 默认（`force=False`）：**走与 `_maybe_organize_batch` 同一套水位闸门**
          （水位 + 宽限 + 冷却）。

        为什么要分岔（现场：会话 20260912-151325-22671f）：C4 网页输入在**每一轮
        之后**调本函数（`serve._run_turn`），而旧实现无视水位 → 网页里"只聊一轮
        就被整理并压缩"：上下文峰值 1.7 万、水位线 10 万，org+split 双调用照样
        发出去（org prompt 22,776 + split 26,310 tok，miss 19,134），且每轮都打断
        一次前缀缓存。CLI 侧的两道门（水位 + 宽限 3 轮）根本不可能在 R1 放行——
        故触发只能来自这里。默认走闸门后，`serve` 侧的调用无需改动即变安全。
        """
        if self.context_mode != MODE_MANAGED or self.task is None:
            return
        if not force:
            # 水位检查本身就是"够不够格整理"的唯一判据（含宽限与冷却），
            # 故不复制一份条件，直接委托——两处口径不可能再走岔。
            self._maybe_organize_batch()
            return
        try:
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
                except Exception as error:  # noqa: BLE001——收尾整理失败不阻塞任务退出
                    # **把异常原文记下来**（2026-09-13 用户口径："直接给我报错"）：
                    # 原先只把轮标成 failed、异常本身一声不响——必须让人看见的失败
                    # （如"文件块没有域认领"）就沉进状态里了。
                    if self.task is not None:
                        self.task.record(
                            "maintenance",
                            f"整理管线异常中止：{type(error).__name__}: {str(error)[:180]}",
                        )
                        r.pop("pending_org", None)
                    for r in batch:
                        r["org_state"] = "failed"
                    self._persist_rounds()
                    return
                finally:
                    for r in batch:
                        self._org_inflight.discard(r["seq"])
                if not org_ok:
                    return
        finally:
            # 退出前把产物落到位（§50）：run 模式下一次自主推进开局就直接是
            # "有子 agent、有归属"的状态，不必再等一次轮开启。
            self._settle_after_maintenance()

    def _round_note_enabled(self) -> bool:
        """每轮一段话（V4 §3.2 第一步）是否开启：managed ＋ 有 task ＋ 开关未关。"""
        if self.context_mode != MODE_MANAGED or self.task is None:
            return False
        return (os.environ.get("WOVRA_ROUND_NOTE", "1") or "1").strip().lower() not in (
            "0", "false", "no", "off",
        )

    def _note_failed(self, round_: dict, why: str) -> None:
        """结算失败：只留痕，不重发、不跨轮、不动历史字节（§155）。"""
        round_["note_state"] = "failed"
        if self.task is not None:
            self.task.record("note", f"R{round_.get('seq')} 结算失败：{why}（不重发，原文继续顶着）")

    def _settle_round_note(self, round_: dict) -> None:
        """轮闭合处的**同步**结算：产出"本轮的一段话"（一句话 ＋ 失败 ＋ 账本增量）。

        形态：**尾部追加调用**——装配快照（与工作调用同一序列）＋ 尾部结算指令，
        tools 数组与工作调用同一序列化（骑满前缀缓存，实测只付"指令＋产物"）。
        **不改装配、不改归因**：产物只落 `round["note"]` 与账本，装配仍走整理链路。

        纪律（§154/§155）：产物一律落地；校验只留痕；失败不重试、不跨轮、不生成骨架段；
        历史字节一字不动。执行者由**代码**盖章（不让模型自报身份）。
        """
        if not self._round_note_enabled():
            return
        round_["note_state"] = "pending"
        snapshot = self._maint_snapshot()
        if snapshot is None:
            self._note_failed(round_, "装配快照不可用（尾部协议不完整）")
            return
        messages = list(snapshot)
        messages.append({
            "role": "user",
            "content": _ROUND_NOTE_INSTRUCTION + "\n\n[锚]\n"
                       + "\n".join(note_module.anchor_lines(round_)),
        })
        tools = maint_tools(self._schemas, _ROUND_NOTE_SUBMIT)
        box: dict = {}

        def run() -> None:
            try:
                box["out"] = self._stream_call(messages, tools=tools, purpose="note")
            except Exception as error:  # noqa: BLE001——异常带回主线程判失败
                box["error"] = f"{type(error).__name__}: {str(error)[:160]}"

        thread = threading.Thread(
            target=safety_module.workspace_bound_target(run),
            name="wovra-round-note", daemon=True,
        )
        thread.start()
        thread.join(self._note_timeout)
        if thread.is_alive():
            self._note_failed(round_, f"结算超时（{self._note_timeout:.0f}s）")
            return
        if "error" in box:
            self._note_failed(round_, f"结算调用异常：{box['error']}")
            return
        out = box.get("out")
        if not out:
            self._note_failed(round_, "结算调用没有返回")
            return
        _content, ordered, _usage = out
        product, why = note_module.parse_note(ordered, round_)
        if product is None:
            self._note_failed(round_, why)
            return
        round_["note"] = product
        round_["note_state"] = "done"
        # 账本增量：**带来源戳**（谁在哪个轮加的）＋ 按正文去重 ＋ 结案带越界保护
        patch, ledger_notes = note_module.build_ledger_patch(
            product.get("ledger_append") or {}, int(round_.get("seq") or 0),
            str(product.get("executor") or "Main"),
            {f: (getattr(self.task.get_state(), f) or []) for f in note_module._LEDGER_FIELDS},
        )
        report = self.task.apply_state_patch(patch) if patch else {}
        self._persist_rounds()
        if self.task is None:
            return
        defects = note_module.soft_defects(product, round_)
        detail = (f"R{round_.get('seq')} 结算完成：{len(product['sentence'])} 字，"
                  f"{len(product['failures'])} 条失败"
                  + ("；账本 " + "；".join(ledger_notes) if ledger_notes else "；账本无变化"))
        if defects:
            detail += f"；软档 {len(defects)} 条（只留痕）：{'；'.join(defects[:3])}"
        self.task.record("note", detail)
        closed = (report or {}).get("closed") or []
        if closed:
            self.task.record("note", f"结案 {len(closed)} 条：" + "；".join(
                f"{field}:{text[:40]}" for field, text in closed))
        for line in ledger_notes:
            if "被拒" in line or "未命中" in line:
                self.task.record("note", f"账本·{line}")

    def _live_file_lines(self) -> list[str]:
        """活性文件清单（路径 + 首行说明，代码取）——V4 分裂的**全部输入**。"""
        from pathlib import Path as _Path

        workspace = _Path(safety_module.workspace_root())
        lines: list[str] = []
        for i, path in enumerate(sorted(self._live_files()), 1):
            note = ""
            try:
                target = workspace / path
                if target.is_file():
                    for line in target.read_text(
                            encoding="utf-8", errors="replace").splitlines()[:12]:
                        text = line.strip().lstrip("#/\"'").strip()
                        if len(text) >= 6:
                            note = f"  —— {text[:60]}"
                            break
            except OSError:
                note = ""
            lines.append(f"{i}. {path}{note}")
        return lines

    def _maybe_split_v4(self) -> None:
        """V4 分裂：只产出结构树（活性文件），批次 = 尚未做过分裂分析的已闭合轮。

        同一批材料只试一次（失败也留痕，等文件集变化再试）——不重发、不跨轮。
        产物落 `pending_org["domains"]`，落地复用既有 landing（注册表 ＋ 归属）。
        """
        if self.task is None:
            return
        batch = [r for r in self.rounds
                 if str(r.get("end_state")) == "completed"
                 and str(r.get("split_state") or "") not in ("ready",)
                 and int(r["seq"]) not in self._org_inflight]
        if not batch:
            return
        fingerprint = "\n".join(sorted(self._live_files()))
        if not fingerprint or fingerprint == getattr(self, "_v4_split_fingerprint", ""):
            return
        snapshot = self._maint_snapshot()
        if snapshot is None:
            return
        self._v4_split_fingerprint = fingerprint
        for r in batch:
            r["split_state"] = "running"
            self._org_inflight.add(int(r["seq"]))
        self._persist_rounds()
        ok = False
        try:
            ok = self._split_rounds(batch, None, snapshot)
        except Exception as error:  # noqa: BLE001——分裂失败不拖垮对话
            if self.task is not None:
                self.task.record(
                    "split", f"V4 分裂异常：{type(error).__name__}: {str(error)[:150]}"
                )
        finally:
            for r in batch:
                self._org_inflight.discard(int(r["seq"]))
                r["split_state"] = "ready" if ok else "failed"
            self._persist_rounds()
        if ok:
            self._publish_product_early()

    def _fold_keep_rounds(self) -> int:
        return int(getattr(self, "_fold_keep", _FOLD_KEEP_ROUNDS_DEFAULT))

    def _advance_fold_line(self) -> None:
        """水位到了就把"超龄且有 note"的轮**一次换到位**（§6.2/§6.3）。

        只在**轮闭合处**推进（轮内冻结）；水位/宽限与整理同一套旋钮（冷却不设——
        换档只在到线时发生，且它是唯一允许断前缀的动作）。逐个标 `folded` 标志：
        渲染只读标志，装配不现场重算，于是 note 迟到或线不动都不会在轮次中部改字节。
        """
        if not self._round_note_enabled():
            return
        closed = [r for r in self.rounds if str(r.get("end_state")) == "completed"]
        if not closed:
            return
        current = int(closed[-1]["seq"])
        if self.last_context_estimate < self._org_watermark or current <= self._org_grace:
            return
        cut = current - self._fold_keep_rounds()
        staged = [r for r in closed
                  if int(r["seq"]) <= cut
                  and not r.get("folded")
                  and str(r.get("note_state")) == "done"]
        if not staged:
            return
        for r in staged:
            r["folded"] = True
        self._persist_rounds()
        if self.task is not None:
            self.task.record(
                "fold",
                f"换档：R{staged[0]['seq']}–R{staged[-1]['seq']}（{len(staged)} 轮换成一段话；"
                f"原文窗口保留最近 {self._fold_keep_rounds()} 轮）",
            )

    def _ensure_worker(self) -> None:
        if self._org_thread is not None and self._org_thread.is_alive():
            return
        self._org_thread = threading.Thread(
            # 整理线程要按**本会话的工作区**读文件（`_guess_file_note` 等），
            # 而 thread-local 不随新建线程继承——把父线程那份带进去。
            target=safety_module.workspace_bound_target(self._org_worker),
            name="wovra-organization", daemon=True,
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
                # 后台整理跑完：用户此刻若没在轮里（正在打字/空闲），立刻生效
                # ——子 agent 与其重组上下文在下一轮开场前就建好（§50）。
                self._settle_after_maintenance()

    def _product_parent_id(self, parent: str) -> str:
        """产物挂在哪条职责线下（嵌套分裂时 = 被再裂的那个子域条目 id）。"""
        want = str(parent or "")
        if not want or want == registry_module.MAIN_AGENT_ID:
            return ""
        if self.task is None:
            return ""
        ent = next(
            (e for e in (self.task.registry or [])
             if isinstance(e, dict)
             and want in (str(e.get("id")), str(e.get("name")))),
            None,
        )
        return str((ent or {}).get("id") or "")

    def _skip_incomplete_split(self, product_round: dict, uncovered: list) -> bool:
        """产物**覆盖不全**（节点声明的范围盖不住现有文件）→ **本次不分裂**。

        用户口径（2026-09-15，原话）："不可以的，你这样降级……如果不行，就不分裂了，
        就直接按整理后结构来。不可以给我错误分裂，错误分裂不如不分裂。"

        于是这里从"机械归位 + 照常落地"改成**闭锁**：

        * 产物声明 `path`/`paths` 却盖不住现有活性文件 → 这棵树是残缺的（模型没看完
          材料 / 边做边加了文件）——落下去就是**错的域树与错的 agent**；
        * 处置：**丢弃产物、不落域树**，但**保留整理结果**（`org_state` 不动，
          不把批次打回水位——那会让整理白跑一遍）；`split_state='skipped'` +
          `split_skipped` 留痕；
        * 下一次水位满时（新轮进批、`_live_files()` 已是全局口径）会重新判断是否分裂，
          届时材料完整，树也就完整。

        返回 True 表示"已按不分裂处置"。
        """
        if self.task is None or not uncovered:
            return False
        product_round.pop("pending_org", None)     # 产物丢弃：不落域树、不落注册表
        gen = product_round.get("org_generation")
        for rr in self.rounds:
            if (str(rr.get("org_state") or "") == "done"
                    and rr.get("org_generation") == gen):
                rr["split_state"] = "skipped"
        self.task.record(
            "split_skipped",
            "分裂产物覆盖不全（声明范围盖不住 "
            f"{len(uncovered)} 个文件：{'、'.join(str(u) for u in uncovered[:3])}"
            "…）——**本次不分裂**：保留整理后的结构，等下一次水位再判断是否分裂"
            "（用户口径：错误分裂不如不分裂）",
        )
        if self.on_progress:
            self.on_progress("⏸ 分裂产物覆盖不全 → 本次不分裂（保留整理结果，下批再判断）")
        return True

    def _publish_product_early(self) -> int:
        """A 步：产物就绪就**立刻**落"不碰上下文字节"的部分，不再等轮边界。

        用户口径更正（2026-09-15）：「下一轮闭合生效」指的是**上下文替换**——只有会改
        "当前开放轮正在用的装配字节"的东西才需要等边界。按这个口径拆开（读码核过）：

        * **注册表**（子 agent 出现）→ 全量路径的装配**不读**它（职责表只塞进**视图
          路径**的系统段；`assembly.py` 里 `responsibility_lines` 只有视图路径调用点）；
        * **`split_state`** → 纯展示；
        * **过期回入水位** → 纯账目；
        * **必须等边界**的只有：`domains`（装配路径的开关）＋ 过去轮渲染用的整理字段
          （summary/normalized/merged/state_patch）＋ 闭合轮归属（视图路径 judge-3 读它，
          且用户口径本就要求"到下一轮才生效"）。

        判据：开放轮**已经在视图路径**（它之前已有域树生效）时，职责表在它的系统段里
        → 注册表仍等边界；只有纯账目类（split_state / 回水位）照落。

        调用点：维护线程分裂产物刚暂存好（立刻），以及"产物就绪但轮在进行中"的让路路径。
        """
        if self.task is None:
            return 0
        products = [
            r for r in self.rounds
            if isinstance(r.get("pending_org"), dict)
            and r["pending_org"].get("domains")
        ]
        if not products:
            return 0
        view_path = False
        open_round = self._open_round_on_disk()
        if open_round is not None:
            try:
                seq = int(open_round.get("seq") or 0)
            except (TypeError, ValueError):
                seq = 0
            view_path = any(
                r.get("domains") for r in self.rounds
                if int(r.get("seq") or 0) < seq
            )
        landed = 0
        for r in products:
            pending = r["pending_org"]
            domains = pending.get("domains")
            # 早落路径同样区分两类：**材料过期**（声明的范围覆盖不到 → 回水位，
            # 可自愈不必等边界）与**硬缺陷**（重叠/空域 → 留到边界分类留痕）。
            # 未覆盖不再是缺陷（2026-09-15：Runtime 已机械归位）。
            if self._skip_incomplete_split(r, list(pending.get("uncovered_by_scope") or [])):
                landed += 1
                continue
            defects = registry_module.split_defects(
                domains, self._live_files(), self._non_live_files()
            )
            if defects:
                continue
            early: list[str] = []
            # **信封类字段：任何路径都能立刻落**（2026-09-15 用户裁定"能提前的都提前"）：
            # `state_patch` → 账本只进**尾部信封**（每步重算、不是历史，动它不破前缀）；
            # `unassigned`/`split_assessment` 纯展示（装配不读）。落完就从暂存区摘掉，
            # 边界那一步不会重复应用。
            if self.task is not None:
                patch = pending.pop("state_patch", None)
                if patch:
                    report = self.task.apply_state_patch(patch)
                    self._record_close_report(report)
                    early.append("账本")
                un = pending.pop("unassigned", None)
                if un is not None:
                    r["unassigned"] = un
                    early.append("未归属")
                sa = pending.pop("split_assessment", None)
                if sa is not None:
                    r["split_assessment"] = sa
                    early.append("可分裂性")
            if not view_path:
                # **闭合轮归属（预备值）也能立刻落**：全量路径装配不读 active_view，
                # 视图路径（职责表/归属都在开放轮系统段与历史里）才必须等边界。
                if self._apply_staged_views(domains):
                    early.append("归属")
            if view_path or pending.get("registry_landed"):
                if early and self.task is not None:
                    self.task.record(
                        "maintenance",
                        "提前生效（不碰开放轮字节）：" + "、".join(early)
                        + ("；注册表/归属等视图路径安全判据" if view_path else ""),
                    )
                    landed += 1
                continue
            parent_id = self._product_parent_id(
                str(pending.get("split_parent") or "")
            )
            projected, added, updated, settle_lines = registry_module.project_merge(
                self.task.registry, domains,
                parent_id=parent_id, retire_id=parent_id,
            )
            if registry_module.registry_defects(projected):
                continue                      # 跨条目互斥 → 留到边界按拒收处理
            registry_module.land(self.task.registry, projected)
            pending["registry_landed"] = True
            if settle_lines:
                self.task.record(
                    "maintenance",
                    f"归属结算：{len(settle_lines)} 个文件的清单从旧条目"
                    "移到新域（" + "；".join(settle_lines[:6])
                    + ("…" if len(settle_lines) > 6 else "") + "）",
                )
            gen = r.get("org_generation")
            for rr in self.rounds:
                if (str(rr.get("org_state") or "") == "done"
                        and rr.get("org_generation") == gen):
                    rr["split_state"] = "ready"
            detail = "、".join((added + updated)[:6]) or "（无变化）"
            self.task.record(
                "maintenance",
                f"registry：分裂产物**提前落实**（不等轮边界）——子 agent 即时可见"
                f"（{detail}）"
                + (f"；同时提前生效：{'、'.join(early)}" if early else "")
                + "；上下文替换（域树/过去轮渲染字段）仍等轮闭合",
            )
            if self.on_progress:
                self.on_progress(f"⚡ 子 agent 已就绪（{detail}）——上下文替换等轮闭合")
            landed += 1
        if landed:
            self._persist_rounds()
        return landed

    def _open_round_on_disk(self) -> Optional[dict]:
        """是否有开放轮——`current_round` 与**盘上**（跨实例）两处都算。

        serve 是每轮新建 agent：后台维护线程（旧实例）看不见下一轮（新实例），
        它的 `current_round` 恒为 None。只查它会让产物在别人的轮进行中落地——
        那一轮下一次落盘（工具卡）会 rebase 合并产物，`latest_domains` 从空变
        非空，装配在**轮中间**从全量切到视图路径（前缀断裂、前后步上下文不
        一致）。故先按一次 stat 重读盘（`_rebase_if_stale`：只有真被改过才读整
        份），以盘上最后一轮为准；`end_state ∈ {"", "open"}` 与
        `_open_or_reuse_round` 的可续判据同一口径。
        """
        if self.current_round is not None:
            return self.current_round
        try:
            self._rebase_if_stale()
        except Exception:  # noqa: BLE001——重读失败按自己这份判（不比原来更差）
            pass
        last = self.rounds[-1] if self.rounds else None
        if last is not None and str(last.get("end_state") or "") in ("", "open"):
            return last
        return None

    def _round_is_open(self, r: dict) -> bool:
        """这一轮是否还开着（`current_round`，或盘上最后一轮且未闭合）。"""
        if r is self.current_round:
            return True
        if not self.rounds or r is not self.rounds[-1]:
            return False
        return str(r.get("end_state") or "") in ("", "open")

    def _prepare_settle_views(self, open_round: dict) -> int:
        """产物就绪但轮在进行中：**判定照做、先记预备区**，边界才生效。

        用户口径（2026-09-14）：「分裂后重组上下文，接着干活，还是异步整理那个
        线程，将现在已经闭合的轮路由判断到对应子 agent 中，只是先添加，等到对话
        完才生效……其它环节很多都可以空闲直接做了，没必要等新轮生效，毕竟其又
        不会影响执行任务」。故这里把 `_settle_views` 的判定在**维护线程**里先算
        完，写进各轮的 `pending_view` 预备区：

        * 只写预备区——轮进行中装配读不到它（生效面只有 `active_view`/
          `domains`/注册表），"一轮内上下文不变"不受影响；
        * 判据用**影子注册表**（`registry_module.project_merge` 纯投影）——产物
          此刻还没落进注册表，而判定必须按落地后的形状做（域名/文件清单都在
          产物里）；
        * 只预备非主 agent 的判定（主 agent 是默认值，边界补判它近乎零成本），
          也只在预备到东西或确有产物待落地时留一条痕——"分裂完了子 agent 还没
          出现"要说得清是推迟，不是没跑。
        """
        from .. import routing as routing_module

        if self.task is None:
            return 0
        products = [
            r["pending_org"] for r in self.rounds
            if isinstance(r.get("pending_org"), dict)
            and r["pending_org"].get("domains")
        ]
        if not products:
            return 0
        for rr in self.rounds:               # 状态可见：产物就绪、等轮边界
            if (str(rr.get("org_state") or "") == "done"
                    and str(rr.get("split_state") or "") in ("", "running", "ready")):
                rr["split_state"] = "deferred"
        domains = products[-1]["domains"]
        parent = str(products[-1].get("split_parent") or "")
        if not parent and self.rounds:
            parent = str(self.rounds[-1].get("active_view") or "")
        entry_id = ""
        if parent and parent != registry_module.MAIN_AGENT_ID:
            ent = next(
                (e for e in (self.task.registry or [])
                 if isinstance(e, dict)
                 and parent in (str(e.get("id")), str(e.get("name")))),
                None,
            )
            entry_id = str((ent or {}).get("id") or "")
        projected, _, _, _ = registry_module.project_merge(
            self.task.registry, domains, parent_id=entry_id, retire_id=entry_id
        )
        digest = registry_module.domains_digest(domains)
        hints = views_module.files_by_domain(self.rounds, domains)
        staged: list[str] = []
        sticky = ""
        for r in self.rounds:
            if self._round_is_open(r):
                continue                       # 开放轮不判：它闭合后走边界那套
            live = str(r.get("active_view") or "")
            if live not in ("", registry_module.MAIN_AGENT_ID):
                sticky = live                   # 已归域：粘滞链的事实来源
                continue
            text = str((r.get("user_input") or {}).get("original") or "")
            result = routing_module.route(
                text, projected, sticky=sticky,
                explicit=str(r.get("route_explicit") or ""), file_hints=hints,
            )
            view = str(result.get("view") or "")
            sticky = view
            if view and view != registry_module.MAIN_AGENT_ID:
                r["pending_view"] = {
                    "view": view,
                    "reason": str(result.get("reason") or ""),
                    "domains": digest,
                }
                staged.append(f"R{r.get('seq')}→{view}")
            else:
                r.pop("pending_view", None)     # 判定为主 agent：无需预备
        if staged:
            detail = (
                f"归属预判（异步，未生效）：{'、'.join(staged[:10])}"
                + ("…" if len(staged) > 10 else "")
                + f"——R{open_round.get('seq')} 仍在进行中，轮边界落地"
            )
        else:
            detail = (
                f"产物已就绪：R{open_round.get('seq')} 仍在进行中，按「轮边界"
                "生效」推迟——该轮闭合时立即落地（域树/子 agent/归属）"
            )
        if not any(
            str(e.get("detail")) == detail for e in (self.task.history or [])[-6:]
        ):
            self.task.record("route", detail)
            self._persist_rounds()
        return len(staged)

    def _apply_staged_views(self, domains: Optional[list] = None) -> int:
        """轮边界：把 `pending_view` 预备区一次性生效（只写 `active_view`）。

        预备值只在**所依据的产物正好是现在落地的那一版**时採用（
        `domains_digest` 核对）——产物被拒收/换了批次就丢弃，交给
        `_settle_views` 按现行材料重算。预备是省时间的加速器，不是新的
        真相来源，故任何对不上都以重算为准。
        """
        staged = [
            r for r in self.rounds if isinstance(r.get("pending_view"), dict)
        ]
        if not staged:
            return 0
        # `domains` 显式给出时用它的指纹（A 步：产物还在暂存区、尚未落档）；
        # 缺省（B 步）用"已落档的最新域树"
        live = registry_module.domains_digest(
            domains if domains is not None
            else registry_module.latest_domains(self.rounds)
        )
        applied = 0
        for r in staged:
            st = r.pop("pending_view", None) or {}
            if str(st.get("domains") or "") != live:
                continue
            view = str(st.get("view") or "")
            if not view or view == registry_module.MAIN_AGENT_ID:
                continue
            if str(r.get("active_view") or "") not in (
                "", registry_module.MAIN_AGENT_ID
            ):
                continue
            r["active_view"] = view
            applied += 1
        if applied and self.task is not None:
            self.task.record(
                "route", f"预备归属生效：{applied} 轮（异步已判好，轮边界落地）"
            )
        return applied

    def _settle_after_maintenance(self) -> None:
        """维护一跑完就让产物**立刻生效**（有开放轮时留给轮边界）。
        2026-09-12 用户口径：「把重组上下文、子 agent 都放到分裂后面、下一轮
        对话前面……下一轮对话开始，基本就只需要追求对话」。旧行为把 promote
        与渐近归属一律推到下一轮开场，于是会话里看得见"分裂完了但子 agent 还
        没出现"，而下一轮开场又要多做一轮建账 + 归位。

        安全性（缓存前缀纪律，AGENTS.md §2：**只有整理生效才允许破坏前缀，
        且不许在轮次中部改写历史字节**）：

        * 开放轮判据按 **`_open_round_on_disk`**（`current_round` + 盘上最后
          一轮）——serve 每轮新建 agent，维护线程看不见"下一轮"，只查自己的
          `current_round` 会让产物在别人的轮进行中泄漏落地（见该函数注释）；
        * 异步维护（chat 后台线程）：与 `_open_or_reuse_round` 共用
          `_view_lock`，两者不会交错——用户没在轮里就立刻生效，已经开始下一轮
          则照旧留给轮边界。

        生效内容是"完整的开场状态"：产物落实（精修索引/状态补丁/**域树 →
        注册表条目**）+ 渐近归属补判（各轮归到对应域，子 agent 的重组上下文
        随之可派生）。两步都是零 LLM 纯函数。

        有开放轮时**不是白等**（2026-09-14 用户口径：「其它环节很多都可以空闲
        直接做了」）：判定照做、先记预备区（`_prepare_settle_views`），轮边界
        只做一次性生效（`_apply_staged_views`）。
        """
        try:
            with self._view_lock:
                open_round = self._open_round_on_disk()
                if open_round is not None:
                    # 轮进行中：装配字节不许动（"一轮内上下文不变"），但归属
                    # 判定可以现在就做完、记进预备区；注册表等"不碰字节"的部分
                    # 也立刻落（A 步，2026-09-15——不再让子 agent 干等轮边界）
                    self._prepare_settle_views(open_round)
                    self._publish_product_early()
                    return
                # 落盘前会由 `_persist_rounds` 自动重读盘 + 并集合并（§86）：
                # 异步维护与下一轮的 agent 会同时写 task.json，两边写的都是整份
                self._promote_org_results()
                self._settle_views()
        except Exception as error:  # noqa: BLE001——即时生效失败不该拖垮会话
            if self.task is not None:
                self.task.record(
                    "maintenance",
                    f"维护产物即时生效失败（{error!r}）——留给下一轮开场补做",
                )

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

        if v4_enabled():
            # V4：输入只有**活性文件**（路径 ＋ 首行说明）——没有块地图、没有用户块/
            # 环境块/保底块、没有非 LIVE 挂载、没有视图水位；其余全由代码算。
            all_ids: set = set()      # V4 输入里没有块，块引用校验自然空过
            # 归属落地仍要这两张表（按路径机械绑定），但它们**不进输入**。
            live_ids, hist_ids = self._split_file_map()
            round_blocks, merged_groups = {}, None   # V4 没有块结构
            instruction = (
                "[分裂结构指令]\n以上是本会话的完整上下文。\n\n"
                "[活性文件]\n" + "\n".join(self._live_file_lines()) + "\n\n"
                + _SPLIT_LIVE_INSTRUCTIONS
            )
        else:
            # 硬数据（Runtime 生成，零 LLM）：活性文件清单 + 数量上限
            live_ids, hist_ids = self._split_file_map()
            hard_lines, n_live = self._split_hard_data(rounds, live_ids, hist_ids)
            round_blocks, map_lines, _merged = self._block_map_lines(rounds)
            # 主 agent 残留桶占比（零 LLM 体量事实）：纯对话块合计占本批内容
            # 多少——顶层节点计数的门槛由它定（判据见 _SPLIT_INSTRUCTIONS）。
            chat_share = self._chat_block_share(rounds, round_blocks)
            # 逐层分裂硬数据（零 LLM）：各视图自身体量与是否到自己的水位。
            # 分裂后水位按视图各自计量（plan §13.1）——子视图到达自己的水位时
            # 按同一套机制在它内部再裂一层（A-1 → A-1-1），终态「只操作单个
            # 文件为止」。判据归机制、语义归模型：Runtime 给体量事实，模型判断
            # 这一摊活是否真已分成互不相干的两条线。
            view_lines = self._split_view_watermarks()
            # 覆盖缺口硬数据（2026-09-12 加，2026-09-14 用户判定"多余且错误"后
            # **不再注入**）：它是给模型自纠用的，现在模型的职责只剩"写树"，
            # 漏项由 Runtime 机械校验并报错，不需要模型看这条。`views.coverage_gap`
            # 与 `_split_coverage_lines` 保留——代码侧判定与仪器仍可用。
            all_ids = {
                b["id"] for blocks in round_blocks.values() for b in blocks
            }
            seq_list = "、R".join(str(r["seq"]) for r in rounds)
            instruction = (
                "[分裂结构指令]\n"
                "以上是本会话的完整上下文（含刚完成的整理产物与分块地图）。"
                f"请把**现状**梳理成结构树（不是话题分类），对象为这些轮次：R{seq_list}。\n\n"
                "[硬数据]（Runtime 生成，零 LLM）\n"
                + "\n".join(hard_lines)
                + f"\n- 纯对话块（无文件交互，闲聊）内容占比：约 {chat_share * 100:.0f}%"
                + ("\n" + "\n".join(view_lines) if view_lines else "")
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
            # 参数截断/解析失败 → 分裂无任何痕迹）——落一条**失败记录**
            # 并留痕，账本可查。失败现场一并记录（arguments 长度+头部 /
            # 正文长度），下次直接能看出是截断还是空壳还是模型没走工具出口。
            # 2026-09-14：**不再写"保守按不可分处理"这类判定**——分裂与否
            # 由代码按结构树决定，模型侧没有判定字段；这里只记录"没拿到树"。
            if not evidence:
                evidence = f"无 submit_domains 调用；正文 {len(content or '')} 字符"
            reason = (
                "分裂分析无可用产物（" + evidence + "），未产出结构树"
                + ("，已带诊断重发一次仍失败" if retried else "，不重试")
            )
            pending = rounds[0].setdefault("pending_org", {})
            pending["split_assessment"] = {"reason": reason}
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

        # **文件编号解析**（2026-09-14 用户思路：路径名太麻烦，用编号）：
        # 模型产物里只写 `L03`/`H07`，Runtime 当场翻回真路径。编号未知、
        # 或用错节（H 当叶子 / L 当历史）→ 带诊断重发一次（与解析失败同款）；
        # 仍坏则中止整批回入水位——宁可重做，不静默错挂。
        def _abort_ids(reason: str) -> None:
            pending = rounds[0].setdefault("pending_org", {})
            pending["split_assessment"] = {"reason": reason}
            if self.task is not None:
                self.task.record("maintenance", f"split：**中止**——{reason}")
            self._persist_rounds()
            raise SplitCoverageError(reason)

        for _attempt in (0, 1):
            file_defects, normalized = self._resolve_file_refs(
                domains, unassigned, live_ids, hist_ids)
            if normalized and self.task is not None:
                self.task.record(
                    "maintenance",
                    f"split：Runtime 归一了 {len(normalized)} 个文件引用"
                    f"（{normalized[0]}…）" if len(normalized) > 1
                    else f"split：Runtime 归一了文件引用（{normalized[0]}）",
                )
            id_defects = (
                file_defects
                + self._validate_block_refs(domains, unassigned, all_ids)
            )
            if not id_defects:
                break
            id_evidence = "；".join(id_defects[:6])
            if _attempt == 1:
                _abort_ids(f"产物文件编号/块 ID 不可用（{id_evidence}）")
            if self.task is not None:
                self.task.record(
                    "maintenance",
                    f"split：编号有误（文件编号或块 ID），带诊断重发一次"
                    f"（{id_evidence}）",
                )
            repair = self._split_repair_messages(
                messages, content, ordered, id_evidence
            )
            if repair is None:
                _abort_ids(f"产物编号不可用且无法重发（{id_evidence}）")
            content, ordered, _usage = self._stream_call(
                repair, tools=split_tools, purpose="split",
            )
            reproduct = self._extract_domains(content, ordered)
            if reproduct is None:
                _abort_ids(f"产物编号不可用，重发后无产物（{id_evidence}）")
            domains, unassigned, split = reproduct

        # 文件域自动归属（2026-09-11）：块的 file ∈ 某域 file_domains 时，
        # Runtime 机械归入该域——模型不必逐块列 block_ids（大幅缩小输出，
        # 防超长截断令分析作废）。模型显式声明的 block_ids 优先（跨域/
        # 例外仍可写）。
        self._auto_assign_domains(domains, round_blocks)
        # 主 agent 兜底（2026-09-10，thoughts 字段收敛后）：没被任何域
        # 认领的块 = 独立思想/零散块 → Runtime 自动归 unassigned（主
        # agent 剩余集合）。模型忘了填 unassigned 也不丢块——语义上
        # "没有域认领"与"归主 agent"等价，不需要模型再声明一次。
        # 2026-09-14：节点声明的**保底块/用户块归宿**（chat_block_ids /
        # user_block_ids）也算认领。
        covered = {
            b for d in domains if isinstance(d, dict)
            for b in ((d.get("block_ids") or []) + (d.get("chat_block_ids") or [])
                      + (d.get("user_block_ids") or []))
        }
        kept = [
            b for b in ((unassigned or {}).get("block_ids") or [])
            if b in all_ids
        ]

        # **主 agent 不许持有文件块**（2026-09-13 用户口径）现在由**构造**保证：
        # 下面 `_bind_files_by_path` 把每个活性文件机械分给某个节点（分不出去就
        # 进 Runtime 机械桶），主 agent 手里永远不会出现文件块——原来的"事后校验
        # + 抛错中止"因此可以整体退场（那也正是"每闭合一轮失败一次"的来源）。
        # 判据仍与 `views.ownership` 同源：归属看节点的 files/file_domains 并集。

        # **文件归属 = 代码算**（2026-09-15 用户口径："分裂只需要写结构树和每个
        # agent 的职责，其它都不需要搞了"）：节点用 `path`/`paths` 声明**范围**，
        # 这里把**每个活性文件**按**最深路径前缀**机械分给节点——不再要求模型
        # 逐文件填编号（那是产物被截断、被拒收的头号来源：实测一次写满编号的
        # 产物截断只抢救出 22 个节点、另一次因两个历史文件落点被整批拒收）。
        # 没有节点声明到它的文件 → 仍走 `_auto_claim`（同目录 → 最近前缀 →
        # 机械桶），**绝不落主 agent**（2026-09-13 原口径不变）。
        bind_notes, uncovered_by_scope = self._bind_files_by_path(domains)
        if bind_notes and self.task is not None:
            for n in bind_notes[:6]:
                self.task.record("maintenance", f"split：{n}")
            if len(bind_notes) > 6:
                self.task.record(
                    "maintenance",
                    f"split：{bind_notes[0]}（等 {len(bind_notes)} 条，同类的见上）",
                )
        # 绑定按**代码**重建了各节点的 files，故 `_claimed`（模型声明 vs 轮归属）
        # 不再需要——保留下面的历史文件机械挂载即可。

        # **非 LIVE 文件机械挂载**（2026-09-14 用户口径："非 LIVE 文件也是
        # 必须填满的"——磁盘上没了不等于没用：临时测试脚本验过什么、结果如何，
        # 是后面装配上下文要用的结论）。2026-09-15 起**不再要求模型填**：
        # Runtime 按"最近的活性兄弟/同目录"挂到某个节点下，挂不上的进机械桶。
        claimed_hist = [
            str(f) for d in domains if isinstance(d, dict)
            for f in (d.get("history_files") or [])
        ]
        missing_hist = sorted(
            p for p in set(self._non_live_files())
            if not pathmatch_module.matches(p, claimed_hist)
        )
        if missing_hist:
            # 同上：Runtime 归位（非 LIVE 文件也**绝不落主 agent**——用户口径
            # 2026-09-14"必须挂满"仍成立，只是由代码来挂）
            notes = self._auto_claim(domains, missing_hist, "history_files")
            if self.task is not None:
                for n in notes[:6]:
                    self.task.record(
                        "maintenance", f"split：Runtime 自动归位（历史文件）——{n}")
                if len(notes) > 6:
                    self.task.record(
                        "maintenance",
                        f"split：Runtime 自动归位（历史文件）——另 {len(notes) - 6} 条",
                    )

        orphans = sorted(all_ids - covered - set(kept))
        if orphans:
            unassigned = {
                "block_ids": kept + orphans,
                "reason": (unassigned or {}).get("reason")
                or "未被任何文件域认领（独立思想/零散块），归主 agent",
                **({"topic": unassigned["topic"]} if isinstance(unassigned, dict)
                   and unassigned.get("topic") else {}),
            }
            if self.task is not None:
                self.task.record(
                    "maintenance",
                    f"split：{len(orphans)} 块未被域认领，已自动归主 agent "
                    f"{orphans[:8]}" + ("…" if len(orphans) > 8 else ""),
                )

        # **主 agent 桶物化成顶层节点**（2026-09-14 用户口径：树里
        # "主 agent（闲谈/未归属）"改名成闲聊的主题，参与顶层计数、但
        # 归属仍是主 agent）。这样"顶层 >1 就分裂"的规则能把它算进去，
        # 同时又不会给它生成子 agent（registry 侧按 `main_agent` 跳过）。
        if unassigned and (unassigned.get("block_ids")):
            topic = " ".join(str(unassigned.get("topic") or "").split())[:24]
            bucket = {
                "name": topic or "闲聊（未归属）",
                "description": " ".join(
                    str(unassigned.get("reason") or "闲聊/未归属的工作线")
                    .split()
                )[:120],
                "main_agent": True,
                "chat_block_ids": list(unassigned.get("block_ids") or []),
            }
            domains.append(bucket)
            unassigned = None          # 已并入桶节点，不再重复暂存
            if self.task is not None:
                self.task.record(
                    "maintenance",
                    f"split：闲聊/未归属 {len(bucket['chat_block_ids'])} 块"
                    f"物化为顶层节点「{bucket['name']}」（归主 agent，不生成子 agent）",
                )
        # 暂存到批首轮（与 state_patch 同通道），下一轮开启随 promote 生效
        pending = rounds[0].setdefault("pending_org", {})
        pending["domains"] = domains
        # **材料过期的信号**（2026-09-15）：节点声明的**范围**覆盖不到的文件。
        # 不再据此拒收（Runtime 已把它们机械归位），但它是"这份产物是对旧材料
        # 做的"的可靠证据——轮到边界生效时若有更新轮次，本批回水位重做（原来
        # 这条判据挂在"未覆盖"缺陷上，那正是整批拒收的来源）。
        pending["uncovered_by_scope"] = list(uncovered_by_scope)
        # **分裂主体**（2026-09-12，worklog §64）：这批轮里出现最多的那个视图。
        # 为什么不取"promote 那一刻的本轮视图"：异步维护可能跨轮完成，届时
        # 当前轮早已换人（甚至换成主 agent）——产物就会被登记成"主 agent 分裂"
        # （A、B…），而它其实是 A 在裂（应为 A-1、A-2…）。
        view_counts: dict[str, int] = {}
        for rr in rounds:
            v = str(rr.get("active_view") or "").strip()
            if v:
                view_counts[v] = view_counts.get(v, 0) + 1
        dominant = max(view_counts, key=lambda k: view_counts[k]) if view_counts else ""
        if dominant == views_module.MAIN_AGENT_ID:
            dominant = ""
        pending["split_parent"] = dominant
        if unassigned:
            pending["unassigned"] = unassigned
        if split:
            pending["split_assessment"] = split
        self._persist_rounds()
        return True

    def _auto_assign_domains(
        self, domains: list[dict], round_blocks: dict[int, list[dict]]
    ) -> None:
        """把文件块按文件归属机械归入对应节点（零 LLM，原地修改 domains）。

        匹配规则与 `views.ownership` / 覆盖校验**同一套**：走
        `views._file_domain_entries`（并集 = files 具体清单 + 旧 file_domains
        前缀 + history_files），精确或目录前缀命中即归入。
        （2026-09-14 修：此前只认 `file_domains` 旧字段，而新产物按指令只给
        `files` —— 那条路下文件块一个都归不进去、全落进主 agent 兜底桶。）
        已显式出现在任何节点 block_ids 里的块不覆盖（尊重模型的例外声明）。
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
        entries = views_module._file_domain_entries(domains)
        if not entries:
            return
        domain_by_name: dict[str, dict] = {
            d["name"]: d for d in domains
            if isinstance(d, dict) and d.get("name")
        }
        for bid, f in block_file.items():
            if bid in claimed:
                continue
            name = views_module._match_domain(str(f), entries)
            if name is None:
                continue
            target = domain_by_name.get(name)
            if target is not None:
                target.setdefault("block_ids", []).append(bid)

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

    def _file_groups(self) -> dict[str, dict]:
        """文件账本按**归一路径**聚合（零 LLM）。

        同一个物理文件常以多种写法入账（`t1_hello.txt` / `./t1_hello.txt` /
        `sub/../t1_hello.txt`——agent 当时就是这么访问的），不聚合会变成
        "几个不同文件"且互相歧义（2026-09-14 实测：覆盖校验因此误判漏项）。
        聚合后：
        * `state`：live（有"写过且没被删"的记账）/ dead（只被删过）/ read_only；
        * 写/读次数求和；块引用并集。
        """
        ledger = lifecycle_module.FileLedger()
        for rr in self.rounds:
            ledger.update(rr, blocks=blocks_module.segment_round_by_file(rr))
        groups: dict[str, dict] = {}
        for path, e in ledger.entries().items():
            key = pathmatch_module.norm(str(path)) or str(path)
            g = groups.setdefault(
                key, {"state": "read_only", "write": 0, "read": 0, "refs": []}
            )
            state = str(e.get("state") or "")
            write = int(e.get("write_count") or 0)
            g["write"] += write
            g["read"] += int(e.get("read_count") or 0)
            g["refs"] += [str(x) for x in (e.get("block_refs") or [])]
            if state == lifecycle_module.STATE_DEAD:
                g["dead"] = True
            elif write > 0:
                g["live_seen"] = True
        for key, g in groups.items():
            if g.get("live_seen"):
                g["state"] = "live"
            elif g.get("dead"):
                g["state"] = "dead"
            else:
                g["state"] = "read_only"
        return groups

    def _split_file_map(self) -> tuple[dict[str, str], dict[str, str]]:
        """**文件编号表**（模型侧编码，2026-09-14 用户思路）。

        用户提的问题："用路径名太麻烦，给每个文件按顺序起个简单编号"。
        采纳方式：编号**只是模型侧的引用符**——硬数据里按 `L00 = 路径` /
        `H07 = 路径` 列出，模型产物里只写编号；Runtime 在提取产物后**立刻
        翻译回真路径**，下游（注册表/视图/前端）看到的仍是路径，零兼容负担。

        * `L00…` = 活性文件（split 单元，只能出现在 file/files）
        * `H00…` = 非 LIVE 文件（只能出现在 history_files）
        前缀分节还能做**机械校验**：编号未知、或把 H 填进叶子 → 当场报错
        （带诊断重发一次），不再依赖路径字符串比对。

        排序保证确定性：同一批数据两次分析编号一致（重试/回放都可复现）。
        """
        groups = self._file_groups()
        live = sorted(k for k, g in groups.items() if g["state"] == "live")
        hist = sorted(k for k, g in groups.items() if g["state"] != "live")
        return ({f"L{i:02d}": p for i, p in enumerate(live)},
                {f"H{i:02d}": p for i, p in enumerate(hist)})

    @staticmethod
    def _resolve_file_refs(
        domains: list, unassigned, live_ids: dict[str, str],
        hist_ids: dict[str, str],
    ) -> tuple[list[str], list[str]]:
        """把产物里的文件编号翻回真路径（原地修改）；返回 (缺陷清单, 归一痕迹)。

        * `file` / `files`：必须用 L 编号（或直接给路径，兼容手写/旧产物）；
        * `history_files`：必须用 H 编号（或路径）；
        * 编号未知 / 用错节（H 当叶子、L 当历史）→ 缺陷（调用方带诊断重发）。

        **路径形态也要归一**（2026-09-15）：模型抄路径时会按"项目自己的视角"写
        （`src/a.cpp` 而真实是 `cpp/src/a.cpp`）或带反引号/引号——原先原样放行，
        于是覆盖检查里**全军覆没**（实测会话 20260915-131130-010611：两批分裂、
        27/29 个文件"没有任何域认领"、烧掉 134 万 prompt tok 一个域没长出来）。
        这里做**唯一化归位**：精确命中 → 用它；唯一后缀命中 → 补成全路径
        （记一条归一痕迹，人视图可查）；对不上或歧义 → 原样保留（后面的
        Runtime 归位再兜）。
        """
        import re as _re
        id_re = _re.compile(r"^([LH])(\d+)$", _re.IGNORECASE)
        defects: list[str] = []
        normalized: list[str] = []
        pool = [p for p in list(live_ids.values()) + list(hist_ids.values()) if p]

        def canon(raw: str) -> str:
            """路径引用 → 真路径：精确 → 唯一后缀；无命中/歧义原样返回。"""
            want = pathmatch_module.norm(raw)
            if not want:
                return raw
            for p in pool:
                if pathmatch_module.norm(p) == want:
                    return p
            tail = "/" + want.lstrip("/")
            hits = [p for p in pool if pathmatch_module.norm(p).endswith(tail)]
            if len(hits) == 1:
                return hits[0]
            return raw

        def ref(v, where: str, ids: dict[str, str], other: dict[str, str],
                label: str, wrong_label: str) -> str:
            raw = str(v or "").strip()
            m = id_re.match(raw)
            if not m:
                fixed = canon(raw)
                if fixed != raw:
                    normalized.append(f"{raw} → {fixed}")
                return fixed
            key = f"{m.group(1).upper()}{int(m.group(2)):02d}"
            if key in ids:
                return ids[key]
            if key in other:
                defects.append(f"{where} 用了{wrong_label}编号 {raw}（{label}应为 "
                               f"{'L' if label == '活性文件' else 'H'} 编号）")
                return raw
            defects.append(f"{where} 引用了不存在的编号 {raw}")
            return raw

        for d in domains or []:
            if not isinstance(d, dict):
                continue
            where = f"节点「{d.get('name') or '?'}」"
            if d.get("file"):
                d["file"] = ref(d["file"], f"{where}.file", live_ids, hist_ids,
                                "活性文件", "非 LIVE 文件")
            if d.get("files"):
                d["files"] = [ref(f, f"{where}.files", live_ids, hist_ids,
                                  "活性文件", "非 LIVE 文件")
                              for f in d["files"]]
            if d.get("history_files"):
                d["history_files"] = [
                    ref(f, f"{where}.history_files", hist_ids, live_ids,
                        "非 LIVE 文件", "活性文件")
                    for f in d["history_files"]
                ]
        return defects, normalized

    @staticmethod
    def _validate_block_refs(
        domains: list, unassigned, all_ids: set
    ) -> list[str]:
        """产物引用的块 ID 必须真的在[分块地图]里（2026-09-14 用户口径）。

        此前未知块 ID 是**静默**的：那块没被认领 → 兜底归主 agent，模型把
        `R13-B1` 抄成 `R31-B1` 这类错误就藏过去了（块数对不上只能靠人看）。
        现在判错 → 带诊断重发一次 → 仍错整批回入水位。
        """
        defects: list[str] = []

        def _check(ids, where: str) -> None:
            for b in ids or []:
                raw = str(b or "").strip()
                if raw and raw not in all_ids:
                    defects.append(f"{where} 引用了不存在的块 ID {raw}")

        for d in domains or []:
            if not isinstance(d, dict):
                continue
            where = f"节点「{d.get('name') or '?'}」"
            _check(d.get("block_ids"), f"{where}.block_ids")
            _check(d.get("chat_block_ids"), f"{where}.chat_block_ids")
            _check(d.get("user_block_ids"), f"{where}.user_block_ids")
        if isinstance(unassigned, dict):
            _check(unassigned.get("block_ids"), "unassigned.block_ids")
        return defects

    def _bind_files_by_path(self, domains: list) -> list[str]:
        """把**每个活性文件**按节点声明的 `path`/`paths` 机械归属（最深前缀优先）。

        2026-09-15 用户口径："分裂只需要写结构树和每个 agent 的职责，其它都不
        需要搞了"——模型给**范围**，代码算**归宿**：

        * 节点可用 `path`（一个目录前缀或具体文件）或 `paths`（多个）声明范围；
          兼容字段 `file`/`files`（路径或已翻译成路径的 L 编号）与旧
          `file_domains`（目录前缀）同样当范围用；
        * 每个文件取**匹配最深**的范围（`src/wovra/tools/` 优先于 `src/`）；
          同深度时按节点声明顺序取前一个（确定性，不随机）；
        * 命中不了的 → **不补救**：记为"覆盖不全"交应用侧闭锁（本次不分裂，
          见 `_skip_incomplete_split`）——错树比如不分裂更坏（用户口径）；
        * 各节点的 `files` 由本函数**重建**（不再信模型手抄的清单）——因此
          "重叠/未覆盖"这两类缺陷在构造上不可能出现，`split_defects` 相应的
          拒收路径随之退场；
        * 没有描述的节点，描述取**它第一份文件的开头**（`_guess_file_note`：
          首行注释/标题/docstring）——"文件描述窃取文件开头一部分"，永远新鲜、
          不需要模型抄。

        返回 `(留痕行, 没被任何范围命中的文件)`——后者是**材料过期**的信号
        （写进 `pending["uncovered_by_scope"]`），不再用来拒收。
        """
        live = sorted(set(self._live_files()))
        if not live:
            return [], []          # 返回值是二元组：调用方按 (留痕, 未命中) 解包

        def scopes_of(node: dict) -> list[str]:
            raw: list[str] = []
            for key in ("path", "paths", "file", "files", "file_domains"):
                value = node.get(key)
                if isinstance(value, str):
                    raw.append(value)
                elif isinstance(value, (list, tuple)):
                    raw.extend(str(x) for x in value)
            out: list[str] = []
            for item in raw:
                norm = pathmatch_module.norm(str(item)).strip("/")
                if not norm or norm == ".":
                    continue
                # 目录形态（末段没有扩展名）统一成前缀，按前缀匹配；具体文件
                # 精确匹配（`webui/index.html` 不该吞掉 `webui/index.html.bak`）。
                is_file = "." in norm.rsplit("/", 1)[-1]
                out.append(norm if is_file else norm + "/")
            return out

        nodes = [d for d in (domains or []) if isinstance(d, dict) and d.get("name")]
        # **先把范围算出来**（`file_domains`/`files`/`file` 都可能是范围声明的载体），
        # 再清空这些字段——顺序反了就会把范围源清掉、所有文件都掉进机械桶
        # （实测：整批被误判"材料过期"回退，工作流空转一轮）。
        node_scopes = {id(node): scopes_of(node) for node in nodes}
        for node in nodes:
            node["files"] = []
            # 旧字段 `file_domains`（目录前缀/文件路径）是**范围声明**，不是归宿：
            # 已经当 scope 用过了，这里清掉——否则 `views._file_domain_entries` 的
            # 并集会让"前缀"与"重建后的具体清单"在两个节点上同时命中同一个文件
            # （F2 重叠又回来了）。归宿现在只有一处真源：上面的 `files`。
            node["file_domains"] = []
        notes: list[str] = []
        unmatched: list[str] = []
        for path in live:
            norm = pathmatch_module.norm(path)
            best: tuple[int, int, dict] | None = None   # (深度, 节点序, 节点)
            for order, node in enumerate(nodes):
                for scope in node_scopes[id(node)]:
                    if scope.endswith("/"):
                        if not norm.startswith(scope):
                            continue
                        depth = scope.count("/")
                    else:
                        if norm != scope:
                            continue
                        depth = 1000                       # 精确文件：最高优先
                    if best is None or (depth, -order) > (best[0], -best[1]):
                        best = (depth, order, node)
            if best is None:
                unmatched.append(path)
                continue
            best[2]["files"].append(norm)
        for node in nodes:
            if node["files"] and not str(node.get("description") or "").strip():
                note = self._guess_file_note(node["files"][0])
                if note:
                    node["description"] = note
        # **未命中范围的文件 = 产物覆盖不全 → 本次不分裂**（2026-09-15 用户拍板：
        # "不可以给我错误分裂，错误分裂不如不分裂"）。旧行为是把它们塞进
        # "Runtime 自动归类"的机械桶再照常落地——那等于**用错的树**（模型没看完
        # 材料的产物落了地、还长了 agent）。现在只报告、不补救：`uncovered_by_scope`
        # 交应用侧闭锁（丢弃产物、保留整理结果、下批水位再判断）。
        notes.append(f"文件归属按路径机械绑定（{len(live)} 个活性文件 → "
                     f"{sum(1 for n in nodes if n['files'])} 个节点"
                     f"{f'，{len(unmatched)} 个没被任何节点范围覆盖' if unmatched else ''}）")
        return notes, unmatched

    @staticmethod
    def _auto_claim(domains: list, paths: list[str], field: str,
                    hints: dict[int, list[str]] | None = None) -> list[str]:
        """**Runtime 机械归位**：把没人认领的文件挂到最近的节点下（原地修改）。

        2026-09-15 用户授权（"你看着按最好的来，我只要效果，可以和之前冲突的方法
        实现"）——原先"文件没域认领 → 整批中止"的口径（2026-09-13）让管道在
        "每闭合一轮再失败一次"上空转（实测会话 20260915-131130-010611：两批分裂
        27/29 个文件全未认领、烧掉 134 万 prompt tok 与约 7 分钟 LLM 时间、一个域
        都没长出来）。归位的判据仍是**代码算**（模型给关系、代码算归宿），只是从
        "拒收整批"改成"最近的节点接手 + 留痕"：

        1. 该文件所在目录已有同节点的文件 → 挂那个节点（最像）；
        2. 否则取**最长公共目录前缀**的节点（至少同一顶层目录）；
        3. 全都不沾边 → 新建 Runtime 节点（按顶层目录聚合，名字写明
           "Runtime 自动归类"）——下一批分裂可以把它拆细。

        返回留痕行（谁被自动挂到哪）；文件**绝不会**落到主 agent 名下。
        """
        if not paths:
            return []

        hint_map = hints or {}

        def claimed_of(d: dict) -> list[str]:
            # 节点"已认领的文件"= 它声明过的**全部**文件形态（与
            # `views._file_domain_entries` 同一并集：files/单文件 file/
            # 旧目录前缀 file_domains/history_files）——只读 files 会漏掉
            # 用旧字段声明的节点，归位就会挑错邻居。
            out: list[str] = list(hint_map.get(id(d), []))   # 调用方已知的声明范围
            for key in ("files", "file_domains", "history_files"):
                out += [str(x) for x in (d.get(key) or [])]
            if d.get("file"):
                out.append(str(d["file"]))
            return out

        def shared_dirs(a: str, b: str) -> int:
            pa = pathmatch_module.norm(a).split("/")[:-1]
            pb = pathmatch_module.norm(b).split("/")[:-1]
            n = 0
            for x, y in zip(pa, pb):
                if x != y:
                    break
                n += 1
            return n

        def score(path: str, d: dict) -> int:
            best = 0
            pd = pathmatch_module.norm(path).rsplit("/", 1)[0] if "/" in pathmatch_module.norm(path) else ""
            for f in claimed_of(d):
                s = shared_dirs(path, f)
                if s <= 0:
                    continue
                if pathmatch_module.norm(f).rsplit("/", 1)[0] == pd:
                    s += 100                     # 同目录：几乎肯定就是这个节点
                best = max(best, s)
            return best

        notes: list[str] = []
        strays: list[str] = []
        for path in sorted(paths):
            cands = [(score(path, d), i) for i, d in enumerate(domains)
                     if isinstance(d, dict) and d.get("name")]
            cands = [c for c in cands if c[0] > 0]
            if cands:
                _, i = max(cands)
                domains[i].setdefault(field, [])
                if path not in domains[i][field]:
                    domains[i][field].append(path)
                notes.append(f"{path} → 节点「{domains[i].get('name')}」（同目录/最近）")
            else:
                strays.append(path)
        # 全都不沾边的：按顶层目录聚合，新建 Runtime 节点
        groups: dict[str, list[str]] = {}
        for path in strays:
            norm = pathmatch_module.norm(path)
            top = norm.split("/")[0] if "/" in norm else "根目录"
            groups.setdefault(top, []).append(path)
        for top, files in sorted(groups.items()):
            name = f"{top}（Runtime 自动归类）"
            node = {"name": name, "description":
                    "Runtime 自动归类：分裂产物没有认领这些文件，按顶层目录先挂这里"
                    "（不落主 agent）；下一批分裂可以把它拆细。",
                    # **不生成 agent**（2026-09-15）：它是机械桶、不是语义工作线——
                    # `registry.build_entries` 见到这个标记就跳过（实测：不作限制时
                    # 4 个这样的桶各占一个子 agent，把用户满意的"不过分分裂"破坏）。
                    "runtime_auto": True,
                    "files": [], "history_files": []}
            node[field] = list(files)
            domains.append(node)
            notes.append(f"{'、'.join(files)} → 新建节点「{name}」")
        return notes

    def _live_files(self) -> list[str]:
        """**分裂单元** = 现有文件（Q1 口径：只读与已删的算历史，不做分裂单元）。

        用户口径（2026-09-12）：LIVE = 整理时判定"这份内容现在还在"（被写过、
        没被删/重构）；`read_only`（只读过）与 `dead`（被删/被取代）都挂到最
        相关 LIVE 块下面当历史，**不单独作为分裂单元**。
        """
        return sorted(k for k, g in self._file_groups().items()
                      if g["state"] == "live")

    def _non_live_files(self) -> list[str]:
        """**非 LIVE 文件**（只读过的 + 被删/被取代的）——全部，不看磁盘。

        用户口径（2026-09-14）：它们和活性文件一样**必须挂满**——虽然磁盘上
        可能已经不存在了，但**早期工作的结论**（临时测试脚本验了什么、结果
        如何）后面要装配进上下文，所以必须有落点（挂在相关叶子下面当
        历史记录）。"还在盘上才列"的旧口径已废（那会让已删的临时脚本
        从树里消失，后面装配时找不到它验过什么）。
        """
        return sorted(k for k, g in self._file_groups().items()
                      if g["state"] != "live")

    def _split_hard_data(
        self, rounds: list[dict],
        live_ids: dict[str, str] | None = None,
        hist_ids: dict[str, str] | None = None,
    ) -> tuple[list[str], int]:
        """分裂硬数据（零 LLM）：活性文件清单 + 非 LIVE 文件清单 + 活性数。

        * **活性 = 被写过且现在还在**（只读不算活性：压缩后真正干活还需要
          重新读；它们不占分裂单元、不计入上限）。
        * **非 LIVE 文件也必须挂满**（2026-09-14 用户口径："非 LIVE 文件也是
          必须填满的"——磁盘上没了不等于没用：临时测试脚本验过什么、结果如何，
          是后面装配上下文要用的结论）。
        * 路径按**归一形态**列出（消解 `./` 与 `..`）：同一文件多种写法只列
          一行，模型逐字抄就不会撞歧义。
        """
        groups = self._file_groups()
        if live_ids is None or hist_ids is None:
            live_ids, hist_ids = self._split_file_map()
        path_to_live = {p: i for i, p in live_ids.items()}
        path_to_hist = {p: i for i, p in hist_ids.items()}
        live_lines: list[str] = []
        hist_lines: list[str] = []
        n = 0
        for path in sorted(groups):
            g = groups[path]
            refs = " ".join(g["refs"]) or "无"
            # **轮数**（2026-09-15）：叶子（单文件单轮）不写描述、多轮/多文件的
            # 节点才写满三件——产物体量直接减半，少撞端点输出预算（截断是分裂
            # 失败的头号来源）。轮号从块 ID（`R3-B2`）里机械派生，零额外数据。
            rounds_hit = sorted({str(r).split("-")[0] for r in (g["refs"] or [])
                                 if str(r).startswith("R")})
            rtag = f"轮 {len(rounds_hit)}（{','.join(rounds_hit[:4])}"
            rtag += "…）" if len(rounds_hit) > 4 else "）"
            if g["state"] == "live":
                n += 1
                live_lines.append(
                    f"  - {path_to_live.get(path, 'L??')} = {path}"
                    f"（live；{rtag}；写 {g['write']}/读 {g['read']}；块: {refs}）"
                )
            elif g["state"] == "dead":
                hist_lines.append(
                    f"  - {path_to_hist.get(path, 'H??')} = {path}"
                    f"（已删/被取代；写 {g['write']}/读 {g['read']}；块: {refs}）"
                )
            else:
                hist_lines.append(
                    f"  - {path_to_hist.get(path, 'H??')} = {path}"
                    f"（只读——被读过没写过；读 {g['read']} 次；块: {refs}）"
                )
        lines = [
            "- 活性文件清单（**路径**就是归属依据：节点的 `path`/`paths` 写它所在的"
            "目录前缀或文件路径，Runtime 按**最深前缀**机械分配——你**不用**逐文件"
            "列清单；`Lxx` 只是行号提示，写编号也行。"
            "LIVE=被写过且现在还在，只读/已删的不是分裂单元）："
        ]
        lines += live_lines or ["  （无——当前无任何活性文件）"]
        lines.append(
            f"- 活性文件数：{n}（**不必**每个文件一个节点：按语义给目录级节点即可，"
            "代码会把文件分到最深的那个路径范围内）"
        )
        lines.append(
            f"- 非 LIVE 文件清单（`Hxx` = 编号；共 {len(hist_lines)} 个；"
            "**不是分裂单元、不计入上限**——磁盘上没了不等于没用：它们承载早期工作的"
            "结论，后面装配上下文要用。**归属由 Runtime 机械挂载**（挂到最近的活性"
            "兄弟下），你**不需要**填 history_files）："
        )
        lines += hist_lines or ["  （无）"]
        return lines, n

    def _extract_domains(self, content: str, ordered: list):
        """从分裂分析响应提取产物：优先 submit_domains 调用参数，回退
        正文 JSON。返回 (domains, unassigned, split_assessment) 或 None。

        空 domains 是合法结果（判不可分/全链单子时域列表为空），不能当
        失败丢弃——2026-09-11 实测：合法的"不可分"响应被当无产物，分裂
        静默落空。只有解析不出任何 dict 状态才算无产物。
        """
        state = None
        salvaged = None
        for tc in ordered or []:
            if tc.get("name") != "submit_domains":
                continue
            raw = tc.get("arguments") or "{}"
            try:
                state = json.loads(raw)
            except json.JSONDecodeError:
                # **截断 → 本次不分裂**（2026-09-15 用户拍板，口径变更）：
                # "不可以给我错误分裂，错误分裂不如不分裂"——截断的产物是**残树**，
                # 落下去就是错的域树与错的 agent（实测"抢救出 22 个完整节点"正是
                # 这种残树）。故只**记录**，不落产物：整理结果照常生效，这一批
                # 不产生域树，等下一次水位满时再判断要不要分裂。
                # （旧口径是抠出写完的部分让"至少能落地"——用户判定那是更坏的结果。）
                salvaged = _salvage_state_json(raw) or {}
                state = None
            if isinstance(state, dict):
                break
            state = None
        if state is None:
            parsed = _MaintenanceMixin._parse_state_json(content)
            if not isinstance(parsed, dict) and content:
                salvaged = salvaged or _salvage_state_json(content) or {}
                parsed = None
            state = parsed
        if not isinstance(state, dict):
            if salvaged and self.task is not None:
                n = len(salvaged.get("domains") or [])
                self.task.record(
                    "split_skipped",
                    f"分裂产物被截断（抢救出 {n} 个节点也不可信）——**本次不分裂**："
                    f"保留整理后的结构，等下一次水位再判断是否分裂"
                    f"（用户口径：错误分裂不如不分裂）",
                )
                if self.on_progress:
                    self.on_progress(
                        "⏸ 分裂产物被截断 → 本次不分裂（保留整理结果，下批再判断）")
            return None
        # **空壳判定**（2026-09-11）：截断到只剩 {} 的 arguments 能解析成功，
        # 但产物键一个都没有——那不算"空域合法"，是无产物。
        if not any(
            k in state for k in ("domains", "unassigned", "split_assessment",
                                 "responsibilities")
        ):
            return None
        domains = _MaintenanceMixin._dedupe_domains(state.get("domains") or [])
        # **职责表合并**（2026-09-15 用户口径："分裂只需要写结构树和每个 agent 的
        # 职责"）：职责写在产物的**最后一节**（`responsibilities: {节点名: 职责}`），
        # 树在前、职责在后——流被端点掐断时只丢尾部几条描述（有文件开头兜底），
        # 树与路径完好。以前描述内联在节点里，截断会**连整棵树的那个节点一起丢**。
        merged = _MaintenanceMixin._merge_responsibilities(domains, state)
        if merged and self.task is not None:
            # 留痕：确认产物真的按"末节职责表"写（1,100 tok 的产物里职责占 ~700，
            # 它是"截断只丢描述"这条保证的落点；模型若仍内联，这里就看不到）
            self.task.record(
                "maintenance",
                f"split：职责表合并 {merged} 条（产物末节 `responsibilities`）",
            )
        return (
            domains,
            state.get("unassigned") or {},
            state.get("split_assessment") or {},
        )

    @staticmethod
    def _merge_responsibilities(domains: list, state: dict) -> int:
        """把产物末节的职责并回各节点（原地改）；节点自己写了描述就不覆盖。

        容错三种写法：字典 `{节点名: 职责}`、数组 `[{name, description}]`、
        以及模型把职责直接写在节点上的老写法（不动）。
        """
        resp = state.get("responsibilities")
        pairs: dict[str, str] = {}
        if isinstance(resp, dict):
            pairs = {str(k).strip(): str(v).strip()
                     for k, v in resp.items() if str(v).strip()}
        elif isinstance(resp, list):
            for item in resp:
                if isinstance(item, dict):
                    name = str(item.get("name") or item.get("node") or "").strip()
                    text = str(item.get("description") or item.get("responsibility")
                               or "").strip()
                    if name and text:
                        pairs[name] = text
        if not pairs:
            return 0
        merged = 0
        for d in domains or []:
            if not isinstance(d, dict):
                continue
            if str(d.get("description") or "").strip():
                continue                      # 节点自带描述优先（老写法）
            name = str(d.get("name") or "").strip()
            text = pairs.get(name) or pairs.get(f"{name}（Runtime 自动归类）")
            if text:
                d["description"] = text
                merged += 1
        return merged

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

        def stage(text: str) -> None:
            """往**直播流**报一句阶段状态（2026-09-13 用户报"卡到回答中了"）。

            整理 + 分裂是真发 LLM 调用的（实测一批 106 秒），这段时间一个分片都
            不推 —— 界面停在"回答中…"不动，看起来像卡死。报一句阶段名，让运行线
            说得清在干什么。CLI 侧只是多两行提示，无副作用。
            """
            if self.on_progress:
                self.on_progress(text)

        def run() -> None:
            try:
                stage("整理上下文…（整理批次 R%d-R%d）"
                      % (batch[0]["seq"], batch[-1]["seq"]))
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
                stage("分裂分析…")
                for rr in batch:
                    rr["split_state"] = "running"      # 界面上可见"分裂中"
                self._persist_rounds()
                results["split"] = self._split_rounds(
                    batch, box.get("exchange"), base
                )
                if results["split"]:
                    for rr in batch:
                        rr["split_state"] = "ready"    # 产物就绪、待轮边界生效
                    self._persist_rounds()
                    # **A 步：产物就绪即落**（2026-09-15）——注册表/split_state/回水位
                    # 都不碰上下文字节，不必等轮边界；只有 `domains` 等边界（见方法注释）
                    self._publish_product_early()
                stage("整理完成")
            except Exception as error:  # noqa: BLE001
                for rr in batch:
                    rr["split_state"] = "failed"
                self._persist_rounds()
                if self.task is not None:
                    self.task.record(
                        "maintenance", f"split 阶段失败：{str(error)[:150]}"
                    )
                # **漏认领是约束性失败**（2026-09-13 用户口径："直接给我报错，不要
                # 往下进行了"）：此刻 org 那一路已经把本批标成 `done` 了，若只记一笔，
                # 这些轮**永远不会再被分析**（批次选取排除 `done`）→ "主 agent 有文件"
                # 就永久留在那儿。故把本批**回入水位**（撤暂存 + 判 failed），下批重做。
                if isinstance(error, SplitCoverageError):
                    for r in batch:
                        r.pop("pending_org", None)
                        r["org_state"] = "failed"
                    self._persist_rounds()

        thread = threading.Thread(
            target=safety_module.workspace_bound_target(run),
            name="wovra-maintenance", daemon=True,
        )
        # 落盘由 `_persist_rounds` 统一"先重读盘 + 并集合并"（§86）：整理改异步
        # 之后，维护线程（旧 agent）与下一轮的 agent 会同时写 task.json，而两边写
        # 的都是整份文件——不合并就会互相覆盖（最坏：刚算出来的分裂产物被抹掉）
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
        """把暂存的整理产物落进正式视图（轮闭合边界 / 新 Round 开启时调用）。

        产物为什么不直写：轮进行中改装配会破坏缓存前缀（AGENTS.md §2）；
        故维护只写各轮的 `pending_org` 暂存区，由**没有开放轮的时刻**落地
        ——`close_round` 边界的 `_settle_after_maintenance`（§50 即时生效，
        主流路径），以及新 Round 开启时的兜底（异步维护跑完时用户已经进了
        下一轮，产物只能等下一次轮开启）。崩溃安全：`pending_org` 随 rounds
        一起持久化，重启后第一次开新轮时补生效。
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
                # **入口校验**（2026-09-12 用户口径，worklog §62）：F2 一个文件
                # 只能一个域、F3 现有文件必须 100% 分完。不过就**拒收**——不落
                # 注册表、不写 `r["domains"]`、不留 `pending`，并落一条醒目错误。
                # 为什么不静默兜底：用户原话"有些错误是根基，其错了，我下面
                # 测试无意义"，静默吸进主 agent 桶正是把这类根基错误藏起来。
                # 2026-09-14：活性文件必须 100% 分完；**非 LIVE 文件也必须
                # 挂满**（用户口径：磁盘上没了不等于没用——早期结论后面要
                # 装配进上下文），由 split_defects 的 F3-历史检查兜底。
                # **材料过期判定**（不拒收、但仍要识别）：产物声明的范围覆盖
                # 不到当前材料 + 有更新轮次 → 本批回水位，下批带新材料重做。
                if self._skip_incomplete_split(
                        r, list(pending.get("uncovered_by_scope") or [])):
                    continue
                defects = registry_module.split_defects(
                    pending["domains"], self._live_files(), self._non_live_files()
                )
                if defects:
                    r.pop("domains", None)
                    self._split_defects = defects
                    # **材料过期 ≠ 根基缺陷**（2026-09-15 修，会话 20260914-181519-0d3875
                    # 实测）：产物是对**某一批材料**做的；之后又有轮闭合（新文件/新改动）
                    # 时，产物必然覆盖不全。旧行为把这判成"根基缺陷"拒收，而那批轮留在
                    # `org_state=done`——它们再也不会被选进整理批次，于是会话永久没有可用
                    # 域树（实测就卡在这里：R1-R6 done、产品被拒、R7 另起一批也覆盖不全）。
                    # 判据：① 缺陷**全是"未覆盖"**（覆盖不全，非 F2 重叠/空域这类结构错）；
                    # ② 存在**不属于本批**的已闭合轮（材料确实变了）→ 回入水位，下批重整。
                    for rr in self.rounds:
                        if (str(rr.get("org_state") or "") == "done"
                                and rr.get("org_generation") == r.get("org_generation")):
                            rr["split_state"] = "rejected"
                    if self.task is not None:
                        self.task.record(
                            "split_defect",
                            "分裂产物被拒收（根基缺陷，需人工查整理/分裂/重组）："
                            + "；".join(defects[:8]),
                        )
                    if self.on_progress:
                        self.on_progress(
                            "⛔ 分裂产物被拒收：" + "；".join(defects[:3])
                        )
                    continue
                # **落点预演**（2026-09-13，worklog §78）：产物内部合规 ≠ 落进
                # 注册表后合规。`split_defects` 只看新域树自己，看不见注册表里
                # 早已据着同一批文件的旧条目——实测曾把 F2 破坏（一个文件同时
                # 挂在主 agent 与子域名下）静默合并进注册表。故这里先做纯投影：
                # 结算归属 + 跑**跨条目**互斥体检，不通过就与内部缺陷同等拒收。
                parent = str(pending.get("split_parent") or "")
                if not parent:
                    parent = str((self.current_round or {}).get("active_view") or "")
                parent_id = self._product_parent_id(parent)
                projected: list[dict] = []
                added: list[str] = []
                updated: list[str] = []
                settle_lines: list[str] = []
                if self.task is not None:
                    projected, added, updated, settle_lines = (
                        registry_module.project_merge(
                            self.task.registry, pending["domains"],
                            parent_id=parent_id, retire_id=parent_id,
                        )
                    )
                    cross = registry_module.registry_defects(projected)
                    if cross:
                        r.pop("domains", None)
                        self._split_defects = cross
                        if self.task is not None:
                            self.task.record(
                                "split_defect",
                                "分裂产物被拒收（落点会造成跨条目 F2 违反）："
                                + "；".join(cross[:6]),
                            )
                        if self.on_progress:
                            self.on_progress(
                                "⛔ 分裂产物被拒收（归属重叠）：" + "；".join(cross[:3])
                            )
                        continue
                r["domains"] = pending["domains"]
                for rr in self.rounds:
                    if (str(rr.get("org_state") or "") == "done"
                            and rr.get("org_generation") == r.get("org_generation")):
                        rr["split_state"] = "done"
                # 组织层落地点（2026-09-11 用户拍板 A：Level 1 视图分化）：
                # 域树 → 注册表条目是**机械翻译**（语义归模型、体量归机制）。
                # 幂等合并：崩溃补做/重启重放只更新既有条目。注册表在此
                # 才第一次长出主 agent 之外的条目——此前永远只有 A。
                if self.task is not None:
                    # 已在 A 步（产物就绪即落）提前落过 → 不重复落/记账；
                    # `domains` 仍等边界（见 `_publish_product_early`）。
                    # 生命周期记录**恒跑**：它描述的是域树本身，与落在哪一步无关。
                    if not pending.get("registry_landed"):
                        registry_module.land(self.task.registry, projected)
                        if settle_lines:
                            self.task.record(
                                "maintenance",
                                f"归属结算：{len(settle_lines)} 个文件的清单从旧条目"
                                "移到新域（" + "；".join(settle_lines[:6])
                                + ("…" if len(settle_lines) > 6 else "") + "）",
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
                    self._record_split_lifecycle(pending["domains"])
            if pending.get("unassigned"):
                r["unassigned"] = pending["unassigned"]
            if pending.get("split_assessment"):
                r["split_assessment"] = pending["split_assessment"]
            patch = pending.get("state_patch")
            if patch and self.task is not None:
                report = self.task.apply_state_patch(patch)
                self._record_close_report(report)
        applied = self._apply_staged_views()
        if changed or applied:
            self._persist_rounds()

    def _split_coverage_lines(self, rounds: list[dict]) -> list[str]:
        """覆盖缺口硬数据（零 LLM）：未覆盖文件 + 多域共命轮（2026-09-12）。

        见 `views.coverage_gap` 的动机（worklog §40.4 待办乙 + §44.3-5）。给模型
        的用途：① 有文件没被任何域认领 = 本批分域的漏项，可自纠；② 多个域共命
        同一批轮 = 父域与子域语义重叠（或分域过细）——它直接抬高视图切换频率、
        压低粘滞率（实测 61.5% vs 目标 >80%）。
        """
        domains = registry_module.latest_domains(self.rounds)
        gap = views_module.coverage_gap(domains, rounds)
        lines: list[str] = []
        uncovered = gap.get("uncovered") or []
        if uncovered:
            shown = "、".join(uncovered[:10]) + ("…" if len(uncovered) > 10 else "")
            lines.append(
                f"- 分裂覆盖缺口：{len(uncovered)} 个文件未落在任何域"
                f"（掉主 agent 兜底桶）：{shown}"
            )
        else:
            lines.append("- 分裂覆盖缺口：无（所有文件块都有域认领）")
        over = gap.get("overlapped_rounds") or []
        if over:
            counts = gap.get("round_domain_counts") or {}
            shown = "、".join(f"R{s}（{counts.get(s, 0)} 域）" for s in over[:10])
            lines.append(
                f"- 多域共命轮：{len(over)} 轮被 ≥2 个域同时命中"
                f"（父子域重叠或分域过细的信号）：{shown}"
            )
        return lines

    def _view_assembly_watermarks(
        self, domains: list, watermark: Optional[int] = None
    ) -> dict[str, dict]:
        """各视图的**装配口径**体量（另存材料口径）+ 是否到自身水位。

        口径订正（2026-09-12，worklog §44.3-3）：`views.view_watermarks` 给的是
        **材料口径**（块的一行式重建），而水位与经济判据里的 `B` 是**装配口径**
        （`last_context_estimate`，含块内事件全文）——二者实测差 12–17.7×。
        混用的后果是同一份材料给出方向相反的两个结论（仪器报"值得拆"，按装配
        口径应为负）。故此处以装配口径为准：逐视图调 `_assemble_view_messages`
        实测（零 LLM、毫秒级；与 `maint_health` 的 A/B 行同一函数、同一算法）。

        派生失败时退回材料口径并在 `degraded` 标出——宁可标注"口径降级"，
        也不静默换一个不同源的数（§40.2 教训：仪器给错数比没数更糟）。
        """
        material = views_module.view_watermarks(
            self.rounds,
            self.task.get_state() if self.task is not None else None,
            domains=domains,
            registry=(self.task.registry if self.task is not None else None),
        )
        out: dict[str, dict] = {}
        for name, mark in material.items():
            item = dict(mark)
            base = int(mark.get("tokens") or 0)
            item["material_tokens"] = base
            item["tokens"] = base
            item["degraded"] = True
            try:
                msgs = self._assemble_view_messages(name, self.rounds)
            except Exception:  # noqa: BLE001——派生失败退回材料口径（带标注）
                msgs = None
            if msgs:
                item["tokens"] = int(self._estimate_messages(msgs))
                item["degraded"] = False
            if watermark is not None:
                item["over"] = item["tokens"] >= int(watermark)
            out[str(name)] = item
        return out

    def _split_view_watermarks(self) -> list[str]:
        """逐层分裂的硬数据行（零 LLM）：各视图自身体量 + 是否到自己的水位。

        水位口径（plan §13.1）：**分裂后水位按视图各自计量**——故子视图到达
        自己的水位时按同一套机制在它内部再裂一层（`A-1` → `A-1-1`），终态
        「只操作单个文件为止」。判据归机制（Runtime 给体量事实）、语义归模型
        （这一摊活是否真已分成互不相干的两条线）。

        **口径 = 装配口径**（2026-09-12 订正）：与水位、`B` 同源；括号内附材料
        口径供对照。主 agent 不出现在这里——它是兜底桶，不参与「拆不拆自己」。
        """
        domains = registry_module.latest_domains(self.rounds)
        if not domains:
            return []
        try:
            marks = self._view_assembly_watermarks(domains, self._org_watermark)
        except Exception as error:  # noqa: BLE001——硬数据不可用不该让分裂分析失败
            # 不静默：口径降级/不可用必须留痕，否则模型是在"没有体量事实"的
            # 情况下做分裂判断，而人看不见这件事（worklog §44.3-4 的教训）。
            if self.task is not None:
                self.task.record(
                    "maintenance",
                    f"分裂硬数据不可用（{error!r}），本批分裂按无体量事实进行",
                )
            return []
        lines = ["- 各视图自身体量（装配口径；括号内为材料口径，两者不可混用）："]
        for name, mark in sorted(
            marks.items(), key=lambda kv: -int(kv[1].get("tokens") or 0)
        ):
            if name == views_module.MAIN_AGENT_ID:
                continue
            over = "**已到自身水位 → 考虑在其内部再裂一层**" if mark.get("over") \
                else "未到水位（不拆）"
            degraded = "（口径降级：仅材料口径）" if mark.get("degraded") else ""
            lines.append(
                f"  - {name}：{int(mark.get('tokens') or 0):,} tok"
                f"（材料 {int(mark.get('material_tokens') or 0):,}）"
                f"／{int(mark.get('blocks') or 0)} 块／活跃 {int(mark.get('rounds') or 0)} 轮"
                f"{degraded} → {over}"
            )
        if len(lines) == 1:
            return []
        return lines

    def _record_split_lifecycle(self, domains: list) -> None:
        """分裂生命周期动作 + 经济判据（机械算式，零 LLM，2026-09-12）。

        依据 plan §13.2/§13.3：四动作里第一版只做 split / no split，
        且「发现职责」与「创建 Agent」是两个动作——故这里只**算与记**：
        * 每个域一个动作（注册表里没有 = 本批发现的职责 → split；
          已有 = 延续 → no split）；
        * status 按材料事实落（该域名下有命中轮 → active，否则 dormant）；
        * 经济判据 `(B − B′) × N_future − C_split` 逐域给读数，
          为负者只记录不拆（不是错误，是"现在还不值得拆"）。

        `B` 取本轮装配体量实测值（`last_context_estimate`）；`B′` 取该域视图
        **装配口径**体量（与 `B` 同源；2026-09-12 订正前用材料口径，二者实测差
        12–17.7×，见 worklog §44.3-3）；`N_future` 取该域活跃轮数（保守下界：
        它已被用了这么多轮，未来至少还会用这么多）。
        """
        if self.task is None:
            return
        try:
            marks = self._view_assembly_watermarks(domains)
        except Exception as error:  # noqa: BLE001——记账失败不能拖垮 promote
            self.task.record(
                "maintenance", f"分裂生命周期：视图体量不可用（{error!r}），跳过记账"
            )
            return
        actions = split_lifecycle_module.plan(
            domains, self.task.registry, marks,
            b_before=int(self.last_context_estimate or 0),
        )
        changed = split_lifecycle_module.apply_status(self.task.registry, actions)
        if changed:
            self.task.record(
                "maintenance",
                f"registry：运行时状态更新（{len(changed)} 条）——"
                + "、".join(changed),
            )
        assessed = economics_module.assess_from_watermarks(
            int(self.last_context_estimate or 0), marks
        )
        for line in economics_module.format_lines(assessed):
            self.task.record("maintenance", line)
        for line in split_lifecycle_module.summary_lines(actions):
            self.task.record("maintenance", line)

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
