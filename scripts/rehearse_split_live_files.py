"""V4 分裂演练：**只按活性文件画结构树**（真跑一次，输入输出都打出来）。

与现状分裂的区别（2026-09-17 用户口径："只需要管活性文件的结构树即可。其它一律不用管。
不需要管用户块了，环境了，什么什么保底了。只需要根据活性文件画出结构树就可以了"）：

* 输入：**活性文件清单**（路径 + 首行说明）——没有块地图、没有用户块/环境块/保底块、
  没有非 LIVE 文件挂载、没有重组上下文；
* 输出：**结构树（节点用 path 声明范围）+ 顶层节点职责**；
* 其余全由代码机械做：文件归属按**最深的 path 前缀**匹配、文件级描述取文件开头。

用法（项目根执行）：

    .venv/bin/python scripts/rehearse_split_live_files.py <会话ID> --dry   # 只打输入
    .venv/bin/python scripts/rehearse_split_live_files.py <会话ID>         # 真跑一次

约定与 AGENTS.md 一致：**只动副本**，原会话一个字节不碰。脚本不删（§0.3）。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from wovra import blocks as blocks_module            # noqa: E402
from wovra import lifecycle as lifecycle_module      # noqa: E402
from wovra.tools import safety as safety_module      # noqa: E402
from wovra import task as task_module                # noqa: E402
from wovra.llm import LLM                            # noqa: E402

REHEARSAL_ROOT = Path("/tmp/wovra-split-live/tasks")

_INSTRUCTION = """[分裂指令]
下面列出的是本工作区**当前有效的文件**（活性文件：路径 + 首行说明）。请按功能语义把它们
聚合成一棵结构树，并给会成为 agent 的节点写职责。

1. 每个节点填 name + parent（顶层留空）+ `path`（或 `paths`）。`path` 就是它的**范围**：
   可以是目录前缀（`gaia_bench/`），也可以是单个文件（`output/gaia/FINDINGS.md`）。
   文件归属由代码按**最深的 path 前缀**机械匹配，**你不用列文件清单**。
2. 职责只写**会成为 agent 的节点**（顶层那几个），写成 `{节点名: 职责}`，每个 ≤60 字，
   写满三件：①干什么 + 产出什么（点出关键文件）；②边界——什么不归我；③什么信号落到我这里。
3. 树要**覆盖全部活性文件**：每个文件都得能被某个节点的路径匹配到，别漏。
4. **不要管用户发言、环境准备、闲聊、临时脚本**——只按文件本身的组织结构分。
5. **什么时候不该单独成域**（2026-09-17 用户口径：`output/gaia/FINDINGS.md` 被单列成域是
   "过分分裂"）——判据是"**这个域能不能独立接活**"，不是"这个文件在不在单独的目录里"：
   * `docs/`、`output/` 这类**目录名不是职责**；
   * 一个文件的**产出工作属于谁，它就归谁**：写 `output/gaia/FINDINGS.md` 要读 GAIA 的全部
     内容、是跑完评测后的汇总 → 它属于 GAIA 那个域，**不要单独成域**；反过来，
     `docs/prompt-review-*.md` 是由**另一条线**独立产出的，才单独成域。
   * 自查一句：**这个域要干活时，是不是得频繁去读别的域的文件、或频繁问别的域？** 是 → 别拆。
   * **顶层域一般是"一个有产出的工作线"，不是一个文件**；把产出与它所服务的工作放在一起。

