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
"""

import os
import re
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

# 界内链接穿透（探针实测的洞）：`cat escape.txt` 里没有绝对路径、
# 也没有 `..`，但 escape.txt 是指向界外的链接——shell 会顺着读到界外。
# 静态分析无法判断"命令里哪个 token 是路径"，只能对**疑似路径的 token**
# 逐个做工作区内的链接解析：token 在工作区内存在、且解析后落在界外 → 拦。

def _linked_outside(command: str) -> str | None:
    """命令里出现"界内指向界外的链接"时返回该 token。

    这是对绝对路径检测的补充：`cat escape.txt` 不含任何绝对路径，
    但 escape.txt 是界外链接。只解析**像文件名的 token**（含 `.` 或 `/`、
    且不是选项/URL），逐个查它在工作区内是否为指向界外的链接。
    """
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


def _outside_absolute_paths(command: str) -> list[str]:
    """找出命令行里指向工作区之外的绝对路径字面量。

    只认"像路径"的 token：以 / 开头、不是命令行选项、不是 URL。
    """
    root = str(PROJECT_ROOT.resolve())
    found: list[str] = []
    # 单独一个 `/`（如 `find / -name x`）也是界外目标；用词边界匹配，
    # 避免把 `a/b` 里的斜杠或除法当路径。
    if re.search(r"(?:^|[\s'\"=])/(?=\s|$)", command):
        found.append("/")
    # 前导断言必须排除 `.` 和 `-`：`./x`、`../x`、`a/../b` 都是**相对**
    # 路径，把其中的 `/x` 当绝对路径会误拦（自我测试抓到的：一条
    # `cat ./t1.txt` 被报成"访问工作区之外的绝对路径 /t1.txt"）。
    for raw in re.findall(r"(?<![\w:/.\-])/[^\s'\"|;&><)]*", command):
        token = raw.rstrip(",;")
        if not token or token == "/":
            continue
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
    return found


def _command_escape(command: str) -> str | None:
    """检测命令里"离开工作区"的意图，返回原因；没有则 None。

    两类：
    * `cd` 到界外（`cd ..`、`cd /tmp`、`cd ~`、`bash -c 'cd /tmp'`）；
    * 出现指向界外的绝对路径字面量（`cat /etc/passwd`）。

    判定的是 **cd 处于命令位置**的上溯，而不是"出现 cd 二字"：

        cd ..                    → 拦（段首命令）
        x; cd ..                 → 拦（分隔符后）
        bash -c 'cd /tmp && pwd' → 拦（shell 包装）
        echo 'cd ..' > note.txt  → 放行（cd 是 echo 的参数，只是文本）
        grep -rn 'cd ' docs/     → 放行（同上）

    写文档/测试断言时经常要引用 "cd .." 这个字符串，误伤它们得不偿失。
    """
    command_word = r"(?:^|[;&|(])\s*"          # 段首、分隔符或子 shell
    wrapper = r"(?:[A-Za-z0-9_./-]+\s+-c\s+['\"]?\s*)?"  # bash -c '…'
    target = r"['\"]?(?:\.\.|/|~|\$HOME|\$\{HOME\})"      # 界外目标（可带引号）
    if re.search(command_word + wrapper + r"cd\s+" + target, command):
        return "cd 到工作区之外"
    outside = _outside_absolute_paths(command)
    if outside:
        return f"访问工作区之外的绝对路径（{outside[0]}）"
    traversal = _traverses_outside(command)
    if traversal:
        return f"用相对路径上溯到工作区之外（{traversal}）"
    linked = _linked_outside(command)
    if linked:
        return f"经由指向工作区之外的链接（{linked}）"
    return None

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
    """写入类工具的统一入口：词法拒绝 `..`，且不允许写到界外链接上。

    write/edit/replace/restore 都会**改动内容**，所以除了不接受
    中间穿越，也不能顺着一个指向界外的链接去写——那等于从工作区
    内部改写外部文件。读类工具同理（read_file 也走这套判定）。
    """
    target = _safe_path_lexical(relative)
    if not _within_root(target):
        raise ValueError(
            f"路径越界，只允许访问项目目录内的文件: {relative}"
            f"（该路径是指向工作区之外的符号链接；允许的根目录: {PROJECT_ROOT}）"
        )
    return target


def _safe_directory(directory: str) -> Path:
    """目录类参数（list_files/search_files/glob_files）的统一入口。

    与 _safe_path_lexical 同规则（拒绝 `..`），额外要求解析后仍在
    界内——遍历的起点不能是一个指向界外的链接。
    """
    target = _safe_path_lexical(directory)
    if not _within_root(target):
        raise ValueError(
            f"路径越界，只允许访问项目目录内的文件: {directory}"
            f"（该路径是指向工作区之外的符号链接；允许的根目录: {PROJECT_ROOT}）"
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
    r"\bgit\s+(commit|tag|merge|rebase|remote\s+add)\b",
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


def _ask_yes_no(question: str) -> bool:
    """交互环境 y/N 询问（默认拒绝）；非交互环境自动放行并留审计。

    等待用户回答期间置 user_input_pending 标记：用户思考多久都行
    （不设超时），但终端看门狗不计这段时间的执行时长。"""
    import sys

    global _user_input_pending
    if not sys.stdin.isatty():
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
