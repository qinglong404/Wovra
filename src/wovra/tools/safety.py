"""工具层安全底座：工作区属主、审计挂钩、路径防护、命令越界判定、确认门。

本模块是 `wovra.tools` 包的共享底层，被 files / shell / background / web /
interaction 各模块依赖；它自身不 import 包内其它模块。

**可变全局的属主规则**（重构纪律）：`PROJECT_ROOT`、`_audit_recorder`、
`_user_input_pending` 在此定义。包内其它模块一律通过 `safety.PROJECT_ROOT`
动态读取；外部改写（task.py 绑定会话工作区、测试/探针重定向）也必须写到
这里——`wovra.tools.PROJECT_ROOT` 只是 import 时的值快照，改写包属性不会
生效（cli.py 需要快照语义，探针另有 cli_module 补丁，属既有行为）。

路径安全层两个入口，各工具按语义选用：

    * `_safe_path_lexical`  —— 词法拒绝 `..`，不跟随末段链接
                              （需要操作链接本身的工具用：delete/move）
    * `_safe_write_path` / `_safe_directory` —— 上述 + 拒绝指向界外的链接

**越界授权（2026-09-11 用户拍板）**：跨工作区访问不再一律拒绝——
先请求用户授权一次（交互 y/N，非交互安全拒绝），授权路径写入
`.wovra/authorized-paths.json`（持久化，重启仍在），之后访问放行。
授权粒度：单文件或目录（目录授权 = 其下全部内容）。未授权仍拦。
"""

import json
import os
import re
import sys
from pathlib import Path, PurePosixPath


# 项目根目录（工作区）：所有文件与命令都限定在这里。
# 解析顺序：
#   1. 环境变量 WOVRA_WORKSPACE（显式指定，脚本/自动化用）
#   2. 启动目录——在哪启动，工作区就在哪（2026-09-07 用户拍板：
#      曾有"仓库子目录自动回退仓库根"的反 Surprise 规则，实测与
#      使用直觉冲突——从 web/ 启动就该以 web/ 为工作区，删除）
# 注意：会话会绑定其工作区（见 task.py）——恢复旧会话时以会话记录为准。
PROJECT_ROOT = Path.cwd().resolve()
_env_ws = os.environ.get("WOVRA_WORKSPACE")
if _env_ws:
    PROJECT_ROOT = Path(_env_ws).resolve()
    PROJECT_ROOT.mkdir(parents=True, exist_ok=True)

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
# 有意保持保守——rm -r 这类即使"看起来安全"也拒绝，
# 模型收到拒绝文本后会自行寻找替代方案（这是流式循环的好处）。
# git 类破坏性操作（push/reset/clean/checkout/restore）2026-09-11 起
# 移出黑名单、改走确认门：用户 y/N 授权一次即可执行，不硬拦。
_DENIED_PATTERNS = (
    "rm -r",          # 递归删除（含 -rf/-fr）
    " -delete",       # find 的删除变体
    "sudo ",
    "mkfs",
    "dd if=",
    ":(){",           # fork 炸弹
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
    # 2026-09-10 补齐（审计问题 #4 的建议清单）：这些与 rm -r 同级危险，
    # 此前只拦了 rm -r——删除动作请走 delete_file（有归档可回滚）
    "unlink ",
    "truncate ",
    "shred ",
    "fdisk",
    "parted ",
    "mkswap",
    "init 0",
    "init 6",
    "halt",
    "poweroff",
    "kill -9 -1",
    "killall ",
    "> /dev/sd",
    "of=/dev/sd",
)

# 工作区约束（审计问题 #4）：run_command 是裸 shell，此前 `cd ..` 就能
# 进出工作区自由读写——文件工具全有路径校验，唯独 shell 通道没有。
# 注意审计只测了 `cd ..`，真跑起来才发现**绝对路径**才是更大的口子：
# `cat /etc/passwd`、`cp /etc/hostname ./x` 都畅通（09-10 用真实模型
# 端到端复测时，模型自己报了这条）。故本层拦两类：
#   ① cd 到界外（含 shell 包装）
#   ② 命令行里出现指向界外的**绝对路径**字面量
# 仍是粗筛：变量拼接、python -c 里的 os.chdir、base64 编码等绕得过，
# 完整隔离需要容器/低权限用户，见 agent-test/security-hardening-20260910.md §5。

