"""Shell 工具：run_command（黑名单 + 工作区约束 + 确认门 + 输出默认全量）。

另含 `_kill_process_tree`——整树强杀，供本模块与 background 共用。
"""

import os
import subprocess
import tempfile
import time

from . import limits, safety


_COMMAND_TIMEOUT = 60  # 秒

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

def _oem_encoding() -> str:
    """Windows 控制台所用 OEM 代码页对应的 Python 编码名（非 Windows 回 utf-8）。

    为什么不能只靠 utf-8（2026-09-11 实测，worklog-20260911.md §9.6-P3）：
    本项目已给子进程设 PYTHONUTF8=1，但那只约束 Python 自己；cmd.exe 的
    内建报错、以及大量原生程序（git for windows 之外的工具链）按**OEM
    代码页**输出。中文 Windows 上 cmd 说 `'x' 不是内部或外部命令` 时给出
    的是 GBK 字节 → 按 utf-8 解码成乱码回传模型（模型看不到真实原因，
    也让探针无法按文本判定"命令不存在"）。
    """
    if os.name != "nt":
        return "utf-8"
    try:
        import ctypes

        return f"cp{ctypes.windll.kernel32.GetOEMCP()}"
    except Exception:  # noqa: BLE001——拿不到代码页时退回 utf-8
        return "utf-8"


