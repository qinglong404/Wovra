"""用户交互与扩展点：ask_user、用户 Hooks（pre/post_tool）、当前时间。

钩子是用户自己的文件（.wovra/hooks/）——审计、禁写区、公司策略都变成
配置而非代码。钩子出错/超时静默跳过：扩展机制绝不能带垮工具本身。
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

from . import safety

# 选项自带的 "A." / "B、" 编号前缀（模型实测会传，渲染会叠成 "A. A. xxx"）
_CHOICE_LABEL_RE = re.compile(r"^[A-Ha-h][.、:：)]\s*")


def _split_choices(choices) -> list[str]:
    """把模型给的 choices 拆成候选项（零 LLM）。

    兼容四种形态：| 分隔（约定格式）、换行分隔（deepseek 实测会传
    换行而非 |）、全角分号分隔（实测模型把选项写成一整句
    "选甲；选乙；选丙"——不拆就渲染成一整条）、list/tuple（绕过
    schema 类型时）。选项自带编号时去掉前缀，避免渲染成 "A. A. xxx"。
    """
    if isinstance(choices, (list, tuple)):
        raw = "\n".join(str(c) for c in choices)
    else:
        raw = str(choices or "")
    out: list[str] = []
    for seg in raw.replace("\r", "\n").split("\n"):
        for part in re.split(r"[|｜；;]", seg):
            part = part.strip()
            if not part:
                continue
            out.append(_CHOICE_LABEL_RE.sub("", part))
    return out


def _read_answer(prompt_text: str) -> str:
    """读一行用户回答：交互终端用 prompt_toolkit——它按显示宽度处理
    光标，中文/emoji 的退格编辑不错位；裸 input() 走内核行编辑，UTF-8
    中文按字节删、在部分终端上删除会错位（ask_user 输入框实测）。

    非 TTY / 无 prompt_toolkit / ptk 在伪造 tty 上失败（如测试）都退
    回 input()。
    """
    if not sys.stdin.isatty():
        return input(prompt_text)
    try:
        from prompt_toolkit import prompt as _pt_prompt
    except Exception:  # noqa: BLE001——缺依赖
        return input(prompt_text)
    try:
        return _pt_prompt(prompt_text)
    except KeyboardInterrupt:
        raise
    except Exception:  # noqa: BLE001——ptk 不可用（如测试伪造 tty）
        return input(prompt_text)


def ask_user(question: str, choices: str = "", multi: bool = False) -> str:
    """就需求或编码细节向用户提问，等待用户在终端输入答案。

    choices 用 | 分隔候选项（如 "是|否|继续"），不要用换行或列表。
    """
    options = _split_choices(choices)
    letters = "ABCDEFGH"
    prompt = f"\n[模型提问] {question}"
    if options:
        for i, opt in enumerate(options[:8]):
            prompt += f"\n  {letters[i]}. {opt}"
        prompt += "\n  （敲字母选择；也可直接输入自由回答"
        prompt += "，多选用逗号分隔如 A,C）" if multi else "）"
    prompt += "\n你的回答> "
    if not sys.stdin.isatty():
        return "（非交互环境，无法获取用户输入。请基于已有信息继续，或在最终回答中说明假设。）"
    safety._user_input_pending = True
    try:
        answer = _read_answer(prompt)
    except EOFError:
        answer = ""
    finally:
        safety._user_input_pending = False
    answer = (answer or "").strip()
    if options and answer:
        # 字母选择 → 展开为选项原文；无法解析为字母的输入按自由文本采纳
        tokens = [t.strip().rstrip(".").upper() for t in answer.split(",")] if multi             else [answer.rstrip(".").upper()]
        last = letters[len(options) - 1]
        if all(len(t) == 1 and "A" <= t <= last for t in tokens):
            picked = " | ".join(options[ord(t) - ord("A")] for t in tokens)
            return f"用户的回答: {picked}"
    return f"用户的回答: {answer or '（空）'}"

# ---- 用户 Hooks（.wovra/hooks/：工具调用的前后拦截点） ----------------------
# zcode-borrowings.md 1.4：扩展者对工具加规则的用户扩展点。约定：
#   .wovra/hooks/pre_tool.py   每次工具调用前执行（stdin 收 JSON：
#                              {"tool", "arguments"}）；exit 0 = 放行
#                              （stdout 忽略），exit 非 0 = 拦截，stdout
#                              作为拒绝理由回传给模型
#   .wovra/hooks/post_tool.py  工具执行成功后执行（stdin 同上 + "result"）；
#                              非空 stdout 作为 [hooks 反馈] 追加到结果
# 钩子是用户自己的文件——审计、禁写区、公司策略都变成配置而非代码。
# 钩子出错/超时静默跳过：扩展机制绝不能带垮工具本身。

_HOOK_TIMEOUT = 10


def _hook_script(name: str) -> Path | None:
    script = safety.PROJECT_ROOT / ".wovra" / "hooks" / name
    return script if script.is_file() else None


def _run_hook(script: Path, payload: dict) -> tuple[int, str] | None:
    """执行一个钩子脚本，返回 (exit_code, stdout)；超时/异常返回 None。"""
    try:
        proc = subprocess.run(
            [sys.executable, str(script)],
            input=json.dumps(payload, ensure_ascii=False),
            capture_output=True, text=True, timeout=_HOOK_TIMEOUT,
            cwd=safety.PROJECT_ROOT,
            env=dict(os.environ, PYTHONUTF8="1"),
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    return proc.returncode, proc.stdout or ""


def run_pre_hook(tool: str, arguments: dict) -> str | None:
    """工具调用前的用户钩子。返回拦截理由（阻塞执行），None = 放行。

    约定：exit 0 = 放行；exit 非 0 = 拦截，stdout 首选作为拒绝理由
    （空 stdout 用默认理由）。理由会回传给模型——它能看到原因并换方案。
    """
    script = _hook_script("pre_tool.py")
    if script is None:
        return None
    hook = _run_hook(script, {"tool": tool, "arguments": arguments})
    if hook is None:
        return None
    code, out = hook
    if code == 0:
        return None
    reason = out.strip() or "（钩子未说明原因）"
    safety._audit(f"[hooks 拦截] {tool}: {reason[:200]}")
    return f"被用户钩子拦截：{reason}"


def run_post_hook(tool: str, arguments: dict, result: str) -> str | None:
    """工具执行成功后的用户钩子。返回非空反馈（追加到结果），None = 无。"""
    script = _hook_script("post_tool.py")
    if script is None:
        return None
    hook = _run_hook(script, {"tool": tool, "arguments": arguments, "result": result})
    if hook is None:
        return None
    feedback = hook[1].strip()
    return feedback[:1000] or None

def get_current_time() -> str:
    """获取当前本地时间（ISO 格式）。"""
    from datetime import datetime

    return datetime.now().isoformat(timespec="seconds")