# 系统路径白名单：这些绝对路径是**读**系统信息用的，误拦会挡掉正常排障。
# 注意 /proc/self 被移出白名单——它是可用的旁路：
# `cat /proc/self/cwd/../outside/secret.txt` 能绕过绝对路径检测（探针实测）。
# 注：本项目自己的 .venv/bin/python 也命中"指向界外的链接"被拦
# （uv 建的 venv 解释器是指向系统 Python 的符号链接）——这是有意行为，
# 正确用法是 `uv run ...`；shell.run_command 会给出该提示。
_ALLOWED_ABS_PREFIXES = (
    "/dev/null", "/dev/stdin", "/dev/stdout", "/dev/stderr", "/dev/tty",
)
_ALLOWED_ABS_EXACT = (
    "/etc/hosts", "/etc/resolv.conf",     # 网络排障高频（DNS 解析失败必看）
    "/dev/null", "/dev/stdin", "/dev/stdout", "/dev/stderr", "/dev/tty",
)

# 解释器/工具路径放行：`/usr/bin/python3 script.py` 是正常调用，不该拦。
# 只放行**指向可执行文件本体**的形式（末段是文件名），不放行目录列举
# （`ls /usr/bin` 会暴露系统全貌，仍拦）。
_INTERPRETER_PATH = re.compile(
    r"^/(?:usr/)?(?:local/)?(?:s?bin)/[\w.+-]+\.?(?:exe)?$"
)

# Windows 绝对路径通道（2026-09-11 两平台等价修复）：盘符绝对（C:\x、
# C:/x）与 UNC（\\server\share\x）。守卫此前只有 POSIX 的 `/` 规则，
# Windows 命令里的 `C:\...` 完全不被识别——探针 9 条 shell 越界用例在
# Windows 全绿漏拦。前导断言排除词内冒号（URL `http://x` 里的 `p://`
# 不误判）与路径中间位置（`a/../C:/x` 这类交给上溯通道）。
_WIN_ABS_PATH = re.compile(
    r"""(?<![\w:./\\-])(?:[A-Za-z]:[\\/][^\s'"|;&><)]*|\\\\[^\s'"|;&><)]+)"""
)

# 引号掩码（2026-09-11 误伤修复）：commit message（-m "…"）、文档文本
# （echo "…"）、grep 正则（grep -e '…'）里的 token 会被链接/上溯/孤立
# 斜杠检测当成真实路径误判。检测前先把**数据**引号段替换为占位符；
# 代码解释器的引号段是**真实要执行的代码**（bash -c '…'、perl -e '…'、
# python3 -c "…"），其内的越界路径是真实访问，必须原样保留给检测器。
# 按命令名区分：白名单命令的 -c/-e 段保留，其余引号段掩码。

_CODE_EXEC_COMMANDS = (
    "bash", "sh", "zsh", "ksh", "dash", "ash", "csh", "tcsh",
    "python", "python2", "python3", "perl", "ruby", "node", "php", "lua",
)
_EXEC_FLAG_ARG = re.compile(
    r"(?:^|[;&|(]\s*)(?:[A-Za-z0-9_./-]*/)?("
    + "|".join(re.escape(c) for c in _CODE_EXEC_COMMANDS)
    + r")(?:\.exe)?\s+-[ce]\s+(['\"])(.*?)\2",
    re.DOTALL,
)
_QUOTED_SEGMENT = re.compile(r"(['\"])(.*?)\1", re.DOTALL)


def _mask_quoted_text(command: str) -> str:
    """把命令里的数据引号段掩码；代码解释器的 `-c/-e` 段还原保留。

    白名单解释器（bash -c、perl -e、python3 -c…）的引号内是**要执行
    的代码**——其中的越界路径必须继续被检测；echo/-m/grep 等传参的
    引号内是**数据**——掩码掉避免误伤。
    """
    protected: list[str] = []

    def _keep(m):
        protected.append(m.group(0))
        return f"\x00{len(protected) - 1}\x00"

    masked = _EXEC_FLAG_ARG.sub(_keep, command)
    masked = _QUOTED_SEGMENT.sub(
        lambda m: m.group(1) + "TEXT" + m.group(1), masked
    )
    for i, seg in enumerate(protected):
        masked = masked.replace(f"\x00{i}\x00", seg)
    return masked


