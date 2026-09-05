"""创建一次受控实验会话：冻结起始文件 + 10 项功能验收标准的全新多轮会话。

用法：uv run python experiments/new_run.py --mode managed --index 1

产出：
* tasks/<task-id>/        —— 新会话（10 项功能写入 acceptance_criteria）
* experiments/runs/<task-id>/chat.html   —— 冻结起始文件的独立副本
* experiments/runs/<task-id>/meta.json   —— 运行元数据（模式/序号/文件哈希）

然后按输出提示启动 chat，按 README 的功能清单逐轮输入（每轮一个功能）。
"""

import argparse
import hashlib
import json
import shutil
from datetime import datetime
from pathlib import Path

from wovra.task import Task

HERE = Path(__file__).resolve().parent
FIXTURE = HERE / "fixtures" / "chat-page.html"

# 功能清单（与 experiments/README.md 的逐轮任务书一一对应）
CRITERIA = [
    "实现语音朗读：AI 回复操作区有 🔊 朗读按钮，用 speechSynthesis 朗读该条回复，再点一次停止",
    "实现导出对话：顶栏有 ⬇️ 按钮，把当前会话导出为 Markdown 文件下载",
    "实现重新生成：AI 回复操作区有 🔄 按钮，用原用户消息重新生成回复并替换",
    "实现会话内搜索：有搜索框，输入关键词时当前会话内匹配的消息高亮",
    "实现消息置顶：消息操作区有 📌，置顶的消息固定显示在会话顶部",
    "实现字数统计：输入框下方实时显示当前输入的字数",
    "实现快捷命令 /roll：输入 /roll 时返回一条 1-100 随机点数的消息",
    "实现强调色自定义：顶栏 🎨 打开颜色选择器修改页面强调色，保存到 localStorage",
    "实现草稿自动保存：输入框未发送内容实时存 localStorage，刷新页面后恢复",
    "实现时间戳开关：设置里有开关，控制每条消息是否显示时间戳",
]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["managed", "baseline"], required=True)
    parser.add_argument("--index", type=int, required=True, help="会话序号（1 起）")
    args = parser.parse_args()

    fixture_sha = hashlib.sha256(FIXTURE.read_bytes()).hexdigest()[:12]

    task = Task.create(
        goal="受控实验：在 chat.html 上按清单逐轮实现 10 个功能",
        acceptance_criteria=list(CRITERIA),
    )
    task.save()

    run_dir = HERE / "runs" / task.id
    run_dir.mkdir(parents=True, exist_ok=True)
    work_file = run_dir / "chat.html"
    shutil.copyfile(FIXTURE, work_file)

    meta = {
        "mode": args.mode,
        "index": args.index,
        "fixture_sha256_12": fixture_sha,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "work_path": work_file.resolve().as_posix(),
    }
    (run_dir / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"运行目录: {work_file}")
    print(f"启动会话: uv run wovra chat {task.id}")
    print("按 experiments/README.md 的功能清单逐轮输入（每轮一个功能）。")
    print("--- 第 1 轮输入（原样粘贴，后续轮次见 README）---")
    print(f"请在 {work_file.resolve().as_posix()} 上实现功能 1：语音朗读——AI 回复"
          "操作区加 🔊 按钮，用 speechSynthesis 朗读该条回复，再点一次停止。"
          "只实现这一个功能，不要动其他功能，不要提前实现后面的功能。")


if __name__ == "__main__":
    main()