完成后调用 submit_live_tree 提交（唯一出口，不要在正文输出 JSON）。"""

_SCHEMA = {
    "type": "function",
    "function": {
        "name": "submit_live_tree",
        "description": "提交按活性文件画出的结构树 + 顶层节点职责（唯一出口）",
        "parameters": {
            "type": "object",
            "properties": {
                "domains": {
                    "type": "array",
                    "description": "结构树：parent 表达层级（子节点 parent=父节点 name，顶层留空）",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "parent": {"type": "string"},
                            "path": {"type": "string", "description": "目录前缀或文件路径"},
                            "paths": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["name"],
                    },
                },
                "responsibilities": {
                    "type": "object",
                    "description": "`{节点名: 职责}`，只写给会成为 agent 的顶层节点",
                },
            },
            "required": ["domains"],
        },
    },
}


def _live_files(task) -> list[str]:
    """活性文件（FileLedger state=live）；含"当前轮"（用户口径：活性即当前有效的文件）。"""
    ledger = lifecycle_module.FileLedger()
    for r in task.rounds or []:
        ledger.update(r, blocks=blocks_module.segment_round_by_file(r))
    return sorted(ledger.live_files())


def _head_line(path: str, workspace: Path) -> str:
    """文件首行说明（代码取，模型不用抄）——注释块就跳着找第一行有内容的。"""
    try:
        target = workspace / path
        if not target.is_file():
            return ""
        for line in target.read_text(encoding="utf-8", errors="replace").splitlines()[:12]:
            text = line.strip().lstrip("#/\"'").strip()
            if len(text) >= 6:
                return text[:60]
    except OSError:
        pass
    return ""


def main() -> int:
    ap = argparse.ArgumentParser(description="只按活性文件画结构树（V4 分裂演练）")
    ap.add_argument("session_id")
    ap.add_argument("--dry", action="store_true", help="只打输入，不调 LLM")
    args = ap.parse_args()

    src = task_module.TASKS_ROOT / args.session_id
    if not (src / "task.json").is_file():
        raise SystemExit(f"会话不存在：{src}")
    REHEARSAL_ROOT.mkdir(parents=True, exist_ok=True)
    dst = REHEARSAL_ROOT / args.session_id
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)
    task_module.TASKS_ROOT = REHEARSAL_ROOT
    task = task_module.Task.load(args.session_id)

    files = _live_files(task)
    # 工作区取**会话自己的**（task.json 的 workspace），不是进程当前绑定的那个——
    # 否则在 Wovra 仓库里跑这个练兵时，读的是 Wovra 的目录，首行说明全取不到。
    workspace = Path(str(getattr(task, "workspace", "") or safety_module.workspace_root()))
    heads = {p: _head_line(p, workspace) for p in files}
    lines = [f"{i}. {p}" + (f"  —— {heads[p]}" if heads[p] else "")
             for i, p in enumerate(files, 1)]

    print(f"副本：{dst}")
    print(f"工作区：{workspace}")
    print(f"活性文件 {len(files)} 个：")
    for line in lines:
        print("  " + line)
    if not files:
        print("（没有活性文件——分裂无从谈起）")
        return 0

    prompt = _INSTRUCTION + "\n\n[活性文件]\n" + "\n".join(lines)
    print(f"\n=== 输入（{len(prompt):,} 字符）===")
    print(prompt)
    if args.dry:
        return 0

    llm = LLM()
    resp = llm.chat([{"role": "user", "content": prompt}], tools=[_SCHEMA])
    usage = resp.usage
    cached = int(getattr(getattr(usage, "prompt_tokens_details", None),
                         "cached_tokens", 0) or 0)
    print(f"\nusage: prompt={usage.prompt_tokens:,} cached={cached:,} "
          f"completion={usage.completion_tokens:,}")
    calls = resp.choices[0].message.tool_calls or []
    if not calls:
        print("（模型没走出口）正文：", str(resp.choices[0].message.content)[:600])
        return 1
    try:
        product = json.loads(calls[0].function.arguments)
    except json.JSONDecodeError as error:
        print(f"产物 JSON 解析失败：{error}\n{calls[0].function.arguments[:800]}")
        return 1

    domains = product.get("domains") or []
    resp_map = product.get("responsibilities") or {}
    print(f"\n=== 输出：结构树（{len(domains)} 个节点）===")
    for d in domains:
        scope = d.get("path") or "、".join(d.get("paths") or []) or "（无路径）"
        print(f"  {d.get('name')}　parent={d.get('parent') or '（顶层）'}　path={scope}")
    print("\n=== 输出：顶层职责 ===")
    for name, duty in resp_map.items():
        print(f"  {name}：{duty}")

    # 代码侧的机械绑定：最深的 path 前缀 + 覆盖率
    rules: list[tuple[str, str]] = []
    for d in domains:
        for p in ([d["path"]] if d.get("path") else []) + list(d.get("paths") or []):
            rules.append((str(p).rstrip("/"), str(d.get("name"))))
    owners: dict[str, list[str]] = {}
    for f in files:
        best = ""
        for prefix, name in rules:
            if f == prefix or f.startswith(prefix + "/"):
                if len(prefix) >= len(best):
                    best, owner = prefix, name
        owners.setdefault(owner if best else "（没人认领）", []).append(f)
    print("\n=== 代码机械绑定（最深的 path 前缀）===")
    for name, group in sorted(owners.items()):
        print(f"  {name}（{len(group)}）：{'、'.join(group[:6])}")
    # 耦合检查（零 LLM）：同一轮里一起被碰过的文件，是"同一域"的证据——
    # 单文件顶层域若与别的域共享轮次，就是过度分裂（本场 output/gaia/FINDINGS.md 即此例）。
    touched: dict[int, set] = {}
    for r in task.rounds or []:
        paths = set()
        for b in blocks_module.segment_round_by_file(r):
            if b.get("kind") == "file" and b.get("file"):
                paths.add(str(b["file"]))
        if paths:
            touched[int(r["seq"])] = paths
    print("\n=== 耦合检查（同一轮共现 → 疑似同一域）===")
    names = [n for n in owners if n != "（没人认领）"]
    bad = 0
    for i, left in enumerate(names):
        for right in names[i + 1:]:
            both = [seq for seq, paths in touched.items()
                    if set(owners[left]) & paths and set(owners[right]) & paths]
            if both:
                bad += 1
                print(f"  ⚠ 「{left}」与「{right}」在 {'、'.join('R%d' % x for x in both)}"
                      f" 里被一起碰过 → 疑似应合并成一个域")
    if not bad:
        print("  ✓ 各域的文件没有跨域共现")
    single = [n for n in names if len(owners[n]) == 1]
    if single:
        print(f"  单文件域（核对独立性）：{'、'.join(single)}")

    missing = owners.get("（没人认领）") or []
    print(f"\n覆盖率：{len(files) - len(missing)}/{len(files)}"
          + (f"　⚠ 未认领 {len(missing)} 个：{'、'.join(missing[:6])}" if missing else "　✓"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