# 命令词集合：孤立 `/`（find /、ls /）只有紧跟**命令词或选项**时才
# 是根目录访问；普通词之间的 `/`（a / b、1 / 2、prompts.py / x）是
# 文本分隔符/除法（probe 实测：commit message 含"空格-斜杠-空格"被误拦）。
# 名单偏保守——误伤（拦死合法命令）代价远大于漏拦（越界访问还有
# 绝对路径/链接/上溯三个通道兜底，且最终有用户授权门）。
_ROOT_TARGET_COMMANDS = frozenset({
    "ls", "dir", "cat", "head", "tail", "find", "grep", "rg", "tree",
    "pwd", "rm", "cp", "mv", "mkdir", "touch", "chmod", "chown", "stat",
    "du", "df", "less", "more", "file", "xxd", "od", "tar", "rsync",
    "scp", "curl", "wget", "python", "python3", "bash", "sh", "awk",
    "sed", "sort", "uniq", "xargs", "env", "which", "whereis", "locate",
    "top", "ps", "kill", "tee", "dd", "mount", "umount", "open", "xdg-open",
})

# cmd 多字母开关（Windows 工具常用；单字母开关见 _is_cmd_option 的规则）。
# 白名单而非黑名单：只有**确认是开关**的名字才跳过，真实路径（/etc、
# /tmp、/usr…长度≥3 且不在表内）照旧受检。
_CMD_OPTION_WORDS = frozenset({
    "nobreak", "recurse", "pid", "im", "ad", "aa", "ar", "ah", "fs",
    "wait", "interactive", "force", "quiet", "verbose", "dry-run",
    "exclude", "include", "user", "system", "help", "version", "list",
})


def _is_cmd_option(token: str) -> bool:
    """`/x` 形态的 token 是否是**命令开关**而不是路径。

    2026-09-11 实测（worklog-20260911.md §9）：cmd 世界大量使用 `/s`
    `/b` `/t` `/nobreak` `/r` `/d` 这类开关，守卫把它们当成"界外绝对
    路径"——`dir /s /b x`、`timeout /t 25` 这类**完全合法的 Windows
    命令被反复弹授权询问**，用户随手答 Y 之后 `/s`、`/b`、`/t`、`D:\\d`
    这些碎片进了授权清单（其中 `D:\\` 一条即让整块 D 盘放行）。

    判据（保守）：单字母 `/X`（几乎所有单字母开关都在用，而单字母根
    目录极罕见）或已知多字母开关名。其余（`/etc`、`/tmp`、`/usr/bin`）
    照旧按路径处理。
    """
    if not token.startswith("/") or token == "/":
        return False
    body = token[1:].split("/")[0]
    return len(body) == 1 or body.lower() in _CMD_OPTION_WORDS


def _has_root_slash_target(masked: str) -> bool:
    """孤立 `/` 是否指向根目录：前面必须是命令词、选项或赋值。

    `find / -name x`、`ls -la /`、`root=/` → True；
    `echo a / b`、`prompts.py / support.py`、`1 / 2` → False。
    """
    for m in re.finditer(r"/", masked):
        if m.end() < len(masked) and not masked[m.end()].isspace():
            continue  # 非孤立（后面还有内容）：交给绝对路径通道
        head = masked[:m.start()]
        prev = re.search(r"([^\s'\"|;&<>=()$`]+)\s*$", head)
        if not prev:
            return True  # 行首就是 /（如 `find /` 无前 token 时，前面其实有命令）
        tok = prev.group(1).rstrip(",;")
        if tok.startswith("-") or tok in _ROOT_TARGET_COMMANDS:
            return True
        if re.search(r"(?:^|[\s;|&(])[A-Za-z_][\w]*=$", head):
            return True  # 赋值右值：root=/、dir=/
    return False


# 界内链接穿透（探针实测的洞）：`cat escape.txt` 里没有绝对路径、
# 也没有 `..`，但 escape.txt 是指向界外的链接——shell 会顺着读到界外。
# 静态分析无法判断"命令里哪个 token 是路径"，只能对**疑似路径的 token**
# 逐个做工作区内的链接解析：token 在工作区内存在、且解析后落在界外 → 拦。

