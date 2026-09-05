"""Wovra 内置工具箱：文件读写、命令执行、审计与防护。

设计原则——"审计换权限"：

    * 能力上，agent 从"只读研究员"升级为能产出、能执行的执行者；
    * 代价上，每个变更类工具的完整内容都会被 Agent 记入任务历史
      （谁、何时、对哪个文件、做了什么、内容是什么），可追溯；
    * 破坏性操作不做"确认弹窗"（CLI 场景做不到良好交互），
      而是直接硬拒绝——宁可让模型换一种做法，也不赌运气。

所有路径类工具都通过 _safe_path 限制在项目根目录内，
防止模型读写项目之外的任何东西。
"""

import itertools
import json
import locale
import os
import re
import subprocess
import tempfile
from pathlib import Path

# 项目根目录（本文件位于 src/wovra/，向上三级）
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# 搜索时跳过的噪声目录（依赖、缓存、运行时数据——搜索它们只有噪音）
_IGNORED_DIRS = {
    ".git", ".venv", "__pycache__", ".pytest_cache",
    "tasks", "output", "node_modules",
}

# 变更类工具的失败标记：ui（红色显示）和 task 报告（"失败："前缀）
# 共用这一份，判定口径保持一致
FAILURE_MARKERS = (
    "工具执行出错",
    "未知工具",
    "合法 JSON",
    "已拒绝执行危险命令",
    "命令执行失败（",
)

# 破坏性命令黑名单：子串匹配，宁可误杀不可放过。
# 有意保持保守——rm -r、git push 这类即使"看起来安全"也拒绝，
# 模型收到拒绝文本后会自行寻找替代方案（这是流式循环的好处）。
_DENIED_PATTERNS = (
    "rm -r",          # 递归删除（含 -rf/-fr）
    " -delete",       # find 的删除变体
    "sudo ",
    "mkfs",
    "dd if=",
    ":(){",           # fork 炸弹
    "git push",       # 对外发布，不由 agent 自主决定
    "git reset --hard",
    "git clean",
    "git checkout -- ",
    "git restore",
    "shutdown",
    "reboot",
    "chmod -R",
    "mv -f",
    "| sh",
    "| bash",
    "|zsh",
    "| sh;",
    "curl ",          # 下载外部内容（配合管道执行是常见攻击面），一律拒绝
    "wget ",
)

_COMMAND_TIMEOUT = 60  # 秒


# ---- 审计挂钩 ---------------------------------------------------------------
# Agent 绑定任务时通过 set_audit_recorder 注册回调；工具用它把
# "参数之外的事实"（如被覆盖文件的旧内容）写进任务历史。走旁路
# 而不是工具参数，是为了不污染模型看到的工具 schema。

_audit_recorder = None


def set_audit_recorder(fn) -> None:
    global _audit_recorder
    _audit_recorder = fn


def _audit(text: str) -> None:
    if _audit_recorder is not None:
        _audit_recorder(text)


def _safe_path(relative: str) -> Path:
    """把相对路径解析到项目根目录内，越界直接报错。"""
    path = (PROJECT_ROOT / relative).resolve()
    if not path.is_relative_to(PROJECT_ROOT):
        raise ValueError(f"路径越界，只允许访问项目目录内的文件: {relative}")
    return path


# ---- 只读工具 -------------------------------------------------------------


def list_files(directory: str = ".") -> list[str]:
    """列出项目内某个目录下的文件和子目录（不含递归）。"""
    path = _safe_path(directory)
    return sorted(p.name + ("/" if p.is_dir() else "") for p in path.iterdir())


def read_file(path: str, start_line: int = 1, num_lines: int = 200) -> str:
    """按行读取项目内一个文本文件的内容片段。需要通读整个文件时，
    按 num_lines=400 连续分段读取，不要零碎小段反复读。

    大文件请配合 search_files 先定位，再用 start_line/num_lines
    分段读取——单次最多 400 行，返回值会标明文件总行数和
    继续读取的位置。
    """
    target = _safe_path(path)
    try:
        text = target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return f"{path} 不是 UTF-8 文本文件（可能是二进制文件），无法按文本读取"
    _observe_file(target)  # 记录观察时的状态，供 edit/write 的过期保护比对
    lines = text.splitlines()
    total = len(lines)
    if total == 0:
        return f"{path} 是空文件"
    start = max(1, start_line)
    if start > total:
        return f"{path} 共 {total} 行，start_line={start} 超出范围"
    end = min(total, start + min(max(1, num_lines), 400) - 1)
    body = "\n".join(lines[start - 1:end])
    header = f"{path}（共 {total} 行，以下为第 {start}-{end} 行）"
    if end < total:
        body += f"\n...（后续还有 {total - end} 行，用 start_line={end + 1} 继续读取）"
    return f"{header}\n{body}"


