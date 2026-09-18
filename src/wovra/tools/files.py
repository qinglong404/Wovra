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

from .. import observed
from . import documents, limits, permissions, safety


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

# 单次 read_file 的行数上限（2026-09-11 放开：原为 400 行硬上限，读一次
# 800 行的文件要两次往返；真正的天花板由字符爆阀 limits.clip 兜底）。
_READ_MAX_LINES = 20_000

# 工具输出的条数上限（2026-09-11 放开，worklog §25）：原 50/200 条太小，
# 正常一次搜索就能撞上，模型看不到后面的命中就得换关键词再搜一遍。
# 现行值由 limits.list_limit() 统一裁决（WOVRA_OUTPUT_LIMIT 可调大）。
_SEARCH_MAX_MATCHES = 200
_GLOB_MAX_FILES = 1_000

# 搜索时单文件大小上限（原 1MB；2026-09-12 放宽，见 search_files 内注释）
_SEARCH_MAX_BYTES = 8_000_000


def _walk(root: Path, glob: str, symlinks: str = _SYMLINK_IN_ROOT, skip_noise: bool = True):
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
    root_resolved = safety.workspace_root().resolve()
    # 界外授权目录：起点已获授权 → 该目录内部按授权目录为边界
    try:
        boundary = (
            root.resolve() if safety.is_authorized(str(root.resolve()))
            else root_resolved
        )
    except OSError:
        boundary = root_resolved
    seen: set[Path] = set()
    # 噪声目录的判定基准是**起点**而不是工作区根（2026-09-12）：显式指定
    # directory='output/spill' 时必须能搜到刚落盘的大输出，否则"量大不进
    # 上下文"就变成了"永远找不到"——大输出可以不全加载，但必须能定位。
    try:
        root_scope = root.resolve()
    except OSError:
        root_scope = root
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
        try:
            from_root = resolved.relative_to(root_scope)
        except ValueError:
            from_root = relative
        if skip_noise and any(part in _IGNORED_DIRS for part in from_root.parts):
            continue  # ② 噪声目录（相对起点判定：显式进 output/ 就照搜；skip_noise=False 用于零命中时诊断）
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
        return path.relative_to(safety.workspace_root())
    except ValueError:
        return path


def _merge_spans(hits: list[int], span: int, total: int) -> list[tuple[int, int]]:
    """命中行号 → 合并后的行区间 [(起, 止), …]。

    每个命中扩成 [i-span, i+span]（裁到文件范围），相邻或重叠的窗口并成一段：
    连续命中不重复输出，返回值直接告诉模型内容落在哪个行数范围。
    """
    spans: list[tuple[int, int]] = []
    for i in hits:
        lo, hi = max(1, i - span), min(total, i + span)
        if spans and lo <= spans[-1][1] + 1:
            spans[-1] = (spans[-1][0], max(spans[-1][1], hi))
        else:
            spans.append((lo, hi))
    return spans

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


