"""仓库根 `.env` 的按行读写：改值不改注释与无关行，原子落盘。

配置页（`wovra serve` 的"配置"面板 → `POST /api/settings`）靠这里落盘；
CLI 与 serve 共用同一份文件（`load_dotenv` 在 `wovra/__init__.py` 导入期加载）。

口径：
* 逐行编辑——命中的键就地改值，**其它行逐字节保留**（注释、空行、顺序都不动）；
* 文件里没有的键追加到末尾（前面补一行标记注释，只补一次）；
* 同一个键出现多次时**全部改写为新值**（dotenv 是后者覆盖前者，全改才不会被旧值压回）；
* 落盘走同目录临时文件 + `os.replace`（原子）；权限沿用原文件，新建取 600。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

# `KEY=值`；容忍 `export ` 前缀与键前后空白
_KEY_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")
_APPEND_MARK = "# —— 以下由前端配置页写入 ——"


def env_path() -> Path:
    """`.env` 位置：`WOVRA_ENV_FILE` 优先，否则仓库根（本文件上溯三级）。"""
    override = (os.environ.get("WOVRA_ENV_FILE") or "").strip()
    if override:
        return Path(override).expanduser()
    return Path(__file__).resolve().parent.parent.parent / ".env"


def _unquote(raw: str) -> str:
    s = raw.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        body = s[1:-1]
        if s[0] == '"':
            body = body.replace('\\"', '"').replace("\\\\", "\\")
        return body
    # 未加引号：` #` 之后算注释（dotenv 同款），行尾空白丢掉
    i = s.find(" #")
    if i >= 0:
        s = s[:i]
    return s.rstrip()


def _quote(value: str) -> str:
    """含空白/引号/`#` 的值加双引号并转义，其余原样写。"""
    if value == "":
        return ""
    if re.search(r"[\s#'\"]", value):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return value


def load_pairs(path: Path | None = None) -> dict[str, str]:
    """文件里当前生效的键值（同键后者覆盖前者）。文件不存在返回空表。"""
    p = path or env_path()
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return {}
    out: dict[str, str] = {}
    for line in text.splitlines():
        m = _KEY_RE.match(line)
        if m:
            out[m.group(1)] = _unquote(m.group(2))
    return out


def set_values(updates: dict[str, str], path: Path | None = None) -> list[str]:
    """把 `updates` 写进文件（值 `""` = 清空该键），返回真正改到的键。"""
    if not updates:
        return []
    p = path or env_path()
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        text = ""
    eol = "\r\n" if "\r\n" in text else "\n"
    out: list[str] = []
    hit: set[str] = set()
    for line in text.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        tail = line[len(body):] or eol
        m = _KEY_RE.match(body)
        if m and m.group(1) in updates:
            key = m.group(1)
            hit.add(key)
            out.append(f"{body[:m.start(1)]}{key}={_quote(updates[key])}{tail}")
        else:
            out.append(line)
    missing = [k for k in updates if k not in hit]
    if missing:
        if out and not out[-1].endswith(("\n", "\r")):
            out[-1] = out[-1] + eol
        if not any(l.strip() == _APPEND_MARK for l in out):
            out.append(_APPEND_MARK + eol)
        out.extend(f"{k}={_quote(updates[k])}{eol}" for k in missing)
    data = "".join(out).encode("utf-8")
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode = p.stat().st_mode & 0o777
    except OSError:
        mode = 0o600
    tmp = p.with_name(p.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    os.replace(tmp, p)
    try:
        os.chmod(p, mode)
    except OSError:
        pass
    return sorted(hit | set(missing))
