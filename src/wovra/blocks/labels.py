"""块标签：生命周期标签（创建/重构/修改/读/只读/删除）→ 标签行。

标签是**分辨率指令**（告诉整理模型这块该写多细），不是重要性评分。
"""
import json
from .segment import _block_kind

def lifecycle_tags(b: dict, versions_before: dict) -> str:
    """文件块的生命周期标签（可多标签，按定义拼接）：

    创建——write_file 且之前无此文件；
    重构——write_file 但文件已存在（整体重写）；
    修改——edit_file / replace_lines（编辑修改，非清空重写）；
    读——有 read 操作且其后有写/改/删（读服务于后续动作）；
    只读——只进行了 read 操作；
    删除——删除文件操作。
    """
    ops = b.get("ops") or []
    has_write = any(o["op"] == "write" for o in ops)
    has_edit = any(o["op"] == "edit" for o in ops)
    has_delete = any(o["op"] == "delete" for o in ops)
    reads = [i for i, o in enumerate(ops) if o["op"] == "read"]
    leading_read = any(
        any(ops[j]["op"] in ("write", "edit", "delete")
            for j in range(i + 1, len(ops)))
        for i in reads
    )
    tags = []
    if has_write:
        tags.append(
            "创建" if versions_before.get(b.get("file", ""), 0) == 0 else "重构"
        )
    if has_edit:
        tags.append("修改")
    if leading_read:
        tags.append("读")
    elif reads and not has_write and not has_edit and not has_delete:
        tags.append("只读")
    if has_delete:
        tags.append("删除")
    return "，".join(tags)

def state_label(ledger_state: str) -> str:
    """文件状态显示：live→LIVE / dead→DEAD / read_only→LIVE（存在即可用）。
    HISTORICAL 预留：文件被另一文件取代时（move/重构替换）出现。"""
    return {"live": "LIVE", "dead": "DEAD"}.get(ledger_state, "LIVE")

def label_line(b: dict, versions_before: dict, state: str,
               round_has_tools: bool = False) -> str:
    """块标签行（只打标签，不写内容）。

    工具标签（2026-09-09 用户拍板：块级精确，Runtime 消解归属）：
      * 文件块 —— 验证/执行类工具确定性吸收进具体块，谁吸收谁打
        「工具」（has_tools 字段），模型不用猜归属；
      * 保底块（无文件交互）调用工具了 → 打「工具」（轮级判定）。
    """
    if b["kind"] == "fallback":
        return "【保底块】：" + ("工具" if round_has_tools else "")
    if b["kind"] == "environment":
        return "【环境块】："
    if b["kind"] == "user":
        return "【用户块】："
    if b.get("fail_tags"):
        # 失败块：幽灵=读文件不存在（从未存在）/ 越界=路径越界被拦
        label = f"【{b['file']}(DEAD)】：{'，'.join(b['fail_tags'])}"
    else:
        label = f"【{b['file']}({state})】：{lifecycle_tags(b, versions_before)}"
    if b.get("has_tools"):
        label += "，工具"
    return label

def round_has_tool_calls(r: dict) -> bool:
    """本轮是否有工具类调用（run_command 非环境、调研类 list_files/
    web_fetch、账本类 todo 等；文件操作与环境命令不算——它们各有块
    承载）：保底块（无文件交互）调用工具了 → 打「工具」标签。"""
    for event in r.get("events") or []:
        if event.get("type") != "tool_call":
            continue
        for call in (event.get("message") or {}).get("tool_calls") or []:
            fn = call.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            if _block_kind(str(fn.get("name") or ""), args) == "tool":
                return True
    return False

def block_end_state(ledger_state: str, b: dict) -> str:
    """块结束时文件状态（零 LLM，供块标签行显示）：块内删除→dead
    （文件最终不在了）；写/改→live；只读→沿用轮前 ledger 状态
    （read_only/live/dead）。fail_tags（幽灵/越界）由 label_line
    强制 DEAD。"""
    if b.get("fail_tags"):
        return "dead"
    ops = b.get("ops") or []
    if any(o["op"] == "delete" for o in ops):
        return "dead"
    if any(o["op"] in ("write", "edit") for o in ops):
        return "live"
    return ledger_state