def read_file(path: str, start_line: int = 1, num_lines: int = 200,
              pattern: str = "", context: int = 0) -> str:
    """按行读取项目内一个文件的内容片段。csv/tsv 表格与 docx/xlsx/pptx/pdf
    附件会自动解析成文本（不必自己写脚本或装解析库）。要通读整个文件时，直接把
    num_lines 放大一次读完（单次上限 20,000 行），不要用小段反复读同一个文件。
    只要一个结果时传 pattern='正则'（配 context=N 取前后 N 行）——返回相关内容的
    **行数范围**，不必全量加载。

    文件很大而只要一个结果时（典型场景：工具输出落盘后的长日志），传
    pattern='关键词'（正则）只返回匹配内容，不必全量加载。返回按
    **行数范围**给：相邻或重叠的命中合并成一段，段头写明 `第 a-b 行`，
    行首标 `:` 的是命中、标 `-` 的是 context 带出的上下文，行号可直接喂给
    replace_lines / edit_file 定位。context=N 时每个命中连带前后 N 行一起
    返回并同样合并——一次调用拿到能判断性质的上下文，不必再补读一次。
    不传 pattern 就是全量读取（pattern 模式下 start_line/num_lines 不参与，
    要按行取窗口就传 start_line/num_lines）——信息永远不丢，只是不必一次
    全进上下文。

    大文件请配合 search_files 先定位，再用 start_line/num_lines
    分段读取——返回值会标明文件总行数和继续读取的位置。

    路径必须是工作区内的相对路径，不接受 `..` 上溯（含 `sub/../x`
    这种中间穿越）——绕路不产生歧义，直接拒掉。指向工作区之外的
    符号链接也读不到。
    """
    target = safety._safe_read_path(path)  # 与写入类同一套边界判定：禁写区**读**得到
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
        origin = ""
    except UnicodeDecodeError:
        # 附件原生解析（2026-09-16，GAIA FINDINGS §3）：docx/xlsx/pptx/pdf/csv
        # 零依赖解成文本，省掉 agent 自己装库写脚本（实测一题堆出 2.3G 自造轮子）。
        # 解不出来才回退到原提示——绝不给乱码。
        parsed = documents.extract(target)
        if parsed is None:
            return (f"{path} 不是 UTF-8 文本文件，无法按文本读取。"
                    f"{documents.binary_hint(target)}")
        origin, text = parsed
    label = f"{origin}，" if origin else ""
    _observe_file(target, text)  # 记录状态 + 内容快照（过期拒绝靠它算差异）
    lines = text.splitlines()
    total = len(lines)
    if total == 0:
        return f"{path} 是空文件"
    if pattern:
        # 定位模式（2026-09-12）：大输出可以不全加载，但必须能快速找到要的
        # 那一段。context ＋ 区间合并（2026-09-17）：命中带前后 N 行、相邻窗口
        # 合成一段——一次调用给到可判断性质的上下文，连续命中不逐行重复。
        try:
            regex = re.compile(pattern)
        except re.error as error:
            return f"正则表达式无效: {error}（pattern={pattern!r}）"
        hits = [i for i, line in enumerate(lines, start=1) if regex.search(line)]
        if not hits:
            return (
                f"{path} 共 {total} 行，无匹配 {pattern!r}。"
                f"可换关键词，或直接全量读取（不带 pattern）。"
            )
        span = min(max(0, context), _READ_MAX_LINES)
        spans = _merge_spans(hits, span, total)
        cap = limits.list_limit(_SEARCH_MAX_MATCHES)
        hit_set = set(hits)
        chunks: list[str] = []
        used = left = 0
        for lo, hi in spans:
            # 命中行标 `:`、context 带出的行标 `-`（grep -C 同款）——不标的话
            # 拿到一段上下文分不清哪几行真正匹配。
            rows = [f"{n}{':' if n in hit_set else '-'} {lines[n - 1].strip()[:200]}"
                    for n in range(lo, hi + 1)]
            room = cap - used
            if room <= 0:
                left += len(rows)
                continue
            if len(rows) > room:
                left += len(rows) - room
                rows = rows[:room]
            chunks.append(
                (f"── 第 {lo}-{hi} 行 ──\n" if hi > lo else "") + "\n".join(rows)
            )
            used += len(rows)
        shown = "\n\n".join(chunks)
        more = f"\n…（还有 {left} 行未显示）" if left else ""
        scope = f"，{len(spans)} 段" if len(spans) > 1 else ""
        legend = "带 - 的行是上下文（不带就是命中）；" if span else ""
        return limits.clip(
            f"{path}（{label}共 {total} 行，匹配 {len(hits)} 行{scope}）\n{shown}{more}\n"
            f"...（{legend}要上下文：read_file('{path}', pattern={pattern!r}, context=10)；"
            f"要全文：不带 pattern 读）",
            f"read-{Path(path).name}", source=path,
        )
    start = max(1, start_line)
    if start > total:
        return f"{path} 共 {total} 行，start_line={start} 超出范围"
    # 行数上限（2026-09-11 放开）：原先硬卡 400 行，读一个 800 行的文件
    # 要两次往返；文件本来就在磁盘上，往返才是浪费。现在由 limits 统一
    # 控制（默认一次可读 20,000 行），超长内容再由字符爆阀兜底落盘。
    end = min(total, start + min(max(1, num_lines), _READ_MAX_LINES) - 1)
    body = "\n".join(lines[start - 1:end])
    header = f"{path}（{label}共 {total} 行，以下为第 {start}-{end} 行）"
    if end < total:
        body += f"\n...（后续还有 {total - end} 行，用 start_line={end + 1} 继续读取）"
    # 原文就在磁盘上：超限时只给预览与体量，取回路径是原文件本身（不落副本）
    return limits.clip(f"{header}\n{body}", f"read-{Path(path).name}", source=path)


