"""文件工具：列目录/读/搜/找 + 变更类（写/改/替换/删/移/回滚）。

另含两项跨工具的状态设施：

* 文件观察注册表（过期保护）：读过的文件被外部改动后再写会被拒绝；
* 版本档案（checkpoint）：覆盖/编辑/删除前自动归档，restore_file 可回滚。

遍历通道（search_files / glob_files）走 `_walk` 做**逐项**校验：
起点校验不等于遍历逐项校验，rglob 会跟随符号链接穿出工作区——这条是
agent-test/tool-layer-audit-20260909.md 问题 #1 的教训。
"""

import itertools
import os
import re
import time
from pathlib import Path

from . import safety


# 搜索时跳过的噪声目录（依赖、缓存、运行时数据——搜索它们只有噪音）
_IGNORED_DIRS = {
    ".git", ".venv", "__pycache__", ".pytest_cache",
    "tasks", "output", "node_modules", ".wovra",
}

# ---- 遍历通道（统一逐项校验） -------------------------------------------------
# 审计教训（agent-test/tool-layer-audit-20260909.md 问题 #1）：
# 起点校验 ≠ 遍历逐项校验。rglob 会跟随符号链接、会走进界外目录，
# 只有对**每一个**候选路径二次判定，遍历才是安全的。search_files 与
# glob_files 共用这一个实现，防护不再逐工具手写。

_SYMLINK_IN_ROOT = "root"   # 只认指向界内的链接（默认，零误伤）
_SYMLINK_SKIP = "skip"      # 一律跳过（不接受链接入参时使用）


def _walk(root: Path, glob: str, symlinks: str = _SYMLINK_IN_ROOT):
    """安全遍历 root 下匹配 glob 的文件——逐项校验，越界即跳过。

    三层防护：
    1. root 本身已由 _safe_directory 校验（起点）；
    2. 每个候选路径按 resolve 后是否仍在界内过滤——rglob 跟随
       symlink 到界外时，这里把它挡在读取之前（问题 #1 的正解）；
    3. 链接直接指向界外文件时 resolve 落在界外，同样被 2 拦下。

    symlinks="root"（默认）额外放行**指向界内**的链接——界内做别名
    是正常工程行为，不该误伤；"skip" 则一律跳过链接。

    界外授权目录（2026-09-11 用户拍板）：起点经用户授权（目录=其下
    全部内容）时，遍历边界从工作区切换到授权目录本身——授权目录内部
    的文件正常产出，不再被"越界即跳过"过滤。

    去重：链接与其目标都命中时只产出一次（按真实路径判重）——
    否则 `glob *.txt` 会把同一个文件列两遍，模型会以为有两份。
    """
    root_resolved = safety.PROJECT_ROOT.resolve()
    # 界外授权目录：起点已获授权 → 该目录内部按授权目录为边界
    try:
        boundary = (
            root.resolve() if safety.is_authorized(str(root.resolve()))
            else root_resolved
        )
    except OSError:
        boundary = root_resolved
    seen: set[Path] = set()
    for path in sorted(root.rglob(glob)):
        if not path.is_file():
            continue
        resolved = path.resolve()
        if not resolved.is_relative_to(boundary):
            continue  # ① 遍历穿出工作区/授权目录（含跟随 symlink 到界外）
        try:
            relative = resolved.relative_to(boundary)
        except ValueError:
            continue
        if any(part in _IGNORED_DIRS for part in relative.parts):
            continue  # ② 噪声目录
        if symlinks == _SYMLINK_SKIP and path.is_symlink():
            continue  # ③ 链接策略
        if resolved in seen:
            continue  # ④ 链接与目标同体：只算一次
        seen.add(resolved)
        yield resolved


def _is_hidden(relative: Path) -> bool:
    """相对路径的任一段以 . 开头（隐藏文件/目录）。"""
    return any(part.startswith(".") for part in relative.parts)


def _display_rel(path: Path) -> Path:
    """路径 → 展示用相对路径；授权目录（界外）内文件回退为绝对路径。

    search_files/glob_files 的输出会被模型用于 read_file——read_file 只收
    工作区相对路径，所以界外授权文件输出绝对路径，模型改用 run_command
    访问（2026-09-11 授权机制引入后新增场景）。
    """
    try:
        return path.relative_to(safety.PROJECT_ROOT)
    except ValueError:
        return path