def _linked_outside(command: str) -> str | None:
    """命令里出现"界内指向界外的链接"时返回该 token。

    这是对绝对路径检测的补充：`cat escape.txt` 不含任何绝对路径，
    但 escape.txt 是界外链接。只解析**像文件名的 token**（含 `.` 或 `/`、
    且不是选项/URL），逐个查它在工作区内是否为指向界外的链接。
    数据引号段（commit message 等）先掩码——那只是文本引用，不是执行。
    """
    command = _mask_quoted_text(command)
    root = PROJECT_ROOT.resolve()
    for raw in re.findall(r"[^\s'\"|;&<>=()$`]+", command):
        token = raw.strip(",;:")
        if not token or token.startswith("-"):
            continue
        if "://" in token:                      # URL
            continue
        if token in (".", "..") or set(token) <= {".", "/"}:
            continue  # 纯当前目录/上溯：由 cd 判定负责，这里不重复报
        if "/" not in token and "." not in token:  # 裸词（命令名/子命令）
            continue
        # 只处理相对路径 token；绝对路径由 _outside_absolute_paths 负责
        if token.startswith("/") or re.match(r"^[A-Za-z]:", token):
            continue
        if ".." in Path(token).parts:
            continue  # 上溯路径同理：交给上溯规则，避免重复归因
        candidate = root / token
        try:
            if not candidate.is_symlink() and not candidate.exists():
                continue
            resolved = candidate.resolve()
        except OSError:
            continue
        if not resolved.is_relative_to(root):
            return token
    return None


# 相对上溯：`cat ../outside/secret.txt`、`cat sub/../../x`。这类 token
# 既不以 / 开头（绝对路径规则看不见），也不是链接（链接规则看不见），
# 必须单独判。判定用**归一化后是否落在界外**，而不是"出现 .. 就拦"——
# `cat a/../b.txt` 归一回界内属正常写法，不该误伤。
def _traverses_outside(command: str) -> str | None:
    """命令里的相对路径 token 经归一化后落在界外 → 返回该 token。"""
    command = _mask_quoted_text(command)
    root = PROJECT_ROOT.resolve()
    for raw in re.findall(r"[^\s'\"|;&<>=()$`]+", command):
        token = raw.strip(",;:")
        if not token or token.startswith("-") or "://" in token:
            continue
        if token.startswith("/") or re.match(r"^[A-Za-z]:", token):
            continue  # 绝对路径：由 _outside_absolute_paths 负责
        if token in (".", "..") or set(token) <= {".", "/"}:
            continue  # 光秃秃的 `..`：cd 判定的辖区；且它常出现在引号文本里
        if ".." not in Path(token).parts:
            continue  # 不含上溯：链接规则/常规路径，不在此判
        normalized = Path(os.path.normpath(root / token))
        if not normalized.is_relative_to(root):
            return token
    return None


def _outside_absolute_paths(command: str, masked: str | None = None) -> list[str]:
    """找出命令行里指向工作区之外的绝对路径字面量。

    只认"像路径"的 token：以 / 开头、不是命令行选项、不是 URL。
    另有 Windows 绝对路径通道（盘符 / UNC，见 _WIN_ABS_PATH）。
    masked：引号文本已掩码的版本（_command_escape 传入）；单独调用时
    自动计算。孤立 `/` 的判定在 masked 上做——引号文本里的 `/` 是
    数据不是访问；`-c` 代码段内的绝对路径（python3 -c "open('/x')"）
    仍由下方 findall 在**原始命令**上捕获。
    """
    root = str(PROJECT_ROOT.resolve())
    found: list[str] = []
    # 单独一个 `/`（如 `find / -name x`）也是界外目标；只有紧跟命令词/
    # 选项才是根目录访问（见 _has_root_slash_target 注释）
    masked = masked if masked is not None else _mask_quoted_text(command)
    if _has_root_slash_target(masked):
        found.append("/")
    # 前导断言必须排除 `.` 和 `-`：`./x`、`../x`、`a/../b` 都是**相对**
    # 路径，把其中的 `/x` 当绝对路径会误拦（自我测试抓到的：一条
    # `cat ./t1.txt` 被报成"访问工作区之外的绝对路径 /t1.txt"）。
    for raw in re.findall(r"(?<![\w:/.\-])/[^\s'\"|;&><)]*", masked):
        token = raw.rstrip(",;")
        if not token or token == "/":
            continue
        if _is_cmd_option(token):
            continue  # `/s`、`/b`、`/t`、`/nobreak` 是命令开关，不是路径
        if token.startswith(root):          # 工作区内的绝对路径：放行
            continue
        if any(token == p or token.startswith(p + "/")
               for p in _ALLOWED_ABS_PREFIXES):
            continue
        if any(token == p for p in _ALLOWED_ABS_EXACT):
            continue
        if _INTERPRETER_PATH.match(token):  # 解释器本体：放行
            continue
        found.append(token)

    # Windows 绝对路径通道（2026-09-11 补齐）：本层加固此前只在 Linux 验证，
    # `C:\...` / `C:/...` / UNC 形式在守卫里没有对应规则——探针 9 条 shell
    # 用例在 Windows 上全部漏拦（cat/rm/重定向拿走界外金丝雀），且 `ln`
    # 用例会把界内链接重指向界外、连累后续反向对照（假失败）。判定与其它
    # 通道一致：解析后落在工作区内放行，界外交给授权门。
    # 只在 Windows 上启用：POSIX 里 `C:\x` 只是普通文件名（相对路径），
    # 不是绝对路径，据此判定会在"PROJECT_ROOT ≠ cwd"时产生误拦。
    if os.name == "nt":
        root_resolved = PROJECT_ROOT.resolve()
        for raw in _WIN_ABS_PATH.findall(masked):
            token = raw.rstrip(",;")
            if not token:
                continue
            try:
                resolved = Path(token).resolve()
            except (OSError, ValueError):
                continue
            if resolved.is_relative_to(root_resolved):
                continue  # 工作区内的绝对路径：正常用法，放行
            found.append(token)
    return found