def search_files(pattern: str, directory: str = ".", glob: str = "*",
                 context: int = 0) -> str:
    """在项目内用正则表达式搜索文本文件（类似 grep）。

    返回 `路径:行号: 行内容` 格式的匹配，默认最多 200 条
    （WOVRA_OUTPUT_LIMIT 可调大）；超出时完整清单落盘并报出总条数，
    可用 read_file 取回（不丢结果）；
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
    # directory 指向文件时**就在该文件里搜**（实测摩擦：想搜一个已知文件里的
    # 函数定义，最自然的写法就是把它填进 directory，却被要求换参数重来一次）。
    single_file: Path | None = None
    if root.is_file():
        single_file = root
        root = root.parent
    if not root.exists():
        return f"directory 不存在: {directory}（解析为 {root}）。先确认目录路径。"
    matches: list[str] = []
    for path in _walk(root, glob):
        if single_file is not None and path.resolve() != single_file.resolve():
            continue  # 指定了单个文件：只搜它
        # 超大文件上限（2026-09-12 由 1MB 放宽到 8MB）：超限落盘的 spill
        # 文件动辄几 MB，原阈值把它挡在搜索之外——而定位恰恰是大输出最
        # 需要的操作。文本 8MB 正则扫描成本可接受，真卡住还有超时兜底。
        if path.stat().st_size > _SEARCH_MAX_BYTES:
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
    if not matches:
        return (
            f"无匹配：pattern={pattern!r}, directory={directory!r}, glob={glob!r}"
            f"{_search_no_match_hint(root, glob, regex)}"
        )
    cap = limits.list_limit(_SEARCH_MAX_MATCHES)
    if len(matches) > cap:
        # 超限不丢结果（2026-09-11 worklog §26 同批）：完整清单落盘可取回，
        # 且明说一共多少条——否则模型不知道"才 200 条"还是"其实有 800 条"。
        saved = limits.spill("\n".join(matches), "search")
        note = (
            f"\n…（共 {len(matches):,} 条匹配，此处只显示前 {cap:,} 条；"
            + (f"完整清单已落盘 {saved}，用 read_file 取回" if saved else "落盘失败")
            + "；也可缩小搜索范围或调大 WOVRA_OUTPUT_LIMIT）"
        )
        return limits.clip("\n".join(matches[:cap]) + note, "search", limit=10 ** 9)
    return limits.clip("\n".join(matches), "search")


def _noise_breakdown(paths: list[Path], root_scope: Path, lead: str):
    """命中里被噪声目录挡掉的部分 → (数量, 目录名, 示例目录)；无则 None。

    lead = pattern/glob 的首段（如 `output/**/*` 的 `output`）：示例优先取与它
    同类的那批，目录名也把它排在前面——按字母序会先蹦出 .venv，指的方向与
    pattern 无关。
    """
    def _from_root(path: Path) -> tuple[str, ...]:
        try:
            return path.relative_to(root_scope).parts
        except ValueError:
            return path.parts

    noise = [p for p in paths if any(part in _IGNORED_DIRS for part in _from_root(p))]
    if not noise:
        return None
    dirs = sorted({part for p in noise for part in _from_root(p) if part in _IGNORED_DIRS})
    ordered = ([lead] if lead in dirs else []) + [d for d in dirs if d != lead]
    topical = [p for p in noise if lead and _from_root(p)[0] == lead]
    if not topical and lead:
        topical = [p for p in noise if lead in _from_root(p)]
    example = _display_rel((topical or noise)[0].parent).as_posix()
    return len(noise), "、".join(ordered[:3]), example


def _lead_segment(pattern: str) -> str:
    """pattern/glob 的第一段（跳过 `**`）——排噪声目录与取示例时的相关方向。"""
    return next((s for s in pattern.split("/") if s and s != "**"), "")


# search_files 零命中时，在噪声目录里探内容命中的预算：真实原因要靠**内容命中**
# 证明，只凭"有一堆文件被跳过"会每次零命中都报（那是新的误导）。探到预算即停。
# 探针只走 Wovra 自己的产物目录：大输出 spill（回取路径）与扩展点。
# .venv/__pycache__/node_modules 是第三方与构建产物，不会是搜索目标；tasks/ 的会话数据
# 不允许被当作搜索去向推荐。探它们只会白烧预算。
_NOISE_PROBE_DIRS = ("output", ".wovra")
_NOISE_PROBE_FILES = 200
_NOISE_PROBE_BYTES = 4_000_000


def _probe_noise_match(root: Path, glob: str, regex: "re.Pattern[str]"):
    """在默认跳过的产物目录里探一次内容命中 → (目录名, 路径:行号)；探不到 None。

    只走产物目录本身（不扫全树）——省掉 .venv 那种上万文件的空转。
    """
    budget = _NOISE_PROBE_BYTES

    def _size(path: Path) -> int:
        try:
            return path.stat().st_size
        except OSError:
            return _SEARCH_MAX_BYTES + 1

    for name in _NOISE_PROBE_DIRS:
        base = root / name
        if not base.is_dir():
            continue
        try:
            candidates = list(_walk(base, glob))
        except (OSError, ValueError):
            continue
        for path in sorted(candidates, key=_size)[:_NOISE_PROBE_FILES]:
            size = _size(path)
            if size > _SEARCH_MAX_BYTES or size > budget:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError, PermissionError):
                continue
            budget -= len(text)
            for line_number, line in enumerate(text.splitlines(), start=1):
                if regex.search(line):
                    return name, f"{_display_rel(path).as_posix()}:{line_number}"
    return None


def _search_no_match_hint(root: Path, glob: str, regex: "re.Pattern[str]") -> str:
    """search_files 零命中时分因：噪声目录内确有命中 / glob 路径过滤。

    旧版只回 `无匹配：pattern=..., directory=..., glob=...`——原因不分，
    模型照着想就容易往错方向改。
    """
    parts: list[str] = []
    probe = _probe_noise_match(root, glob, regex)
    if probe:
        where, example = probe
        parts.append(
            f"pattern 在默认跳过的噪声目录 {where} 内有命中（如 {example}），其内容未被搜索——"
            f"把 directory 指到该目录即照搜"
        )
    if glob not in ("*", "**", "**/*"):
        parts.append(
            f"glob={glob!r} 只搜文件名匹配它的文件；不确定文件类型时可省略 glob（默认 '*'）"
        )
    if not parts:
        return ""
    return "（" + "；".join(parts) + "）"


def _no_match_hint(root: Path, pattern: str, include_hidden: bool) -> str:
    """零命中时分因给提示：噪声目录 / pattern 层级 / 确实没有这种文件。

    原来三种原因压成一句"隐藏文件未计入"——实测据此推出过错误机制
    （`src/*.py` 从仓库根零命中，被当成隐藏文件的锅；真因是 pattern 的
    递归层级：`src/*.py` 意即"任意层级的 src/ 下正好一层"）。
    """
    root_scope = root.resolve()
    try:
        all_hits = list(_walk(root, pattern, skip_noise=False))
    except (OSError, ValueError):
        all_hits = []

    def _visible(paths: list[Path]) -> list[Path]:
        return [p for p in paths if include_hidden or not _is_hidden(_display_rel(p))]

    breakdown = _noise_breakdown(all_hits, root_scope, _lead_segment(pattern))
    if breakdown:
        count, dirs, example = breakdown
        return (
            f"（{count} 个文件在默认跳过的噪声目录 {dirs} 内——"
            f"把 directory 指到该目录即会照搜，如 directory={example!r}）"
        )
    if "/" in pattern and not pattern.startswith("**/"):
        segments = [s for s in pattern.split("/") if s]
        candidates: list[str] = []
        if len(segments) >= 2:
            candidates.append(f"{segments[0]}/**/{segments[-1]}")
        candidates.append(f"**/{pattern}")
        for candidate in dict.fromkeys(candidates):
            try:
                deeper = _visible(list(_walk(root, candidate)))
            except (OSError, ValueError):
                continue
            if deeper:
                return (
                    f"（pattern 是整棵子树匹配，{pattern!r} 要求目录层级完全一致；"
                    f"要取到这一层写 {candidate!r}"
                    f"，如 {_display_rel(deeper[0]).as_posix()}）"
                )
    if not include_hidden:
        return "（隐藏文件未计入，需要时加 include_hidden=True）"
    return ""


def glob_files(pattern: str, directory: str = ".", include_hidden: bool = False) -> str:
    """按文件名通配模式查找文件（如 *.py、docs/**/*.md），返回相对路径。

    模式递归匹配所有子目录（*.py 等价于 **/*.py）；与 search_files
    （搜内容）互补：找"有哪些文件"用本工具，找"哪些文件里有什么
    内容"用 search_files。自动跳过 .git/.venv 等噪声目录，默认最多
    返回 1,000 条（WOVRA_OUTPUT_LIMIT 可调大）；超出时完整清单落盘并报出
    总数，可用 read_file 取回（不丢结果）。

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
        hint = _no_match_hint(root, pattern, include_hidden)
        return f"无匹配文件: {pattern}（directory={directory}）{hint}"
    cap = limits.list_limit(_GLOB_MAX_FILES)
    lines = [_display_rel(p).as_posix() for p in filtered[:cap]]
    if len(filtered) > cap:
        # 超限不丢结果（2026-09-11 worklog §26 同批）：完整清单落盘可取回，
        # 并报出总条数——「一共 3,000 个」和「就这 1,000 个」对判断完全不同。
        saved = limits.spill("\n".join(_display_rel(p).as_posix() for p in filtered), "glob")
        more = (
            f"\n…（共 {len(filtered):,} 个文件，此处只显示前 {cap:,} 个；"
            + (f"完整清单已落盘 {saved}，用 read_file 取回" if saved else "落盘失败")
            + "；也可缩小 pattern 或调大 WOVRA_OUTPUT_LIMIT）"
        )
    else:
        more = ""
    return limits.clip("\n".join(lines) + more, "glob", limit=10 ** 9)

# ---- 文件观察注册表（过期保护） ---------------------------------------------
# 本进程读/写过的文件 → (mtime_ns, size)。edit_file/write_file 前核对：
# 文件在观察后被外部（用户、其他会话、其他进程）改动 → 拒绝执行并要求
# 重新 read_file，防止模型按过期的上下文内容覆盖用户的新修改。
# 本进程从未观察过的文件无从判断，保持原行为（write_file 有完整留底）。

# 键是 **(agent, path)**：V4 分流后同一进程里多个 agent 各自观察（进程级全局那份会让
# B 的写入刷新 A 的记录 → A 的过期写入检测漏报，2026-09-17 修）。
_file_registry: dict[tuple[str, Path], tuple[int, int]] = {}


def _observe_file(path: Path, content: str | None = None) -> None:
    """记下这次读到/写到的状态；给了 content 就同时存一份内容快照。

    内容快照让"文件被改过"的**差异**可算（只说"被改过"的话，模型只能整读
    一遍才知道变了什么）。与 agent 层的观察是同一份存储（内容寻址，重复无害）。
    """
    key = (safety.current_agent(), path)
    try:
        st = path.stat()
        _file_registry[key] = (st.st_mtime_ns, st.st_size)
    except OSError:
        _file_registry.pop(key, None)
    if content is None:
        return
    try:
        workspace = Path(safety.workspace_root())
        rel = path.relative_to(workspace).as_posix()
    except (ValueError, OSError):
        return
    observed.record(workspace, safety.current_agent(), rel, content, by="read")


def _stale_detail(path: Path, now: str | None = None) -> str:
    """「变了哪几行」——相对本 agent 上次看到的内容（拿不到就返回空串）。

    过期拒绝只说"被改过"是不够的（实测：每次都得把文件整读一遍才知道变了什么，
    大文件尤其贵）。观察快照里存着上次看到的内容，直接给出 hunk。
    """
    if now is None:
        try:
            now = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            return ""
    try:
        workspace = Path(safety.workspace_root())
        rel = path.relative_to(workspace).as_posix()
        report = observed.stale_report(workspace, safety.current_agent(), rel, now)
    except (ValueError, OSError):
        return ""
    return f"\n{report}\n" if report else ""


def _stale_error(path: Path, now: str | None = None) -> str | None:
    """文件在观察后被外部修改 → 返回拒绝原因；否则 None（**按当前 agent 的记录判**）。"""
    observed_state = _file_registry.get((safety.current_agent(), path))
    if observed_state is None:
        return None
    try:
        st = path.stat()
    except OSError:
        return None
    if (st.st_mtime_ns, st.st_size) != observed_state:
        return (
            f"文件在你上次读取后已被外部修改（用户、别的会话或别的进程）：{path}。"
            + _stale_detail(path, now)
            + "上下文里的内容可能已过期——按上面的差异对齐锚点/行号即可继续，"
              "或先 read_file 重新确认最新内容。"
        )
    return None

# ---- 文件版本档案（checkpoint：无 git 环境的后悔药） ------------------------
# 每次覆盖/编辑/按行替换之前，旧内容自动归档到 .wovra/history/<相对路径>/
# （路径中的 / 替换为 __）。每文件保留最近 _HISTORY_KEEP 份，超出淘汰最旧；
# restore_file 列出并回滚（回滚前当前内容也归档——回滚本身可再回滚）。

_HISTORY_KEEP = 10


def _history_slot(target: Path) -> Path | None:
    """文件 → 它的版本档案目录（从有效工作区现算，测试可重定向）。

    授权目录外的文件（界外链接目标）不属于工作区版本档案：返回 None
    （_archive_version 会跳过归档，写入照常进行）。
    """
    try:
        rel = target.relative_to(safety.workspace_root()).as_posix()
    except ValueError:
        return None
    return safety.workspace_root() / ".wovra" / "history" / rel.replace("/", "__")


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
    # 参数误用预检（2026-09-13 摩擦修复）：目标是目录 → 明确提示而不是落到
    # "没有历史版本"（误导——目录本不该是 restore 对象）。注意：对**不存在**
    # 的文件不能加预检——已删除文件是合法的找回对象（delete_file 删除前
    # 归档，restore_file 凭历史版本恢复），提示"没有历史版本"是正确的。
    if target.is_dir():
        return (
            f"路径是目录（而非文件），restore_file 只回滚文件的历史版本：{path}。"
            f"要列出该目录内容请用 list_files('{path}')。"
        )
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
    _observe_file(target, matches[0].read_text(encoding="utf-8"))
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
    denied = safety.readonly_write_denial(target, shown=path)   # 禁写区：不可授权
    if denied:
        safety._audit(f"[delete_file][禁写区] {path}")
        return denied
    denied = permissions.check("delete", path)
    if denied:
        safety._audit(f"[delete_file][权限拒绝] {path}")
        return denied
    if target.is_symlink():
        link_to = os.readlink(target)
        dangling = not target.exists()
        _archive_version(target) if not dangling else None
        target.unlink()  # unlink 作用于链接名本身，不触碰目标
        _file_registry.pop((safety.current_agent(), target), None)
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
    _file_registry.pop((safety.current_agent(), target), None)
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
    for probe, label in ((src, path), (dst, new_path)):
        denied_ro = safety.readonly_write_denial(probe, shown=label)  # 禁写区：源与目标都算
        if denied_ro:
            safety._audit(f"[move_file][禁写区] {label}")
            return denied_ro
    denied = permissions.check("move", path)
    if denied:
        safety._audit(f"[move_file][权限拒绝] {path}")
        return denied
    denied_dst = permissions.check("write", new_path)
    if denied_dst and dst.exists():
        safety._audit(f"[move_file][权限拒绝·目标] {new_path}")
        return denied_dst
    if dst.is_symlink() or dst.exists():
        # 目标已存在且是目录：用户本意多半是"移进该目录"——给路，别只让删/换名。
        # （2026-09-13 摩擦修复：实测 move_file('f.txt', 'bdir') 只回"目标已存在"）
        if dst.is_dir() and not dst.is_symlink():
            hint = str(src.name) if src.name else path.split("/")[-1]
            return (
                f"目标是一个已存在的目录：{new_path}（解析为 {dst}）。"
                f"move_file 不覆盖也不并入目录——要把 {path} 移进该目录，"
                f"请把 new_path 写为目标内的完整路径（如 {new_path}/{hint}）。"
            )
        return f"目标已存在: {new_path}（解析为 {dst}）——move 不覆盖，先删除或换名"
    if not src.is_symlink() and not src.exists():
        return f"文件不存在: {path}（解析为 {src}）"
    if src.is_symlink() and not src.exists():
        # 悬空链接：可以移动（移动的是链接名），但要说清
        dst.parent.mkdir(parents=True, exist_ok=True)
        src.rename(dst)
        _file_registry.pop((safety.current_agent(), src), None)
        safety._audit(f"[move_file] {path} → {new_path}（悬空链接）")
        # 同样避开"不存在"：移动悬空链接是成功的（见 delete_file 注释）
        return f"已移动悬空符号链接 {path} → {new_path}（该链接原本已失效）"
    dst.parent.mkdir(parents=True, exist_ok=True)
    src.rename(dst)
    _file_registry.pop((safety.current_agent(), src), None)
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
    denied = permissions.check("write", path)
    if denied:
        safety._audit(f"[write_file][权限拒绝] {path}")
        return denied
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
    _observe_file(target, content)
    action = "覆盖" if existed else "创建"
    if not existed:
        # F5：新文件谁创建谁拥有——创建成功即归属当前视图（运行时守卫负责落册）
        permissions.claim("write", path)
    if old is not None:
        # 旧内容完整留底（审计原则：能还原）；超大文件截断到 20000 字符
        backup = old if len(old) <= 20_000 else old[:20_000] + "\n...(已截断)"
        safety._audit(f"[write_file 旧内容备份] {path}:\n{backup}")
    diff = _change_diff(old, content) if old is not None else ""
    tail = f"\n改动：\n{diff}" if diff else ""
    return f"已{action} {path}（{len(content)} 字符）{tail}"


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


def _change_diff(before: str, after: str, max_lines: int = 20) -> str:
    """改动前后的最小 diff——**成功**写的回执里带上它（2026-09-13 用户口径）。

    为什么（`agent-test/block-detail-organization-sample.md:166` 记的老痛点）：
    改完文件后模型只能靠 `read_file` 重读、或 `git diff` 核对，而
    `git diff` 对**未跟踪的新文件**什么都不显示——"我到底改成了什么"
    在这条路上是无法自证的。把 diff 直接放进结果：确认改动只需看回执，
    省掉一整次 read_file 往返（也省了那一份文件的 token）。

    只回**变动行**（隔离上下文 0），超长截断并给行数——回执不是重读。
    """
    import difflib

    diff = [line for line in difflib.unified_diff(
        before.splitlines(), after.splitlines(),
        fromfile="改动前", tofile="改动后", lineterm="", n=1,
    )][2:]  # 去掉 ---/+++ 两行头，省 token（路径已在回执正文里）
    if not diff:
        return ""
    if len(diff) > max_lines:
        diff = diff[:max_lines] + [f"…（改动共 {len(diff)} 行差异，只显示前 {max_lines} 行）"]
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
    denied = permissions.check("edit", path)
    if denied:
        safety._audit(f"[edit_file][权限拒绝] {path}")
        return denied
    stale = _stale_error(target)
    # 锚点仍然唯一时**不必白拒一次**（实测摩擦：明明只差一处引用，却被要求整读
    # 一遍）。文件被改过但 old_text 在**当前磁盘内容**里恰好命中 → 放行。
    pass_stale = False
    if stale:
        try:
            latest = target.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            latest = None
        hits = latest.count(old_text) if latest is not None else 0
        if hits == 1 or (replace_all and hits >= 1):
            pass_stale = True
        else:
            return stale
    # 参数误用预检（2026-09-13 摩擦修复）：目标是目录 → 明确提示而不是裸 IsADirectoryError。
    # 文案保留 "是目录" 子串：lifecycle/blocks 靠它判断编辑是否真发生（同 read/write 预检约定）。
    if target.is_dir():
        return (
            f"路径是目录（而非文件），不能编辑：{path}。edit_file 只编辑文件；"
            f"要列出该目录内容请用 list_files('{path}')，"
            f"要新建其中的文件请用 write_file。"
        )
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
    _observe_file(target, replaced)
    scope = f"全部 {n} 处" if replace_all else "唯一一处"
    # 替换片段对完整留底：改了哪段、改成了什么，一目了然
    safety._audit(f"[edit_file] {path}（替换{scope}）\n定位片段:\n{old_text}\n替换为:\n{new_text}")
    anchor = _persistent_anchor(text, line_no)
    diff = _change_diff(text, replaced)
    tail = f"\n改动：\n{diff}" if diff else "\n（改动后内容与原来一致）"
    stale_note = ("\n（文件在你上次读取后被改过：已按当前磁盘内容替换；"
                  "差异见上面的「改动」）" if pass_stale else "")
    return (f"已修改 {path}（替换{scope}，{len(old_text)} 字符 → {len(new_text)} 字符，"
            f"位于第 {line_no} 行附近{anchor}）{tail}{stale_note}")


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
    denied = permissions.check("edit", path)
    if denied:
        safety._audit(f"[replace_lines][权限拒绝] {path}")
        return denied
    stale = _stale_error(target)
    if stale:
        return stale
    # 参数误用预检（2026-09-13 摩擦修复）：目标是目录 → 明确提示而不是裸 IsADirectoryError。
    # 文案保留 "是目录" 子串：lifecycle/blocks 靠它判断替换是否真发生。
    if target.is_dir():
        return (
            f"路径是目录（而非文件），不能按行替换：{path}。replace_lines 只编辑文件；"
            f"要列出该目录内容请用 list_files('{path}')，"
            f"要新建其中的文件请用 write_file。"
        )
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
    _observe_file(target, "\n".join(replaced) + ("\n" if trailing else ""))
    # 旧块与新块都留底（超长截断到 5000，原则与 write_file 备份一致）
    safety._audit(f"[replace_lines] {path} 第 {start_line}-{end_line} 行\n旧内容:\n"
           f"{old_block[:5000]}\n替换为:\n{new_content[:5000]}")
    # **被删掉的内容要摆到显眼处**（实测损伤：区间里几行不该删的常量被一起吃掉，
    # 回执只说"N 行 → M 行"，靠事后 `git diff` 才发现）。判据用 difflib 的
    # `delete` 块——那才是"原文里没有、新内容里也没有"的行；`replace`（改写）
    # 不算丢失，否则每次改一行都会报一遍。
    import difflib

    old_lines = old_block.split("\n")
    deleted = [old_lines[i] for tag, i1, i2, _j1, _j2
               in difflib.SequenceMatcher(None, old_lines, new_lines).get_opcodes()
               if tag == "delete" for i in range(i1, i2) if old_lines[i].strip()]
    warn = ""
    if deleted:
        shown = "\n".join(f"  − {ln}" for ln in deleted[:20])
        more = f"\n  …（共 {len(deleted)} 行）" if len(deleted) > 20 else ""
        warn = (f"\n⚠ 这个区间里有 {len(deleted)} 行非空内容没出现在 new_content 里"
                f"（若不是本意，用 restore_file 回滚）：\n{shown}{more}")
    return (f"已替换 {path} 第 {start_line}-{end_line} 行"
            f"（{end_line - start_line + 1} 行 → {len(new_lines)} 行，现共 {len(replaced)} 行）"
            f"\n改动：\n{_change_diff(old_block, new_content)}{warn}")