# ---- 只读工具 -------------------------------------------------------------


def list_files(directory: str = ".") -> list[str]:
    """列出项目内某个目录下的文件和子目录（不含递归）。

    文件附带大小与修改时间——帮模型决定分段读取策略、判断内容新鲜度。
    """
    path = safety._safe_directory(directory)
    # 参数误用预检（2026-09-13 摩擦修复）：目录不存在/目标是文件 → 明确提示而不是裸 OSError
    if not path.exists():
        return f"目录不存在: {directory}（解析为 {path}）。先确认目录路径，可用 glob_files('*') 看看现有目录。"
    if path.is_file():
        return (
            f"目标是一个文件而非目录：{directory}。list_files 列目录内容；"
            f"读该文件请用 read_file('{directory}')。"
        )
    out = []
    for p in sorted(path.iterdir()):
        if p.is_dir():
            out.append(p.name + "/")
            continue
        try:
            st = p.stat()
            size = f"{st.st_size / 1024:.1f}KB" if st.st_size >= 1024 else f"{st.st_size}B"
            mtime = time.strftime("%m-%d %H:%M", time.localtime(st.st_mtime))
            out.append(f"{p.name}（{size}, {mtime}）")
        except OSError:
            out.append(p.name)
    return out


def read_file(path: str, start_line: int = 1, num_lines: int = 200) -> str:
    """按行读取项目内一个文本文件的内容片段。需要通读整个文件时，
    按 num_lines=400 连续分段读取，不要零碎小段反复读。

    大文件请配合 search_files 先定位，再用 start_line/num_lines
    分段读取——单次最多 400 行，返回值会标明文件总行数和
    继续读取的位置。

    路径必须是工作区内的相对路径，不接受 `..` 上溯（含 `sub/../x`
    这种中间穿越）——绕路不产生歧义，直接拒掉。指向工作区之外的
    符号链接也读不到。
    """
    target = safety._safe_write_path(path)  # 与写入类同一套判定：不接受 .. 与界外链接
    # 参数误用预检（2026-09-13 摩擦修复）：把裸 OSError 转成可行动的提示。
    # 模型看到的是工具返回文本而不是异常栈——目标类型不对/不存在要一次说清怎么办。
    # 文案保留 "不存在"/"是目录" 子串：lifecycle/blocks 靠它们判断读是否真发生。
    if target.is_dir():
        return (
            f"路径是目录（而非文本文件）：{path}。read_file 只读文件；"
            f"要列出该目录内容请用 list_files('{path}')，"
            f"要找其中的文件请用 glob_files('*', directory='{path}')。"
        )
    if not target.exists():
        return (
            f"文件不存在: {path}（解析为 {target}）。"
            f"先用 glob_files('*') 确认路径拼写与所在目录，再重试 read_file。"
        )
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


def search_files(pattern: str, directory: str = ".", glob: str = "*",
                 context: int = 0) -> str:
    """在项目内用正则表达式搜索文本文件（类似 grep）。

    返回 `路径:行号: 行内容` 格式的匹配，最多 50 条；
    自动跳过 .git/.venv 等噪声目录。找"某个函数在哪定义"、
    "哪个文件用了某配置" 都靠它。
    context：每个匹配额外附带前后 N 行上下文（类似 grep -C，单行内
    以 ⏎ 连接）——判断匹配性质用；默认 0（省 token）。
    """
    try:
        regex = re.compile(pattern)
    except re.error as error:
        raise ValueError(f"正则表达式无效: {error}") from error

    root = safety._safe_directory(directory)
    # 参数误用预检（2026-09-13 摩擦修复）：directory 指向文件时不再静默"无匹配"，
    # 那会误导模型以为目录里真的没内容（实测踩过）。明确指路。
    if root.is_file():
        return (
            f"directory 指向一个文件而非目录：{directory}。search_files 在目录内搜内容；"
            f"读该文件请用 read_file('{directory}')，"
            f"或在它所在目录内搜索（directory=其父目录）。"
        )
    if not root.exists():
        return f"directory 不存在: {directory}（解析为 {root}）。先确认目录路径。"
    matches: list[str] = []
    for path in _walk(root, glob):
        if path.stat().st_size > 1_000_000:  # 跳过超大文件
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, PermissionError):
            continue
        all_lines = text.splitlines()
        for line_number, line in enumerate(all_lines, start=1):
            if regex.search(line):
                # 统一用 / 分隔，输出跨平台一致（也便于回填给 read_file 等工具）
                relative = _display_rel(path).as_posix()
                entry = f"{relative}:{line_number}: {line.strip()[:200]}"
                if context > 0:
                    lo = max(0, line_number - 1 - context)
                    hi = min(len(all_lines), line_number + context)
                    snippet = " ⏎ ".join(
                        all_lines[i].strip()[:120] for i in range(lo, hi) if all_lines[i].strip()
                    )
                    entry += f"  ｜上下文: {snippet}"
                matches.append(entry)
                if len(matches) >= 50:
                    return "\n".join(matches) + "\n...(已达 50 条上限，请缩小搜索范围)"
    if not matches:
        return f"无匹配：pattern={pattern!r}, directory={directory!r}, glob={glob!r}"
    return "\n".join(matches)