def _command_escape_targets(command: str) -> tuple[str, list[str]] | None:
    """检测命令里"离开工作区"的意图，返回 (原因, 越界目标路径列表)。

    目标列表供授权门使用：授权后这些路径放行。没有越界则 None。
    判定的是 **cd 处于命令位置**的上溯，而不是"出现 cd 二字"：

        cd ..                    → 拦（段首命令）
        x; cd ..                 → 拦（分隔符后）
        bash -c 'cd /tmp && pwd' → 拦（shell 包装）
        echo 'cd ..' > note.txt  → 放行（cd 是 echo 的参数，只是文本）
        grep -rn 'cd ' docs/     → 放行（同上）

    写文档/测试断言时经常要引用 "cd .." 这个字符串，误伤它们得不偿失。
    """
    masked = _mask_quoted_text(command)
    command_word = r"(?:^|[;&|(])\s*"          # 段首、分隔符或子 shell
    wrapper = r"(?:[A-Za-z0-9_./-]+\s+-c\s+['\"]?\s*)?"  # bash -c '…'
    target = r"['\"]?(?:\.\.|/|~|\$HOME|\$\{HOME\})"      # 界外目标（可带引号）
    if re.search(command_word + wrapper + r"cd\s+" + target, command):
        targets = _extract_cd_targets(command)
        root_resolved = PROJECT_ROOT.resolve()
        # 界内 cd 放行（2026-09-11 运行者实测误伤）：`cd /home/.../Wovra`
        # 是工作区本身的绝对路径，不该拦；`cd /tmp`、`cd ..` 仍拦。
        # targets 提取失败（空）时保守拦截。
        if targets and all(
            Path(t).is_relative_to(root_resolved) for t in targets
        ):
            pass
        else:
            return "cd 到工作区之外", targets
    outside = _outside_absolute_paths(command, masked=masked)
    if outside:
        return f"访问工作区之外的绝对路径（{outside[0]}）", outside
    traversal = _traverses_outside(masked)
    if traversal:
        return f"用相对路径上溯到工作区之外（{traversal}）", [traversal]
    linked = _linked_outside(masked)
    if linked:
        return f"经由指向工作区之外的链接（{linked}）", [linked]
    return None


def _extract_cd_targets(command: str) -> list[str]:
    """cd 越界时提取目标路径（归一化为绝对路径，供授权使用）。

    `cd ..` → 工作区父目录；`cd /tmp` → /tmp；`cd ~` → 家目录。
    提取失败返回空列表（授权门会退化为"仅放行已授权项"）。
    """
    targets: list[str] = []
    for m in re.finditer(
        r"(?:^|[;&|(])\s*(?:[A-Za-z0-9_./-]+\s+-c\s+['\"]?\s*)?cd\s+['\"]?([^\s'\"|;&<>=()$`]+)",
        command,
    ):
        raw = m.group(1).rstrip(",;")
        if raw in (".", ".."):
            resolved = (PROJECT_ROOT / raw).resolve()
        elif raw.startswith("~"):
            expanded = os.path.expanduser(raw)
            resolved = Path(expanded).resolve()
        elif raw.startswith("/"):
            resolved = Path(raw).resolve()
        else:
            resolved = (PROJECT_ROOT / raw).resolve()
        targets.append(str(resolved))
    return targets


