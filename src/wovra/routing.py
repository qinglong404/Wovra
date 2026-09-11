"""路由：这一轮该由谁接（Level 1 第三步，纯函数、零 LLM）。

## 路由的输入只有职责表（2026-09-12 用户拍板：隔离第一）

隔离生效后，主 agent **看不到**子域的内容——连模糊的大概都看不到。于是
路由不可能靠"读一读哪份材料更像"，唯一允许的输入是**分裂时写下的职责**
（domain 的 name/description/goal/file_domains，机械翻译进注册表条目）。
本模块把"这一轮的用户消息"与这张职责表对上，产出 `view` 名字。

## 判定顺序（先定后判，逐步降级）

1. **显式转交**（`switch_view` 工具 / 上一轮接活视图用 notify 交出去的活）
   ——人工与机制的直接意志，最高优先；
2. **文件命中**：用户消息里出现某域 `file_domains` 的路径或文件名 → 交给它；
   命中多个域（一句话跨两摊活）→ 第一版交回主 agent（多域命中不猜）；
3. **粘滞**：都没命中时留在上一轮的视图里——同一摊活连着干是常态，
   每轮重判只会让前缀反复断裂（粘滞是钱的问题，也是连贯性的问题）；
4. **兜底主 agent**：环境准备、纯讨论、跨域闲聊天然归它。

## 误路由是可以纠正的（不是一次判死的分类）

路由准确率不必 99%：接活视图发现"这不是我的活"时，用 `notify` 把它转给
更相关方（v4 §4.1），下一轮即切换。只付一轮白做 + 一条消息的代价——
故规则宁可精，不必贪全。
"""
import os
from typing import Iterable, Optional

from .registry import MAIN_AGENT_ID

# 视图分化的总开关（2026-09-12）：默认关——装配行为与今天逐字节相同。
# 打开（重启后手动设 WOVRA_ACTIVE_VIEW=1）才让 active_view 参与装配。
ACTIVE_VIEW_ENV = "WOVRA_ACTIVE_VIEW"


def active_view_enabled() -> bool:
    """视图分化开关（env，默认关；关闭时路由结果不影响装配）。"""
    value = (os.environ.get(ACTIVE_VIEW_ENV) or "").strip().lower()
    return value in ("1", "true", "yes", "on")


def _entries(registry: Optional[Iterable[dict]]) -> list[dict]:
    return [e for e in (registry or []) if isinstance(e, dict) and e.get("name")]


def _find(registry: Optional[Iterable[dict]], ref: str) -> Optional[dict]:
    """按 id 或名字查注册表条目（路由产物统一给**名字**，与域树口径一致）。"""
    want = str(ref or "").strip()
    if not want:
        return None
    for entry in _entries(registry):
        if want in (str(entry.get("id") or ""), str(entry.get("name") or "")):
            return entry
        if want.lower() == str(entry.get("id") or "").lower():
            return entry
    return None


def _needles(file_domains: Iterable[str], extra_files: Iterable[str] = ()) -> list[str]:
    """文件域 → 可在用户消息里直接找的串。

    取三种形态（都在"能指向一处"的前提下尽量宽松）：
    * **完整路径**（`src/wovra/tools/`）——最精确；
    * **目录尾段带斜杠**（`tools/`）——用户常说「tools 里那个」；带斜杠
      比裸词（`tools`）安全得多，`src/`、`tests/` 这类泛目录也仍可能误伤，
      故只在目录名长度 ≥3 时收录；
    * **带扩展名的文件名**（`safety.py`）——来自 `file_domains` 里的具体
      文件，或材料侧给的 `extra_files`（该域名下真实出现过的文件）。

    **不收**裸的无扩展名目录末段（`tools`）：中文对话里太容易误命中。
    """
    out: list[str] = []

    def _add(token: str) -> None:
        if token and token not in out:
            out.append(token)

    for raw in list(file_domains or []) + list(extra_files or []):
        path = str(raw).strip().strip("/").replace("\\", "/")
        if not path:
            continue
        _add(path)
        tail = path.rsplit("/", 1)[-1]
        if "." in tail:
            _add(tail)                     # 具体文件：文件名形态
        elif len(tail) >= 3:
            _add(tail + "/")               # 目录：尾段带斜杠
    return out