def glob_files(pattern: str, directory: str = ".", include_hidden: bool = False) -> str:
    """按文件名通配模式查找文件（如 *.py、docs/**/*.md），返回相对路径。

    模式递归匹配所有子目录（*.py 等价于 **/*.py）；与 search_files
    （搜内容）互补：找"有哪些文件"用本工具，找"哪些文件里有什么
    内容"用 search_files。自动跳过 .git/.venv 等噪声目录，最多
    返回 200 条。

    include_hidden：是否把隐藏文件/目录（.env、.github 等）算进结果。
    注意标准的通配语义：`*.py` 不匹配 .a.py，`*` 也不匹配 .env——
    要找隐藏文件除了开这个开关，模式本身也要能匹配（如 `.*`）。
    排障时找不到配置就打开它。
    """
    root = safety._safe_directory(directory)
    if root.is_file():
        return (
            f"directory 指向一个文件而非目录：{directory}。glob_files 按文件名在目录内查找；"
            f"该文件已存在，读它请用 read_file('{directory}')。"
        )
    if not root.exists():
        return f"directory 不存在: {directory}（解析为 {root}）。先确认目录路径。"
    filtered = [
        p for p in _walk(root, pattern)
        if include_hidden or not _is_hidden(_display_rel(p))
    ]
    filtered.sort(key=lambda p: p.as_posix())
    if not filtered:
        hint = "" if include_hidden else "（隐藏文件未计入，需要时加 include_hidden=True）"
        return f"无匹配文件: {pattern}（directory={directory}）{hint}"
    lines = [_display_rel(p).as_posix() for p in filtered[:200]]
    more = f"\n…(共 {len(filtered)} 个，已显示前 200)" if len(filtered) > 200 else ""
    return "\n".join(lines) + more

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

# ---- 文件版本档案（checkpoint：无 git 环境的后悔药） ------------------------
# 每次覆盖/编辑/按行替换之前，旧内容自动归档到 .wovra/history/<相对路径>/
# （路径中的 / 替换为 __）。每文件保留最近 _HISTORY_KEEP 份，超出淘汰最旧；
# restore_file 列出并回滚（回滚前当前内容也归档——回滚本身可再回滚）。

_HISTORY_KEEP = 10


def _history_slot(target: Path) -> Path | None:
    """文件 → 它的版本档案目录（从 PROJECT_ROOT 现算，测试可重定向）。

    授权目录外的文件（界外链接目标）不属于工作区版本档案：返回 None
    （_archive_version 会跳过归档，写入照常进行）。
    """
    try:
        rel = target.relative_to(safety.PROJECT_ROOT).as_posix()
    except ValueError:
        return None
    return safety.PROJECT_ROOT / ".wovra" / "history" / rel.replace("/", "__")


_VERSION_SEQ = itertools.count(1)