def _command_escape(command: str) -> str | None:
    """检测命令里"离开工作区"的意图，返回原因；没有则 None。"""
    result = _command_escape_targets(command)
    return result[0] if result else None

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

def _within_root(path: Path) -> bool:
    """path 解析后是否落在工作区内（不抛错，供遍历循环逐项过滤用）。

    对不存在的路径也能判定：resolve() 在 strict=False 下依然归一化
    `..` 与已存在的链接前缀。
    """
    try:
        return path.resolve().is_relative_to(PROJECT_ROOT.resolve())
    except OSError:  # 链接成环等病态路径：一律视为界外
        return False


def _safe_path_lexical(relative: str) -> Path:
    """词法解析路径——不跟随末段符号链接，且词法拒绝任何 `..` 上溯。

    两个用途：
    * 拒绝 `sub/../x` 这类中间穿越（起点校验漏掉的那类）——注意这里
      是**拒绝**而不是归一化：`sub/../t1.txt` 虽然能归一化回界内，
      但归一化等于承认"随便绕、落地在界内就行"，规则一复杂就守不住。
      简单规则才可审计：工作区内就用工作区内的相对路径。
    * 让 delete_file/move_file 能操作链接**本身**而不是它的目标。
    """
    pure = PurePosixPath(relative.replace("\\", "/"))
    if pure.is_absolute() or re.match(r"^[A-Za-z]:", relative):
        raise ValueError(
            f"路径越界，只允许访问项目目录内的文件: {relative}"
            f"（允许的根目录: {PROJECT_ROOT}）"
        )
    if ".." in pure.parts:
        raise ValueError(
            f"路径越界，只允许访问项目目录内的文件: {relative}"
            f"（不接受 `..` 上溯，请直接用工作区内的相对路径；"
            f"允许的根目录: {PROJECT_ROOT}）"
        )
    cleaned = [part for part in pure.parts if part not in ("", ".")]
    if not cleaned:
        return PROJECT_ROOT
    parent = (PROJECT_ROOT / Path(*cleaned[:-1])).resolve() if len(cleaned) > 1 else PROJECT_ROOT
    if not parent.is_relative_to(PROJECT_ROOT):
        raise ValueError(
            f"路径越界，只允许访问项目目录内的文件: {relative}"
            f"（允许的根目录: {PROJECT_ROOT}）"
        )
    return parent / cleaned[-1]


def _safe_write_path(relative: str) -> Path:
    """写入类工具的统一入口：词法拒绝 `..`，界外链接需用户授权。

    write/edit/replace/restore 都会**改动内容**，所以除了不接受
    中间穿越，也不能顺着一个指向界外的链接去写——那等于从工作区
    内部改写外部文件。读类工具同理（read_file 也走这套判定）。
    2026-09-11 起：指向界外的链接不再一律拒绝——经用户授权一次后
    放行（授权清单 .wovra/authorized-paths.json，重启仍在）。
    """
    target = _safe_path_lexical(relative)
    if not _within_root(target):
        try:
            resolved = target.resolve()
        except OSError:  # 链接成环等病态路径：不可授权
            resolved = None
        if resolved is None or not _request_path_authorization(
            [str(resolved)], "文件工具"
        ):
            raise ValueError(
                f"路径越界，只允许访问项目目录内的文件: {relative}"
                f"（该路径是指向工作区之外的符号链接；允许的根目录: {PROJECT_ROOT}。"
                f"越界访问需用户授权一次，授权后自动放行）"
            )
    return target


def _safe_directory(directory: str) -> Path:
    """目录类参数（list_files/search_files/glob_files）的统一入口。

    与 _safe_path_lexical 同规则（拒绝 `..`）；解析后仍在界内的链接
    照常放行，指向界外的链接需用户授权一次（授权目录 = 其下全部内容）。
    """
    target = _safe_path_lexical(directory)
    if not _within_root(target):
        try:
            resolved = target.resolve()
        except OSError:
            resolved = None
        if resolved is None or not _request_path_authorization(
            [str(resolved)], "目录遍历"
        ):
            raise ValueError(
                f"路径越界，只允许访问项目目录内的文件: {directory}"
                f"（该路径是指向工作区之外的符号链接；允许的根目录: {PROJECT_ROOT}。"
                f"越界访问需用户授权一次，授权后自动放行）"
            )
    return target