def search_files(pattern: str, directory: str = ".", glob: str = "*") -> str:
    """在项目内用正则表达式搜索文本文件（类似 grep）。

    返回 `路径:行号: 行内容` 格式的匹配，最多 50 条；
    自动跳过 .git/.venv 等噪声目录。找"某个函数在哪定义"、
    "哪个文件用了某配置" 都靠它。
    """
    try:
        regex = re.compile(pattern)
    except re.error as error:
        raise ValueError(f"正则表达式无效: {error}") from error

    root = _safe_path(directory)
    matches: list[str] = []
    for path in sorted(root.rglob(glob)):
        if not path.is_file():
            continue
        if any(part in _IGNORED_DIRS for part in path.parts):
            continue
        if path.stat().st_size > 1_000_000:  # 跳过超大文件
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, PermissionError):
            continue
        for line_number, line in enumerate(text.splitlines(), start=1):
            if regex.search(line):
                # 统一用 / 分隔，输出跨平台一致（也便于回填给 read_file 等工具）
                relative = path.relative_to(PROJECT_ROOT).as_posix()
                matches.append(f"{relative}:{line_number}: {line.strip()[:200]}")
                if len(matches) >= 50:
                    return "\n".join(matches) + "\n...(已达 50 条上限，请缩小搜索范围)"
    if not matches:
        return f"无匹配：pattern={pattern!r}, directory={directory!r}, glob={glob!r}"
    return "\n".join(matches)


def get_current_time() -> str:
    """获取当前本地时间（ISO 格式）。"""
    from datetime import datetime

    return datetime.now().isoformat(timespec="seconds")


# ---- 文件观察注册表（过期保护） ---------------------------------------------
# 本进程读/写过的文件 → (mtime_ns, size)。edit_file/write_file 前核对：
# 文件在观察后被外部（用户、其他会话、其他进程）改动 → 拒绝执行并要求
# 重新 read_file，防止模型按过期的上下文内容覆盖用户的新修改。
# 本进程从未观察过的文件无从判断，保持原行为（write_file 有完整留底）。

_file_registry: dict[Path, tuple[int, int]] = {}


def _observe_file(path: Path) -> None:
    try:
        st = path.stat()
        _file_registry[path] = (st.st_mtime_ns, st.st_size)
    except OSError:
        _file_registry.pop(path, None)


def _stale_error(path: Path) -> str | None:
    """文件在观察后被外部修改 → 返回拒绝原因；否则 None。"""
    observed = _file_registry.get(path)
    if observed is None:
        return None
    try:
        st = path.stat()
    except OSError:
        return None
    if (st.st_mtime_ns, st.st_size) != observed:
        return (
            f"文件在你上次读取后已被外部修改（用户或其他进程）：{path}。"
            f"上下文里的内容可能已过期。请先 read_file 重新确认最新内容，"
            f"再决定如何修改。"
        )
    return None


# ---- 变更类工具（AUDITED_TOOLS，Agent 会做完整审计记录） --------------------


def write_file(path: str, content: str) -> str:
    """创建或整体覆盖项目内的一个文本文件。

    覆盖是全量的——只改一部分请用 edit_file，它要求唯一定位，
    误伤面小得多。覆盖时旧内容会通过审计挂钩完整留底，
    出问题可以对照还原。若文件在你上次读取后被外部修改过，
    会拒绝执行并要求重新确认。
    """
    target = _safe_path(path)
    stale = _stale_error(target)
    if stale:
        return stale
    existed = target.exists()
    old = None
    if existed:
        try:
            old = target.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            old = "(非 UTF-8 内容，未留底)"
    # 允许写到尚不存在的子目录（模型经常给出 "reports/xx.md" 这类路径）
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    _observe_file(target)
    action = "覆盖" if existed else "创建"
    if old is not None:
        # 旧内容完整留底（审计原则：能还原）；超大文件截断到 20000 字符
        backup = old if len(old) <= 20_000 else old[:20_000] + "\n...(已截断)"
        _audit(f"[write_file 旧内容备份] {path}:\n{backup}")
    return f"已{action} {path}（{len(content)} 字符）"