def _archive_version(target: Path) -> str | None:
    """把文件的当前内容归档为一份历史版本，返回版本时间戳（失败返回 None）。

    时间戳 = 秒级时间 + 进程内递增序号：同秒内的多次归档也能保证
    文件名字典序 = 时间序（版本列表按名排序即按时间排序的前提）。
    """
    try:
        old = target.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None  # 非文本/不可读：无法归档，但也不阻止写操作
    slot = _history_slot(target)
    if slot is None:
        return None  # 界外授权文件：不在工作区版本档案内，跳过归档
    slot.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{next(_VERSION_SEQ):04d}"
    (slot / f"{stamp}.bak").write_text(old, encoding="utf-8")
    versions = sorted(slot.glob("*.bak"))
    for stale in versions[:-_HISTORY_KEEP]:
        stale.unlink(missing_ok=True)
    return stamp


def restore_file(path: str, version: str = "") -> str:
    """列出或回滚文件的历史版本（checkpoint 后悔药）。

    version 传空 → 列出该文件所有可用版本（时间戳 + 体量 + 首行）；
    version 传时间戳前缀（如 20260907-14）→ 回滚到该版本。当前内容
    会先归档——回滚本身可再回滚。文件被外部修改过会拒绝（防覆盖用户
    的新改动）。
    """
    target = safety._safe_write_path(path)
    stale = _stale_error(target)
    if stale:
        return stale
    slot = _history_slot(target)
    if slot is None:
        return f"{path} 位于工作区之外（授权文件不纳入版本档案），restore_file 仅支持工作区内文件"
    versions = sorted(slot.glob("*.bak")) if slot.exists() else []
    if not versions:
        return f"{path} 没有历史版本（版本归档自启用 checkpoint 起生效）"
    if not version:
        lines = [f"{path} 的历史版本（共 {len(versions)} 份，restore_file 传时间戳回滚）："]
        for v in versions:
            body = v.read_text(encoding="utf-8")
            first = body.splitlines()[0][:60] if body.splitlines() else "(空)"
            lines.append(f"  {v.stem}  {len(body):,} 字符  首行: {first}")
        return "\n".join(lines)
    matches = [v for v in versions if v.stem.startswith(version)]
    if len(matches) != 1:
        return (
            f"版本前缀 {version!r} 匹配到 {len(matches)} 份（需要恰好 1 份）。"
            f"可用版本：{', '.join(v.stem for v in versions)}"
        )
    if target.exists():
        _archive_version(target)  # 当前内容先归档：回滚可撤销
    target.write_text(matches[0].read_text(encoding="utf-8"), encoding="utf-8")
    _observe_file(target)
    safety._audit(f"[restore_file] {path} ← {matches[0].stem}")
    return f"已回滚 {path} 到版本 {matches[0].stem}（回滚前的内容已归档，可再次回滚）"

def delete_file(path: str) -> str:
    """删除项目内的一个文件（删除前自动归档，restore_file 可回滚）。

    删除是破坏性操作：交互环境 y/N 确认（默认拒绝），非交互环境放行
    并留审计标记。只处理文件——空目录请用 run_command 的 rmdir。

    符号链接的语义（2026-09-10 修正，审计问题 #3）：路径用词法解析，
    删的永远是**链接本身**，绝不顺着链接删掉它指向的文件。链接指向
    工作区外时也照删不误——删链接不碰目标，是界内操作（旧实现会报
    "路径越界"，用户只能绕 shell 的 unlink）。
    """
    target = safety._safe_path_lexical(path)
    if target.is_symlink():
        link_to = os.readlink(target)
        dangling = not target.exists()
        _archive_version(target) if not dangling else None
        target.unlink()  # unlink 作用于链接名本身，不触碰目标
        _file_registry.pop(target, None)
        safety._audit(f"[delete_file] {path} → {link_to}（仅删链接）")
        # 文案避开"不存在"——lifecycle/blocks 用子串判定操作失败
        # （读/删"没真发生"才是失败）。删悬空链接是**成功**的删除，
        # 写成"目标不存在"会被误判成幽灵（从未存在），状态就错了。
        tail = "（链接指向的目标未受影响）" if not dangling else "（该链接原本已失效）"
        return f"已删除符号链接 {path} → {link_to}{tail}"
    if not target.exists():
        return f"文件不存在: {path}（解析为 {target}）"
    if not target.is_file():
        return f"{path} 是目录而非文件——目录删除请用 run_command（rmdir，仅限空目录）"
    if not safety._ask_yes_no(f"确认删除文件 {path}？（删除前自动归档，可 restore_file 回滚）"):
        safety._audit(f"[delete_file][用户拒绝] {path}")
        return "用户拒绝了删除操作。请换一种做法或向用户说明原因。"
    _archive_version(target)
    target.unlink()
    _file_registry.pop(target, None)
    safety._audit(f"[delete_file] {path}")
    return f"已删除 {path}（删除前内容已归档，restore_file 可回滚）"