def match_domains(
    text: str,
    registry: Optional[Iterable[dict]] = None,
    file_hints: Optional[dict[str, Iterable[str]]] = None,
) -> list[str]:
    """用户消息文件命中的域名列表（保序、去重；多命中由调用方处置）。

    `file_hints` = 域 → 该域名下**真实出现过的文件**（材料侧机械统计，
    见 `file_hints`）；它让「safety.py 加个白名单」这种只提文件名、不写
    路径的说法也能命中。缺失（离线/无材料）时退化为只看职责表。
    """
    flat = str(text or "").replace("\\", "/")
    if not flat:
        return []
    hints = file_hints or {}
    hits: list[str] = []
    for entry in _entries(registry):
        if str(entry.get("id")) == MAIN_AGENT_ID:
            continue  # 主 agent 的文件域是"别人的补集"，不参与命中
        needles = _needles(
            entry.get("file_domains") or [],
            hints.get(str(entry.get("name"))) or [],
        )
        for needle in needles:
            if needle and needle in flat:
                name = str(entry["name"])
                if name not in hits:
                    hits.append(name)
                break
    return hits


def route(
    user_input: str,
    registry: Optional[Iterable[dict]] = None,
    *,
    sticky: str = "",
    explicit: str = "",
    file_hints: Optional[dict[str, Iterable[str]]] = None,
) -> dict:
    """给这一轮定一个视图。返回 `{"view", "reason", "matched"}`。

    `view` 恒为注册表里的名字（兜底 `"A"` = 主 agent）；`reason` 是给
    history 留痕的一句话（人视图可查"这轮为什么给了它"）。
    """
    entries = _entries(registry)
    if not entries:
        return {"view": MAIN_AGENT_ID, "reason": "无注册表条目，交主 agent", "matched": []}

    # 1. 显式转交
    target = _find(entries, explicit)
    if target is not None:
        return {
            "view": str(target["name"]),
            "reason": f"显式转交 → {target.get('id')}({target['name']})",
            "matched": [],
        }

    # 2. 文件命中
    hits = match_domains(user_input, entries, file_hints)
    if len(hits) == 1:
        entry = _find(entries, hits[0])
        return {
            "view": str(entry["name"]),
            "reason": f"文件命中：{hits[0]}",
            "matched": hits,
        }
    if len(hits) > 1:
        return {
            "view": MAIN_AGENT_ID,
            "reason": f"多域命中（{'、'.join(hits)}）→ 交主 agent（不猜）",
            "matched": hits,
        }

    # 3. 粘滞
    stay = _find(entries, sticky)
    if stay is not None and str(stay.get("id")) != MAIN_AGENT_ID:
        return {
            "view": str(stay["name"]),
            "reason": f"粘滞：延续上一轮视图 {stay.get('id')}({stay['name']})",
            "matched": [],
        }

    # 4. 兜底
    return {"view": MAIN_AGENT_ID, "reason": "无命中，交主 agent", "matched": []}


def responsibility_lines(registry: Optional[Iterable[dict]]) -> list[str]:
    """全局职责表（注册表机械渲染，一行一条；零 LLM）。

    这是**跨 agent 的唯一公共信息**：路由只读它，主 agent 也只看得到它
    （隔离第一）。故它必须自足——id、名字、一句话职责、所有权文件域。
    """
    lines: list[str] = []
    for entry in _entries(registry):
        fds = "、".join(str(f) for f in (entry.get("file_domains") or [])) or "未划定"
        desc = str(entry.get("description") or "").strip()
        status = str(entry.get("status") or "dormant")
        lines.append(
            f"- {entry.get('id')}（{entry.get('name')}）：{desc or '（无描述）'}"
            f"｜文件域：{fds}｜状态：{status}"
        )
    return lines


def identity_card(name: str, registry: Optional[Iterable[dict]]) -> list[str]:
    """本视图的身份与约束段（第 3 段；只含自己的职责，不含别人的内容）。"""
    entry = _find(registry, name)
    if entry is None:
        if str(name) == MAIN_AGENT_ID:
            return [
                "[当前身份] A（主agent）：全局协调与未归属事务"
                "（环境准备、独立思想、零散块）。路由不了的活、跨域的活、"
                "以及用户没说清落在哪一摊的活都由你接。"
            ]
        return [f"[当前身份] {name}（注册表无此条目——请核对分裂产物）"]
    fds = "、".join(str(f) for f in (entry.get("file_domains") or [])) or "未划定"
    lines = [
        f"[当前身份] {entry.get('id')}（{entry.get('name')}）："
        f"{entry.get('description') or '（无描述）'}"
    ]
    if entry.get("goal"):
        lines.append(f"目标：{entry['goal']}")
    lines.append(f"所有权文件域：{fds}")
    lines.append(
        "隔离纪律：你只看到属于自己这摊活的历史与账目——其他职责域的内容"
        "不在你的上下文里（这是设计，不是缺失）。如果这一轮不是你的活，"
        "用 notify 把它转给更相关的 agent（下一轮生效）；需要别人的事实，"
        "用 consult 问属主域。"
    )
    return lines