# ---- 敏感操作确认 -------------------------------------------------------------
# 黑名单拦"绝对不做"的；确认层拦"有实际副作用但合法"的（安装依赖、
# 版本提交、删除/移动/权限变更）。交互环境 y/N 询问（默认拒绝）；
# 非交互环境自动放行并留审计标记——实验与脚本不被阻塞，代价记录在案。

# 工具是否正在等待用户输入（确认/提问）：等待期不算执行时长，
# 终端看门狗静默——用户的思考时间不是工具的时间，也不设超时。
_user_input_pending = False


def user_input_pending() -> bool:
    return _user_input_pending


_CONFIRM_PATTERNS = (
    r"\bgit\s+(commit|tag|merge|rebase|push|reset|clean|checkout|restore|remote\s+add)\b",
    r"\b(pip|pip3)\s+install\b", r"\buv\s+(pip\s+)?(add|install|sync)\b",
    r"\bconda\s+(install|create|remove)\b",
    r"\bnpm\s+(install|i)\b", r"\bpnpm\s+(add|install)\b", r"\byarn\s+add\b",
    r"\b(rm|del|erase|rmdir)\b", r"\b(mv|move)\b",
    r"\b(chmod|chown|icacls)\b", r"\b(taskkill|kill)\b",
    r"\b(docker|podman)\s+(rm|rmi|system\s+prune)\b",
)


def _confirm_reason(command: str) -> str | None:
    """命令命中敏感操作 → 返回命中的模式；否则 None。"""
    for pattern in _CONFIRM_PATTERNS:
        if re.search(pattern, command):
            return pattern
    return None


NONINTERACTIVE_ENV = "WOVRA_NONINTERACTIVE"

# 最近一次授权请求的驳回说明（根目录等不可授权目标），由调用方取用
_auth_reject_note = ""


def _noninteractive() -> bool:
    """当前进程是否按**非交互**处理：显式标记优先，其次 stdin.isatty()。

    显式标记（`WOVRA_NONINTERACTIVE=1`）由工具层启动子进程时注入
    （shell.run_command / background._launch_background）。为什么需要它：
    **Windows 下 NUL 设备（`DEVNULL`）的 `isatty()` 仍返回 True**（实测，
    见 docs/worklog-20260911.md §7）——只靠 isatty 判定，子进程内的
    确认门会落在"打印提示 → input() 读 EOF → 拒绝"的中间态，与 Linux
    （/dev/null → isatty False → 静默放行）不一致。显式标记让"这是
    无人值守环境"成为确定事实，跨平台一致。
    """
    if (os.environ.get(NONINTERACTIVE_ENV) or "").strip() == "1":
        return True
    try:
        return not sys.stdin.isatty()
    except (AttributeError, ValueError, OSError):
        return True  # 无法判定时保守按非交互（宁不阻塞，不静默挂死）


def _ask_yes_no(question: str) -> bool:
    """交互环境 y/N 询问（默认拒绝）；非交互环境自动放行并留审计。

    等待用户回答期间置 user_input_pending 标记：用户思考多久都行
    （不设超时），但终端看门狗不计这段时间的执行时长。"""
    global _user_input_pending
    if _noninteractive():
        _audit("[确认] 非交互环境，自动放行")
        return True
    _user_input_pending = True
    try:
        answer = input(f"{question} [y/N] ").strip().lower()
    except EOFError:
        answer = ""
    finally:
        _user_input_pending = False
    # Ctrl+C 不吞：向上传播 = 打断本轮（统一的中断语义）
    return answer in ("y", "yes")


# ---- 越界授权（2026-09-11 用户拍板） ----------------------------------------
# 跨工作区访问不再一律拒绝：先请求用户授权一次（交互 y/N，非交互安全
# 拒绝），授权路径写入 .wovra/authorized-paths.json（与版本归档同目录，
# gitignored——绝对路径是机器本地状态，不进版本库），之后访问自动放行。
# 授权粒度：单文件或目录（目录授权 = 其下全部内容，按前缀匹配）。

def _authorized_store() -> Path:
    return PROJECT_ROOT / ".wovra" / "authorized-paths.json"


