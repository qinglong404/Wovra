"""静态验收检查：按功能清单逐项判定，支持累计快照。

结构级检查客观、可复现，但**可博弈**（写出标记不等于实现行为）——
协议要求每轮闭合后做当轮功能的 1 分钟人工冒烟抽查
（见 experiments/README.md）。

用法：
  uv run python experiments/check_acceptance.py <chat.html>            # 检查全部 10 项
  uv run python experiments/check_acceptance.py <chat.html> --round 3  # 检查功能 1..3
  ... --round 3 --save                                                 # 并快照到所在运行目录
输出：逐项 PASS/FAIL + JSON 汇总行（供 collect.py 读取）。
"""

import argparse
import json
from pathlib import Path

from typing import Callable

# (编号即下标+1, 功能名, 静态标记判定) —— 与 README 功能清单一一对应
FEATURES: list[tuple[str, str, Callable[[str], bool]]] = [
    ("语音朗读", "AI 回复操作区 🔊，speechSynthesis 朗读 + 停止",
     lambda h: "speechSynthesis" in h and ".speak(" in h
     and ".cancel(" in h and "🔊" in h),
    ("导出对话", "顶栏 ⬇️，Blob 下载当前会话的 Markdown",
     lambda h: "Blob" in h and "createObjectURL" in h
     and "download" in h.lower() and ".md" in h.lower()),
    ("重新生成", "AI 回复操作区 🔄，用原用户消息重新生成并替换",
     lambda h: any(m in h for m in ("重新生成", "regenerate", "🔄"))),
    ("会话内搜索", "搜索框，输入关键词时匹配消息高亮",
     lambda h: "搜索" in h and any(m in h for m in ("高亮", "highlight", "<mark"))),
    ("消息置顶", "📌 置顶的消息固定显示在会话顶部",
     lambda h: "📌" in h or "置顶" in h),
    ("字数统计", "输入框下方实时显示当前输入的字数",
     lambda h: "字数" in h),
    ("快捷命令 /roll", "输入 /roll 返回一条 1-100 随机点数的消息",
     lambda h: "/roll" in h),
    ("强调色自定义", "🎨 颜色选择器修改强调色，保存到 localStorage",
     lambda h: 'type="color"' in h.lower()),
    ("草稿自动保存", "输入框未发送内容实时存 localStorage，刷新后恢复",
     lambda h: "草稿" in h or "draft" in h.lower()),
    ("时间戳开关", "设置里的开关控制每条消息是否显示时间戳",
     lambda h: "时间戳" in h and ("checkbox" in h.lower()
                                  or "toggle" in h.lower() or "开关" in h)),
]


def check(html: str, upto: int | None = None) -> dict[str, bool]:
    """检查功能 1..upto（None = 全部）的静态标记，返回 {条目: 是否通过}。"""
    results: dict[str, bool] = {}
    for i, (name, _spec, fn) in enumerate(FEATURES, start=1):
        if upto is not None and i > upto:
            continue
        results[f"F{i:02d} {name}"] = bool(fn(html))
    return results


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("html_path", help="产出的 chat.html 路径")
    parser.add_argument("--round", type=int, default=None,
                        help="只检查功能 1..k（第 k 轮闭合后的累计口径）")
    parser.add_argument("--save", action="store_true",
                        help="把结果快照写入所在运行目录 acceptance-R{k}.json")
    args = parser.parse_args()

    html_path = Path(args.html_path)
    html = html_path.read_text(encoding="utf-8", errors="replace")
    results = check(html, upto=args.round)
    passed = sum(results.values())

    for name, ok in results.items():
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    print(f"累计通过 {passed}/{len(results)}")

    if args.save:
        label = args.round if args.round is not None else "final"
        out = html_path.parent / f"acceptance-R{label}.json"
        out.write_text(json.dumps(
            {"round": args.round, "passed": passed,
             "total": len(results), "results": results},
            ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"快照已写入: {out}")
    print(json.dumps({"passed": passed, "total": len(results), "results": results},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
