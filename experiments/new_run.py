"""创建一次受控实验运行：冻结起始文件 + 验收标准的全新会话。

用法：uv run python experiments/new_run.py --mode managed --index 1

产出：
* tasks/<task-id>/        —— 新会话（验收标准已写入 acceptance_criteria）
* experiments/runs/<task-id>/chat.html   —— 冻结起始文件的独立副本
* experiments/runs/<task-id>/meta.json   —— 运行元数据（模式/序号/文件哈希）

然后按输出提示启动 chat，把任务书原样粘贴为第一条输入。
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

TASK_BOOK = (
    "请在 chat.html 上实现以下 4 个功能，逐一完成，不要扩展其他功能：\n"
    "1. 语音朗读：AI 回复的操作区加 🔊 按钮，点击用浏览器 speechSynthesis "
    "朗读该条回复，再点一次停止；\n"
    "2. 导出对话：顶栏加 ⬇️ 按钮，把当前会话导出为 Markdown 文件下载；\n"
    "3. 重新生成：AI 回复的操作区加 🔄 按钮，点击后用原用户消息重新生成"
    "回复并替换原回复；\n"
    "4. 会话内搜索：加一个搜索框，输入关键词时当前会话内匹配的消息高亮。\n"
    "完成后用系统方式打开页面供检查。"
)

CRITERIA = [
    "实现语音朗读：AI 回复操作区有朗读按钮，使用 speechSynthesis，含停止逻辑",
    "实现导出对话：顶栏有导出按钮，用 Blob 下载当前会话的 Markdown 文件",
    "实现重新生成：AI 回复操作区有重新生成按钮，用原用户消息重新生成并替换",
    "实现会话内搜索：有搜索框，输入关键词时匹配消息高亮",
    "不引入外部 API 与外部依赖（无网络请求、无外部脚本/样式）",
    "页面结构完整可渲染（标签闭合、脚本无语法错误）",
]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["managed", "baseline"], required=True)
    parser.add_argument("--index", type=int, required=True, help="运行序号（1 起）")
    args = parser.parse_args()

    fixture_sha = hashlib.sha256(FIXTURE.read_bytes()).hexdigest()[:12]

    task = Task.create(
        goal="受控实验：在 chat.html 上实现 4 个指定功能",
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
    print("--- 任务书（原样粘贴为第一条输入）---")
    print(TASK_BOOK)


if __name__ == "__main__":
    main()