def edit_file(path: str, old_text: str, new_text: str) -> str:
    """把文件中「恰好出现一次」的 old_text 替换为 new_text。

    强制唯一定位：找不到或出现多次都直接报错，让模型补充更多
    上下文再试。这是防止"替换了不想替换的地方"的关键约束。
    若文件在你上次读取后被外部修改过，会拒绝执行并要求重新确认。
    """
    target = _safe_path(path)
    stale = _stale_error(target)
    if stale:
        return stale
    text = target.read_text(encoding="utf-8")
    count = text.count(old_text)
    if count == 0:
        raise ValueError(f"{path} 中未找到待替换文本（前 80 字符: {old_text[:80]!r}）")
    if count > 1:
        raise ValueError(
            f"{path} 中待替换文本出现 {count} 次，请补充前后文使其唯一定位"
        )
    target.write_text(text.replace(old_text, new_text, 1), encoding="utf-8")
    _observe_file(target)
    # 替换片段对完整留底：改了哪段、改成了什么，一目了然
    _audit(f"[edit_file] {path}\n定位片段:\n{old_text}\n替换为:\n{new_text}")
    return f"已修改 {path}（{len(old_text)} 字符 → {len(new_text)} 字符）"


def _kill_process_tree(pid: int) -> None:
    """强杀 pid 及其全部后代进程。

    Windows 的 Popen.kill() 只杀 shell 本身（shell=True 时是
    cmd.exe），孙进程会变成孤儿活下来——既泄漏进程/端口，还攥着
    输出管道让清理阶段的 communicate 永久阻塞。整树强杀
    （taskkill /T）从根上杜绝这两件事。POSIX 侧配合
    start_new_session：子进程自成一个进程组，killpg 一网打尽。
    """
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True,
        )
        return
    import signal

    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except OSError:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def run_command(command: str, timeout: int | None = None) -> str:
    """在项目根目录运行一条 shell 命令，返回退出码与输出。默认 60 秒
    超时整树强杀（timeout 可调，1-600 秒）；不要运行前台常驻服务
    （http.server 之类永不退出的命令只会白等到超时）——预览网页用
    系统方式打开文件（Windows: start 文件名），长驻服务用
    run_background 后台启动。

    防护：
        * 黑名单匹配到破坏性模式时直接拒绝，不执行；
          拒绝文本会回传给模型（它能看到原因并换方案）
        * 超时强制终止整棵进程树，防止长命令卡死整个任务
        * 输出各截断 1500 字符，防止超长输出撑爆上下文
    """
    _audit(f"[run_command] {command}")  # 无论执行与否，命令原文都进审计
    for pattern in _DENIED_PATTERNS:
        if pattern in command:
            return (
                f"已拒绝执行危险命令：包含被禁止的模式 `{pattern}`。"
                f"如需完成类似效果，请使用更安全的替代方案。"
            )
    wait = _COMMAND_TIMEOUT if timeout is None else max(1, min(int(timeout), 600))

    # 输出重定向到临时文件而不是 PIPE：文件没有"写端被孙进程攥住"
    # 的问题（http.server 这类孤儿进程曾把管道清理阶段永久挂死），
    # 也没有 PIPE 缓冲写满导致的子进程阻塞，超时后的清理必然可返回
    # 输出重定向到临时文件而不是 PIPE：文件没有"写端被孙进程攥住"
    # 的问题（http.server 这类孤儿进程曾把管道清理阶段永久挂死），
    # 也没有 PIPE 缓冲写满导致的子进程阻塞，超时后的清理必然可返回
    with tempfile.TemporaryFile() as out_f, tempfile.TemporaryFile() as err_f:
        proc = subprocess.Popen(
            command,
            shell=True,
            stdout=out_f,
            stderr=err_f,
            cwd=PROJECT_ROOT,  # 固定工作目录：相对路径都在项目内
            # POSIX：让子进程自成进程组，超时后 killpg 整组杀掉而不伤自身
            start_new_session=os.name != "nt",
        )
        try:
            proc.wait(timeout=wait)
        except subprocess.TimeoutExpired:
            _kill_process_tree(proc.pid)
            try:
                proc.wait(timeout=5)  # 整树已灭，这只是兜底限时
            except subprocess.TimeoutExpired:
                pass
            return f"命令执行失败（超时 {wait} 秒被强制终止）：{command[:200]}"

        out_f.seek(0)
        err_f.seek(0)
        encoding = locale.getpreferredencoding(False)  # 与原 text=True 口径一致
        stdout = out_f.read().decode(encoding, errors="replace").strip() or "(无输出)"
        stderr = err_f.read().decode(encoding, errors="replace").strip() or "(无输出)"

    header = (
        f"命令执行失败（exit_code={proc.returncode}）"
        if proc.returncode != 0
        else "exit_code=0"
    )
    return (
        f"{header}\n"
        f"stdout:\n{stdout[:1500]}\n"
        f"stderr:\n{stderr[:1500]}"
    )