def _decode_output(raw: bytes) -> str:
    """解码子进程输出：先按 UTF-8 严格试，失败再用 OEM 代码页。

    严格 utf-8 失败即说明不是 UTF-8 字节（合法 UTF-8 与 GBK 混淆的概率
    极低，且这是"先到先得"的确定性规则）；两者都不行才退回替换解码。
    """
    if not raw:
        return ""
    for encoding in ("utf-8", _oem_encoding()):
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def run_command(command: str, timeout: int | None = None) -> str:
    """在项目根目录运行一条 shell 命令，返回退出码与输出。默认 60 秒
    超时整树强杀（timeout 可调，1-600 秒）；不要运行前台常驻服务
    （http.server 之类永不退出的命令只会白等到超时）——预览网页用
    系统方式打开文件（Windows: start 文件名），长驻服务用
    run_background 后台启动。

    防护：
        * 黑名单匹配到破坏性模式时直接拒绝，不执行；
          拒绝文本会回传给模型（它能看到原因并换方案）
        * 工作目录固定在项目根：显式离开工作区的命令（`cd ..`、
          `cd /绝对路径`）会被拒绝——需要访问工作区外请说明理由
        * 超时强制终止整棵进程树，防止长命令卡死整个任务
        * 输出默认全量返回（上限 200,000 字符，WOVRA_OUTPUT_LIMIT 可调）；
          真超限时完整内容落盘 output/spill/ 并给出路径，不丢数据
    """
    safety._audit(f"[run_command] {command}")  # 无论执行与否，命令原文都进审计
    for pattern in safety._DENIED_PATTERNS:
        if pattern in command:
            return (
                f"已拒绝执行危险命令：包含被禁止的模式 `{pattern}`。"
                f"如需完成类似效果，请使用更安全的替代方案。"
            )
    escape = safety._command_escape_targets(command)
    if escape:
        reason, targets = escape
        # venv 可用性提示（2026-09-11 运行者实测）：uv 建的 .venv/bin/python
        # 是指向工作区外系统解释器的符号链接，命中 _linked_outside 被拦——
        # 这是安全层的有意行为，但第一次撞上的人会以为工具坏了。识别出
        # `.venv/` 时把正确用法（uv run）直接给出来，省一轮试错。
        hint = (
            "提示：`.venv/bin/...` 被拦是因为 venv 解释器是指向工作区之外"
            "的符号链接——请改用 `uv run ...`（如 `uv run python -m pytest`）。"
            if ".venv/" in command else ""
        )
        # 越界授权（2026-09-11 用户拍板）：目标已授权或用户当场授权一次 →
        # 放行执行；未授权 → 拒绝。targets 提取失败（空）时保守拒绝。
        if targets and safety._request_path_authorization(targets, "run_command"):
            safety._audit(f"[run_command][越界已授权] {command}")
        else:
            note = safety.auth_rejection_note()
            tail = (
                f"{note}。"
                if note else
                "越界访问需用户授权：授权一次后该路径自动放行"
                f"（授权清单: {safety.PROJECT_ROOT / '.wovra' / 'authorized-paths.json'}）。"
            )
            return (
                f"已拒绝执行：命令试图{reason}（{command[:120]}）。"
                f"所有命令默认限定在工作区 {safety.PROJECT_ROOT} 内运行。"
                f"{hint}{tail}"
            )
    reason = safety._confirm_reason(command)
    if reason and not safety._ask_yes_no(
        f"命令包含敏感操作（命中 `{reason}`），是否允许执行？\n  {command[:200]}"
    ):
        safety._audit(f"[run_command][用户拒绝] {command}")
        return (
            "用户拒绝了该命令的执行。请换一种无副作用的做法，"
            "或向用户说明为什么需要它。"
        )
    wait = _COMMAND_TIMEOUT if timeout is None else max(1, min(int(timeout), 600))

    # 输出重定向到临时文件而不是 PIPE：文件没有"写端被孙进程攥住"
    # 的问题（http.server 这类孤儿进程曾把管道清理阶段永久挂死），
    # 也没有 PIPE 缓冲写满导致的子进程阻塞，超时后的清理必然可返回
    # 子进程强制 UTF-8 输出：否则中文输出按各自主观编码（gbk/utf-8）
    # 混流，父进程解码必出乱码（实测 py_compile 输出花屏）
    child_env = dict(os.environ, PYTHONUTF8="1")
    # 子进程显式声明非交互（safety.NONINTERACTIVE_ENV）：本层已给子进程
    # 接了 DEVNULL，但 Windows 下 NUL 的 isatty() 仍为 True（实测，
    # worklog-20260911.md §7）——只靠 stdin 判定，子进程内的确认门会落在
    # "打印提示→读 EOF→拒绝"的中间态。显式标记让它成为确定事实。
    child_env[safety.NONINTERACTIVE_ENV] = "1"
    started = time.monotonic()
    with tempfile.TemporaryFile() as out_f, tempfile.TemporaryFile() as err_f:
        # stdin 显式接 DEVNULL（worklog-20260911.md §5-P2）：子进程继承终端
        # stdin 时 isatty()=True——命令内部若再触发工具层确认门（例：跑探针
        # 脚本），会阻塞在用户**看不见**的提示上（输出被重定向到临时文件、
        # 命令结束才回显），等待期间用户敲的字符还会被那个 input() 吃掉，
        # 敲到 y 就等于无提示地授权了界外访问。接 DEVNULL 后读 stdin 立即
        # EOF，确认门走确定的非交互路径（敏感操作自动放行并留审计；
        # 越界授权安全拒绝）。
        proc = subprocess.Popen(
            command,
            shell=True,
            stdin=subprocess.DEVNULL,
            stdout=out_f,
            stderr=err_f,
            cwd=safety.PROJECT_ROOT,  # 固定工作目录：相对路径都在项目内
            env=child_env,
            # POSIX：让子进程自成进程组，超时后 killpg 整组杀掉而不伤自身
            start_new_session=os.name != "nt",
            # Windows：新进程组默认免疫 Ctrl+C——用户中断对话不能带走
            # 正在运行的服务器/安装进程（同控制台进程会收到 CTRL_C）
            creationflags=(
                subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
            ),
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
        stdout = _decode_output(out_f.read()).strip() or "(无输出)"
        stderr = _decode_output(err_f.read()).strip() or "(无输出)"

    elapsed = time.monotonic() - started
    header = (
        f"命令执行失败（exit_code={proc.returncode}，耗时 {elapsed:.1f}s）"
        if proc.returncode != 0
        else f"exit_code=0（耗时 {elapsed:.1f}s）"
    )
    # 耗时进结果（2026-09-08 用户拍板，DeepSeek 点评采纳）：模型看到
    # "这条跑了 90s"才有自调节信号——验证分层的代价可见，零配额零
    # 语义判断。重型验证（浏览器 E2E 等）的累计账由此可算。

    def _clip(text: str) -> str:
        """默认全量返回，只在超爆阀时降级（2026-09-11 用户两次拍板）。

        历史沿革与教训：原实现只留前 1500 字符（中段直接消失）；随后改
        「首尾各留一大段」，两版共同的毛病是**上限太小**且**白留**——
        正常 `dir /s`、跑一次测试就能撞上，模型看不到中段就换方法反复猜、
        白烧数轮到数十轮往返。

        现行口径（worklog §26）：默认上限 200,000 字符（WOVRA_OUTPUT_LIMIT
        可调），未超限原样全量返回；真超限时只内联开头一小段预览，附上
        原文体量（字符/行数）与落盘路径，完整内容可用 read_file 取回——
        大输出不进上下文，但一个字都不丢，模型还知道它有多大。
        """
        return limits.clip(text, "run_command")

    return (
        f"{header}\n"
        f"stdout:\n{_clip(stdout)}\n"
        f"stderr:\n{_clip(stderr)}"
    )