def move_file(path: str, new_path: str) -> str:
    """移动/重命名项目内的文件（可逆操作，审计留痕，不覆盖目标）。

    new_path 已存在时拒绝——move 永不静默覆盖。移动后文件观察注册表
    同步更新，后续 edit_file/write_file 以新路径为准。

    路径用词法解析：移动符号链接时移动的是链接本身（目标不动）。
    """
    src = safety._safe_path_lexical(path)
    dst = safety._safe_path_lexical(new_path)
    if dst.is_symlink() or dst.exists():
        return f"目标已存在: {new_path}（解析为 {dst}）——move 不覆盖，先删除或换名"
    if not src.is_symlink() and not src.exists():
        return f"文件不存在: {path}（解析为 {src}）"
    if src.is_symlink() and not src.exists():
        # 悬空链接：可以移动（移动的是链接名），但要说清
        dst.parent.mkdir(parents=True, exist_ok=True)
        src.rename(dst)
        _file_registry.pop(src, None)
        safety._audit(f"[move_file] {path} → {new_path}（悬空链接）")
        # 同样避开"不存在"：移动悬空链接是成功的（见 delete_file 注释）
        return f"已移动悬空符号链接 {path} → {new_path}（该链接原本已失效）"
    dst.parent.mkdir(parents=True, exist_ok=True)
    src.rename(dst)
    _file_registry.pop(src, None)
    _observe_file(dst)
    safety._audit(f"[move_file] {path} → {new_path}")
    return f"已移动 {path} → {new_path}"

# ---- 变更类工具（AUDITED_TOOLS，Agent 会做完整审计记录） --------------------