# ---- 后台任务 ----------------------------------------------------------------
# 长驻进程（开发服务器、watcher、长安装）的后台运行与控制：启动即返回，
# 输出落日志文件，增量查看，整树强杀。控制句柄仅存活于本进程——
# Wovra 退出后进程仍在运行，日志保留在 .wovra-background/ 下。

_BACKGROUND_TASKS: dict[str, dict] = {}
_BACKGROUND_SEQ = itertools.count(1)
_BACKGROUND_LOG_DIR = PROJECT_ROOT / ".wovra-background"


def run_background(command: str) -> str:
    """后台启动一条 shell 命令（服务器、监听、长安装等），立即返回任务 ID。

    输出写入日志文件；用 check_background 查看增量输出，
    stop_background 停止（整树强杀）。黑名单与 run_command 相同。
    注意：Wovra 退出后进程仍在运行，但控制句柄失效——日志保留在
    .wovra-background/ 下，可手动查看。
    """
    _audit(f"[run_background] {command}")
    for pattern in _DENIED_PATTERNS:
        if pattern in command:
            return (
                f"已拒绝执行危险命令：包含被禁止的模式 `{pattern}`。"
                f"如需完成类似效果，请使用更安全的替代方案。"
            )
    _BACKGROUND_LOG_DIR.mkdir(parents=True, exist_ok=True)
    task_id = f"bg-{next(_BACKGROUND_SEQ)}"
    log_path = _BACKGROUND_LOG_DIR / f"{task_id}.log"
    with open(log_path, "wb") as log_file:
        proc = subprocess.Popen(
            command,
            shell=True,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            cwd=PROJECT_ROOT,  # 固定工作目录：相对路径都在项目内
            start_new_session=os.name != "nt",  # 与 run_command 同一套树杀约定
        )
    _BACKGROUND_TASKS[task_id] = {
        "proc": proc, "log": log_path, "pos": 0, "command": command,
    }
    return (
        f"后台任务 {task_id} 已启动（PID {proc.pid}）：{command[:120]}\n"
        f'查看输出: check_background(task_id="{task_id}") | '
        f'停止: stop_background(task_id="{task_id}")'
    )


def check_background(task_id: str) -> str:
    """查看后台任务的运行状态与增量输出（自上次查看以来的新增部分）。"""
    entry = _BACKGROUND_TASKS.get(task_id)
    if entry is None:
        return f"未找到后台任务: {task_id}（控制句柄仅在本会话内有效）。"
    proc = entry["proc"]
    running = proc.poll() is None
    new_text = ""
    if entry["log"].exists():
        with open(entry["log"], "rb") as f:
            f.seek(entry["pos"])
            data = f.read()
        entry["pos"] += len(data)
        new_text = data.decode(locale.getpreferredencoding(False), errors="replace")
    status = "运行中" if running else f"已退出（exit_code={proc.returncode}）"
    body = new_text.strip() or "（无新输出）"
    return f"[{task_id}] {status}\n{body[-2000:]}"


def stop_background(task_id: str) -> str:
    """停止后台任务（整树强杀），返回退出状态。"""
    entry = _BACKGROUND_TASKS.get(task_id)
    if entry is None:
        return f"未找到后台任务: {task_id}（控制句柄仅在本会话内有效）。"
    proc = entry["proc"]
    if proc.poll() is None:
        _kill_process_tree(proc.pid)
    try:
        code = proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        return f"后台任务 {task_id} 已发送终止信号但未退出，请稍后用 check_background 确认。"
    return f"后台任务 {task_id} 已停止（exit_code={code}）。"