def _is_fs_root(path: Path) -> bool:
    """文件系统根 / 盘根（`D:\\`、`/`）——授权它等于放行**整块盘**。

    实测（worklog-20260911.md §9）：清单里出现过一条 `D:\\`，此后
    `is_authorized("D:/任何东西")` 全返回 True——工作区边界实质失效。
    根目录因此永不可授权。
    """
    try:
        return path.parent == path
    except (OSError, ValueError):
        return False


def _normalize_auth_targets(targets: list[str]) -> tuple[list[str], list[str]]:
    """授权目标规范化：一律解析为绝对路径，并剔除过于宽泛的根目录。

    返回 (可用目标, 被驳回目标)。被驳回的进不了清单——`/t`、`/b` 这类
    开关碎片与 `D:\\` 这类盘根都在此拦下。
    """
    valid: list[str] = []
    rejected: list[str] = []
    for raw in targets:
        try:
            resolved = Path(str(raw)).resolve()
        except (OSError, ValueError):
            rejected.append(str(raw))
            continue
        if _is_fs_root(resolved):
            rejected.append(str(resolved))
            continue
        text = str(resolved)
        if text not in valid:
            valid.append(text)
    return valid, rejected


def _load_authorized() -> list[str]:
    store = _authorized_store()
    try:
        return json.loads(store.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def _save_authorized(paths: list[str]) -> None:
    store = _authorized_store()
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text(
        json.dumps(paths, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def is_authorized(target: str) -> bool:
    """target（绝对路径）是否已被授权：自身精确匹配或位于某授权目录下。

    防御性过滤（§9 教训）：清单里的**根目录条目一律不作数**——手工编辑、
    历史遗留或旧版本写入的 `D:\\` 不能让整块盘放行。边界不能因为一条
    脏条目就失效。
    """
    t = Path(target).resolve()
    for p in _load_authorized():
        try:
            ap = Path(str(p)).resolve()
        except (OSError, ValueError):
            continue
        if _is_fs_root(ap):
            continue
        if t == ap or t.is_relative_to(ap):
            return True
    return False


def add_authorization(target: str) -> None:
    """把目标绝对路径写入授权清单（幂等；规范化后写入，根目录拒绝）。"""
    valid, rejected = _normalize_auth_targets([target])
    if rejected or not valid:
        _audit(f"[授权] 拒绝写入过于宽泛/非法的目标: {rejected or target}")
        return
    paths = _load_authorized()
    if valid[0] not in paths:
        paths.append(valid[0])
        _save_authorized(paths)
    _audit(f"[授权] {valid[0]}")


def auth_rejection_note() -> str:
    """最近一次授权请求的驳回说明（供调用方拼进拒绝文本，空串表示无）。"""
    return _auth_reject_note


def _request_path_authorization(targets: list[str], tool: str) -> bool:
    """越界目标未授权时向用户请求授权一次；全部已授权直接放行。

    交互环境 y/N（默认拒绝）；非交互环境**安全拒绝**——越界不是普通
    敏感操作（_ask_yes_no 对脚本自动放行是怕阻塞实验），授权自动放行
    等于静默打开工作区边界。授权成功 → 写入持久化清单并返回 True。

    目标先规范化（`_normalize_auth_targets`）：根目录/盘根直接驳回且
    **不询问**（问了也只能答"不"，那是全盘放行），驳回理由经
    `auth_rejection_note()` 交给调用方展示。
    """
    global _auth_reject_note
    _auth_reject_note = ""
    valid, rejected = _normalize_auth_targets(targets)
    if rejected:
        _audit(f"[授权] 驳回过于宽泛的目标: {rejected}")
        _auth_reject_note = (
            "该目标过于宽泛（文件系统根/盘根），不能作为授权对象："
            + "、".join(rejected)
        )
        return False
    new = [t for t in valid if not is_authorized(t)]
    if not new:
        return True
    if _noninteractive():
        _audit(f"[授权] 非交互环境拒绝越界访问: {new}")
        return False
    question = (
        f"以下目标在工作区之外（工具: {tool}），是否授权本次访问？\n"
        + "\n".join(f"  - {t}" for t in new)
        + "\n授权后写入授权清单（.wovra/authorized-paths.json），之后"
          "访问自动放行；未授权的越界访问仍会被拒绝。"
    )
    if _ask_yes_no(question):
        for t in new:
            add_authorization(t)
        return True
    _audit(f"[授权] 用户拒绝: {new}")
    return False