def write_file(path: str, content: str, force: bool = False) -> str:
    """创建或整体覆盖项目内的一个文本文件。

    覆盖是全量的——只改一部分请用 edit_file，它要求唯一定位，
    误伤面小得多。覆盖时旧内容会完整归档（restore_file 可回滚）
    并通过审计挂钩留底。若文件在你上次读取后被外部修改过，
    会拒绝执行并要求重新确认。
    防呆：新内容比现有内容缩小过半（且现有内容 ≥1000 字符）时拦截，
    确认是有意覆盖用 force=true 重试——失误的"整体薄壳化"在结果上
    与有意重写一模一样，只有大小差异可查（实测教训）。
    """
    target = safety._safe_write_path(path)
    # 参数误用预检（2026-09-13 摩擦修复）：目标是目录 → 明确提示而不是裸 IsADirectoryError
    if target.is_dir():
        return (
            f"路径是目录（而非文件），不能写入：{path}。write_file 只写文件；"
            f"要在该目录下新建文件请给出完整路径（如 {path}/新文件名），"
            f"或先用 list_files('{path}') 查看目录内容。"
        )
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
    if (not force and old is not None and len(old) >= 1000
            and len(content) < len(old) // 2):
        pct = int((1 - len(content) / len(old)) * 100)
        return (
            f"⚠ 防呆拦截：新内容比现有内容缩小约 {pct}%"
            f"（{len(old):,} → {len(content):,} 字符）。"
            f"如果这是有意的覆盖（拆分文件、重写为薄壳），用 force=true 重新"
            f"调用确认；如果只想改一部分，应该用 edit_file。"
        )
    # 允许写到尚不存在的子目录（模型经常给出 "reports/xx.md" 这类路径）
    target.parent.mkdir(parents=True, exist_ok=True)
    if existed:
        _archive_version(target)  # 覆盖前归档旧内容：restore_file 可回滚
    target.write_text(content, encoding="utf-8")
    _observe_file(target)
    action = "覆盖" if existed else "创建"
    if old is not None:
        # 旧内容完整留底（审计原则：能还原）；超大文件截断到 20000 字符
        backup = old if len(old) <= 20_000 else old[:20_000] + "\n...(已截断)"
        safety._audit(f"[write_file 旧内容备份] {path}:\n{backup}")
    return f"已{action} {path}（{len(content)} 字符）"


def _closest_anchor_hint(text: str, old_text: str,
                          threshold: float = 0.6) -> tuple[int, str] | None:
    """edit_file 锚点未命中时，找与 old_text 最接近的文件区域。

    返回 (行号, 该窗口首行)，供模型一次修正锚点，不必整文件重读。
    相似度用 SequenceMatcher 对等长行窗口逐一比对，阈值以下的
    完全不相关内容不产生提示。"""
    from difflib import SequenceMatcher

    lines = text.splitlines()
    if not lines or not old_text.strip():
        return None
    window = max(1, len(old_text.splitlines()))
    best_ratio, best = 0.0, None
    for start in range(0, max(1, len(lines) - window + 1)):
        chunk = "\n".join(lines[start:start + window])
        ratio = SequenceMatcher(None, chunk, old_text).ratio()
        if ratio > best_ratio:
            best_ratio, best = ratio, (start + 1, lines[start])
    if best is None or best_ratio < threshold:
        return None
    return best


def _mini_diff(provided: str, actual: str, max_lines: int = 24) -> str:
    """「你提供的 vs 文件实际」的最小差异展示（edit_file 未命中时反馈）。

    模型照着差异改一次 old_text 就能命中，省一整轮重读；两段逐字符
    相同时给出不可见空白提示（全角空格/行尾空格是实测陷阱）。
    """
    import difflib

    diff = list(difflib.unified_diff(
        provided.splitlines(), actual.splitlines(),
        fromfile="你提供的", tofile="文件实际", lineterm="",
    ))
    if not diff:
        return "（两者逐字符相同——请检查不可见空白：全角空格、行尾空格、\\r\\n 行尾）"
    if len(diff) > max_lines:
        diff = diff[:max_lines] + [f"…（差异过长，只显示前 {max_lines} 行）"]
    return "\n".join(diff)


def edit_file(path: str, old_text: str, new_text: str,
              replace_all: bool = False) -> str:
    """把文件中出现的 old_text 替换为 new_text（默认要求恰好出现一次）。

    强制唯一定位是防止"替换了不想替换的地方"的关键约束；确认意图
    就是全部替换时传 replace_all=True。锚点未命中时给出「你提供的
    vs 文件实际」的最小差异——照着差异改一次 old_text 即可命中，
    不必整文件重读。
    若文件在你上次读取后被外部修改过，会拒绝执行并要求重新确认。
    """
    target = safety._safe_write_path(path)
    stale = _stale_error(target)
    if stale:
        return stale
    if not target.exists():
        return (
            f"文件不存在: {path}（解析为 {target}）。"
            f"先用 glob_files 确认文件的实际位置——注意 path 参数应只含路径本身。"
        )
    text = target.read_text(encoding="utf-8")
    count = text.count(old_text)
    if count == 0:
        message = f"{path} 中未找到待替换文本（你提供的前 80 字符: {old_text[:80]!r}）"
        hint = _closest_anchor_hint(text, old_text)
        if hint:
            line_no, _ = hint
            window = max(1, len(old_text.splitlines()))
            actual = "\n".join(text.splitlines()[line_no - 1:line_no - 1 + window])
            message += (
                f"\n最接近的内容在第 {line_no} 行附近。"
                f"差异（你提供的 vs 文件实际）：\n{_mini_diff(old_text, actual)}"
            )
        raise ValueError(message)
    if count > 1 and not replace_all:
        positions, pos = [], text.find(old_text)
        while pos != -1 and len(positions) < 10:
            positions.append(text.count("\n", 0, pos) + 1)
            pos = text.find(old_text, pos + 1)
        where = ", ".join(f"第 {n} 行" for n in positions)
        more = f"（另有 {count - len(positions)} 处未列出）" if count > len(positions) else ""
        raise ValueError(
            f"{path} 中待替换文本出现 {count} 次：{where}{more}。"
            f"扩大上下文消歧，或加 replace_all=True 全部替换"
        )
    line_no = text.count("\n", 0, text.find(old_text)) + 1
    n = count if replace_all else 1
    replaced = text.replace(old_text, new_text) if replace_all else text.replace(old_text, new_text, 1)
    _archive_version(target)  # 编辑前归档：restore_file 可回滚
    target.write_text(replaced, encoding="utf-8")
    _observe_file(target)
    scope = f"全部 {n} 处" if replace_all else "唯一一处"
    # 替换片段对完整留底：改了哪段、改成了什么，一目了然
    safety._audit(f"[edit_file] {path}（替换{scope}）\n定位片段:\n{old_text}\n替换为:\n{new_text}")
    anchor = _persistent_anchor(text, line_no)
    return (f"已修改 {path}（替换{scope}，{len(old_text)} 字符 → {len(new_text)} 字符，"
            f"位于第 {line_no} 行附近{anchor}）")


def _persistent_anchor(text: str, line_no: int, max_lookback: int = 40) -> str:
    """编辑点上方最近的「持久锚点」：注释/函数/类定义行。

    行号在多轮编辑后会漂移，注释与函数名存活得久得多——回显它，
    模型下一轮还能靠它定位（实测反馈：行号快照易误读）。
    语言无关启发式：向上最多看 max_lookback 行，命中即返回。
    """
    lines = text.split("\n")
    anchor_re = re.compile(
        r"(/\*|\*/|//|#\s|def\s|function\s|class\s|const\s|<script|<style|--\s)"
    )
    for i in range(min(line_no, len(lines)) - 2, max(-1, len(lines) - 1 - max_lookback), -1):
        stripped = lines[i].strip()
        if stripped and anchor_re.search(stripped):
            shown = stripped if len(stripped) <= 50 else stripped[:50] + "…"
            return f"（↳ {shown}）"
    return ""


def replace_lines(path: str, start_line: int, end_line: int, new_content: str) -> str:
    """按行号把文件的 [start_line, end_line] 行区间（含两端）替换为
    new_content（传空串即删除该区间）。

    行号来自 read_file 的回显——确定性编辑，不依赖文本匹配，适合
    old_text 反复匹配失败的场景，也省去引用大段原文的 token。
    纪律：行号必须以最近一次 read_file 回显为准；你中间执行过任何
    写操作后请重新读取，否则行号已经漂移。文件被外部修改过会拒绝。
    """
    target = safety._safe_write_path(path)
    stale = _stale_error(target)
    if stale:
        return stale
    if not target.exists():
        return (
            f"文件不存在: {path}（解析为 {target}）。"
            f"先用 glob_files 确认文件的实际位置——注意 path 参数应只含路径本身。"
        )
    text = target.read_text(encoding="utf-8")
    lines = text.split("\n")
    trailing = bool(lines) and lines[-1] == ""
    if trailing:
        lines = lines[:-1]  # 末尾换行产生的空元素不是真实行
    total = len(lines)
    if not (1 <= start_line <= end_line <= total):
        return (
            f"行号越界：文件 {path} 共 {total} 行，收到 {start_line}-{end_line}。"
            f"请以最近一次 read_file 回显的行号为准"
        )
    old_block = "\n".join(lines[start_line - 1:end_line])
    new_lines = new_content.split("\n") if new_content else []
    replaced = lines[:start_line - 1] + new_lines + lines[end_line:]
    _archive_version(target)  # 替换前归档：restore_file 可回滚
    target.write_text("\n".join(replaced) + ("\n" if trailing else ""), encoding="utf-8")
    _observe_file(target)
    # 旧块与新块都留底（超长截断到 5000，原则与 write_file 备份一致）
    safety._audit(f"[replace_lines] {path} 第 {start_line}-{end_line} 行\n旧内容:\n"
           f"{old_block[:5000]}\n替换为:\n{new_content[:5000]}")
    return (f"已替换 {path} 第 {start_line}-{end_line} 行"
            f"（{end_line - start_line + 1} 行 → {len(new_lines)} 行，现共 {len(replaced)} 行）")
