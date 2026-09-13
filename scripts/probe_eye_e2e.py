"""E2E：眼睛的端到端验证（**真实 API 调用**，2026-09-13）。

验收标准（用户拍板「将眼睛和 diff 都做了吧」时的口径）：模型能通过
`screenshot` + `view_image` **真正看到像素**，不是 mock、不是靠读源码猜。

怎么排除"靠猜"：页面颜色**不在 HTML 文本里**——它以 base64 写在 JS 里，
运行时才解码上色。模型若答对主色，只能是从图上读出来的（对比
`scripts/probe_vision_channel.py` 的通道验证：那个测的是端点收不收图，
这个测的是整条运行时链路：工具 → 引用标记 → 装配期注入 → 模型看到）。

代价：一次真实对话（约 3 次调用）。产物落在临时 TASKS_ROOT，用完即删
（AGENTS.md 测试纪律：tasks/ 只允许存在用户真实会话）。

跑法：uv run --no-sync python scripts/probe_eye_e2e.py
"""
import base64
import shutil
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from wovra import task as task_module  # noqa: E402

RED = "#dc2828"                  # 目标主色（页面实际渲染色）
GREEN = "#28a03c"


def build_page() -> str:
    """颜色藏在 base64 里：读源码必须解码才知道，读图直接看得出。"""
    enc = base64.b64encode(RED.encode()).decode()
    return (
        "<html><body style='margin:0'>"
        "<script>"
        f"const c=atob('{enc}');"
        "const d=document.createElement('div');"
        "d.style.cssText='position:fixed;inset:0;background:'+c;"
        "d.textContent='WOVRA EYE';"
        "d.style.cssText+=';color:#fff;font:64px sans-serif;padding:48px';"
        "document.body.appendChild(d);"
        "</script></body></html>"
    )


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="wovra-eye-e2e-"))
    page = PROJECT_ROOT / "output" / "eye_e2e.html"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(build_page(), encoding="utf-8")

    task_module.TASKS_ROOT = tmp          # 会话落在临时目录，用完即删
    try:
        from wovra.cli.prompt import _build_agent

        task = task_module.Task.create(
            goal="眼睛端到端验证（screenshot + view_image 真看到像素）",
            requirements=["模型必须凭图回答页面主色"],
        )
        agent = _build_agent(task, mode="managed", async_organization=False)
        print(f"会话 {task.id}　模型 {agent.llm.model}")
        answer = agent.run(
            "用 screenshot 对 output/eye_e2e.html 截图（窗口 480x320），"
            "然后用 view_image 看那张图，告诉我整页的主色调是什么颜色。"
            "只答颜色，不要解释。"
        )
        print("模型回答：", (answer or "").strip()[:300])

        low = (answer or "").lower()
        hit = any(w in low for w in ("红", "red", RED))
        wrong = any(w in low for w in ("绿", "green", GREEN, "蓝", "blue"))
        print(f"判读：{'PASS' if hit and not wrong else 'FAIL'}"
              f"（期望红 {RED}；答对 = 真看到了像素）")
        # 顺带核对：工具结果里只有引用标记，没有 base64
        text = (tmp / task.id / "task.json").read_text(encoding="utf-8")
        print(f"task.json {len(text):,} 字符；"
              f"含【图片】引用 {'是' if '【图片】path=' in text else '否'}；"
              f"含 base64 图体 {'是（不该有）' if 'data:image/png;base64' in text else '否'}")
        return 0 if hit and not wrong else 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        try:
            page.unlink()
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())
