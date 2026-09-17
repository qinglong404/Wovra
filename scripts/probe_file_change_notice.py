"""把"你读过的文件被改了"渲染成一条注入通知（零 LLM，只读）。

动机（2026-09-17 用户口径）："读文件后，如果发生改变，下次输入指令时，将其当作提示，
注入到用户内容上面，告诉其之前读过的文件进行了修改，修改了哪些行。方便其快速少量的更新。"

现状已有的原料（都在仓库里，不用新造）：
* `tools/files.py::_file_registry`——观察到的 (mtime_ns, size)，`_stale_error` 拿它**拒绝**
  过期写入（"写到一半才发现"）；
* `.wovra/history/<路径替换 __>/<时间戳>.bak`——每次覆盖/编辑前的旧内容（`_HISTORY_KEEP=10`），
  **这就是算 diff 的原料**；
* `difflib`（stdlib）。

本脚本用真实归档版本 + 真实当前文件，渲染这条通知长什么样（含行数上限与续读指引）。

用法：

    uv run --no-sync python scripts/probe_file_change_notice.py <工作区> <相对路径> [--lines 14]

脚本不删（AGENTS.md §0.3）。
"""
from __future__ import annotations

import argparse
import difflib
from pathlib import Path


def archived(workspace: Path, rel: str) -> list[Path]:
    slot = workspace / ".wovra" / "history" / rel.replace("/", "__")
    return sorted(slot.glob("*.bak")) if slot.is_dir() else []


# 两种归属的措辞（用户口径 2026-09-17：外部改动**不预判对错**——可能是加错一个符号导致的
# 报错、可能是误删，**也可能是高质量修改**；只陈述事实 + 让人自己判断）
_ATTRIB = {
    "tool": "这条改动来自工具写入（某个 agent 改的）",
    "user": ("**这是用户操作，不是工具写的**——可能是有意的高质量修改，也可能是不小心的"
             "（例如多加一个符号导致报错、误删了一段）；**不要当成权威版本，也不要当成错误**，"
             "先看差异再决定：顺着它改、改回去、还是先问一句"),
}


def notice(workspace: Path, rel: str, lines: int = 14, observed: str = "",
           attrib: str = "tool") -> str:
    target = workspace / rel
    if not target.is_file():
        return f"[文件变更·{attrib}] {rel}：现在不存在（可能被删/改名）——先 list_files 确认"
    versions: list[Path] = []
    if observed and Path(observed).is_file():
        old_text = Path(observed).read_text(encoding="utf-8", errors="replace")
        source = f"你的观察副本 {Path(observed).name}"
    else:
        versions = archived(workspace, rel)
        if not versions:
            return (f"[文件变更·{attrib}] {rel}：内容变了，但你上次观察时的副本不在"
                    f"——只能提示你重读，给不了行级差异")
        old_text = versions[-1].read_text(encoding="utf-8", errors="replace")
        source = f"你上次观察时的版本 {versions[-1].name}"
    old = old_text.splitlines()
    new = target.read_text(encoding="utf-8", errors="replace").splitlines()
    diff = list(difflib.unified_diff(old, new, n=1, lineterm=""))
    added = sum(1 for x in diff if x.startswith("+") and not x.startswith("+++"))
    removed = sum(1 for x in diff if x.startswith("-") and not x.startswith("---"))
    head = (f"[文件变更·{'用户操作' if attrib == 'user' else '工具'} ] {rel}（{source}）\n"
            f"  改动：+{added} −{removed} 行（{len(old)} → {len(new)} 行）\n"
            f"  {_ATTRIB.get(attrib, _ATTRIB['tool'])}")
    body = diff[2:2 + lines]                       # 去掉 ---/+++ 两行头
    more = max(0, len(diff) - 2 - lines)
    tail = []
    if more:
        tail.append(f"（还有 {more} 行差异未显示）")
    if len(versions) > 1:
        tail.append(f"（该文件共 {len(versions)} 份历史版本，更早的改动可能也影响你手里的内容）")
    tail.append("（只要改动处就够用，别整文件重读；要全文再 read_file）")
    return head + "\n" + "\n".join(body) + "\n" + "\n".join(tail)


def main() -> int:
    ap = argparse.ArgumentParser(description="渲染一条'文件变更'注入通知")
    ap.add_argument("workspace")
    ap.add_argument("relpath")
    ap.add_argument("--lines", type=int, default=14)
    ap.add_argument("--observed", default="", help="观察时的那份副本（不给则取最新归档版本）")
    ap.add_argument("--attrib", default="tool", choices=("tool", "user"),
                    help="改动归属：tool=某个 agent 写的；user=用户/外部改的")
    args = ap.parse_args()
    print(notice(Path(args.workspace), args.relpath, args.lines, args.observed, args.attrib))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
