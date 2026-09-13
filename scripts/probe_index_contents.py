"""核对：索引（staged）里本节的改动是否齐全——提交前最后一道闸。

为什么要有它：分块提交（`scripts/git_stage_hunks.py`）只把自己那部分 hunk 写进
索引，一旦分类关键词写错（实测两次：注释里出现 "github"/"GitHub 检索" 被自己的
exclude 规则误伤），关键 hunk 会**静默漏掉**——`git diff --cached --stat` 看着
正常，但包里少一行 import，提交出去就是坏的。所以提交前逐项断言。

只读，不改任何东西。
"""
import subprocess
import sys

# (文件, 必须出现的片段, 说明)
CHECKS = [
    ("src/wovra/tools/__init__.py", "from .eyes import screenshot, view_image",
     "眼睛工具的公开导出"),
    ("src/wovra/agent/support.py", '_READ_ONLY_TOOLS | {"view_image"}',
     "view_image 进只读并发集"),
    ("src/wovra/cli/prompt.py", "screenshot,", "screenshot 进工具注册清单"),
    ("src/wovra/cli/prompt.py", "view_image,", "view_image 进工具注册清单"),
    ("src/wovra/agent/assembly.py", "def _eye_image_message",
     "装配期图片注入"),
    ("src/wovra/agent/assembly.py", "eye is not None", "注入挂载点"),
    ("src/wovra/tokens.py", "content_to_text", "图片投影（非 base64）"),
    ("src/wovra/tokens.py", "_content_tokens", "图片 token 固定口径"),
    ("src/wovra/serve.py", "_content_text", "前端 JSON 不带 base64"),
    ("src/wovra/tools/files.py", "def _change_diff", "改动 diff 生成器"),
    ("src/wovra/tools/files.py", "改动：", "回执带 diff"),
    ("tests/test_tools/_helpers.py", '"screenshot": ["target", "width"',
     "工具面基线已显式确认"),
    ("tests/test_tools/test_files.py", "改动：", "diff 回显用例"),
    ("docs/worklog-20260911.md", "## 93. 眼睛", "worklog 本节"),
]


def show(path: str) -> str:
    res = subprocess.run(["git", "show", ":" + path], capture_output=True)
    return res.stdout.decode("utf-8", errors="replace")


def main() -> int:
    bad = []
    for path, needle, why in CHECKS:
        text = show(path)
        if not text:
            bad.append(f"{path}: 不在索引里（{why}）")
            continue
        if needle not in text:
            bad.append(f"{path}: 缺 {needle!r}（{why}）")
    # 反向：别人的改动不该被带进来——判据是"这些文件在索引里**没有变更**"，
    # 而不是"内容里不含某关键词"（未暂存时索引版本 == HEAD 版本，内容里当然有
    # 那些符号；2026-09-13 实测：反向断言初版就是这么写错的）。
    others = [
        "src/wovra/agent/core.py",
        "src/wovra/tools/web.py",
        "tests/test_tools/test_web.py",
        "scripts/webui_render_check.py",
    ]
    for path in others:
        staged = subprocess.run(["git", "diff", "--cached", "--name-only", "--", path],
                                capture_output=True, text=True).stdout.strip()
        if staged:
            bad.append(f"{path}: 混入了并行会话的改动（该文件本不该被暂存）")
    if bad:
        print("索引核对失败：")
        for line in bad:
            print("  ✗", line)
        return 1
    print(f"索引核对通过（{len(CHECKS)} 项齐全，未混入并行会话改动）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
