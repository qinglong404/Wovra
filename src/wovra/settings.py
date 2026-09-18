"""运行参数的单一登记表 + 落盘/生效逻辑（前端"配置"面板的数据源）。

两件事：
* `describe()` —— 给前端的分组参数表：当前值（密钥只回掩码）、类型、默认值、
  **生效档位**（`live` = 写完当场生效 / `restart` = 重启 `wovra serve` 才生效）、
  以及"文件里已改、进程还没吃上"的重启待办；
* `apply()` —— 校验 → 写 `.env`（`envfile.set_values`）→ 立即档当场写进
  `os.environ`（调用期读环境的那些参数因此**下一步/下一轮**就用新值）。

档位判据：**运行时在调用期读 `os.environ`** 的为 `live`（工具限额、超时、
模型与密钥、开关…）；**只在 import / 进程启动期读一次**的为 `restart`
（数据目录、工作区、配色这类）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from . import envfile

LIVE = "live"
RESTART = "restart"

GROUPS: list[tuple[str, str]] = [
    ("model", "模型与密钥"),
    ("search", "检索与抓取"),
    ("ctx", "上下文与水位"),
    ("maint", "维护与结算"),
    ("tools", "工具与输出"),
    ("misc", "服务与环境"),
]


@dataclass(frozen=True)
class Spec:
    key: str
    label: str
    group: str
    kind: str = "str"          # str | int | float | bool | secret
    default: str = ""
    scope: str = LIVE
    hint: str = ""
    lo: float | None = None
    hi: float | None = None
    choices: tuple[str, ...] = field(default_factory=tuple)


SPECS: tuple[Spec, ...] = (
    # ---- 模型与密钥 ----
    Spec("Wovra_API_KEY", "API 密钥", "model", "secret", "",
         hint="模型服务商的密钥（写进 .env，页面只回掩码）"),
    Spec("Wovra_BASE_URL", "服务地址", "model", "str", "",
         hint="OpenAI 协议兼容端点，如 https://api.deepseek.com/v1；留空=官方地址"),
    Spec("Wovra_MODEL", "模型", "model", "str", "gpt-4o-mini"),
    Spec("WOVRA_MAX_TOKENS", "单次最大输出 tok", "model", "int", "393216",
         lo=1, hi=4000000),
    Spec("WOVRA_READ_TIMEOUT", "读超时（秒）", "model", "float", "180", lo=1, hi=3600),
    Spec("WOVRA_TOKENIZER", "分词器", "model", "str", "",
         hint="留空=用 tiktoken；heuristic=离线启发式估算"),
    Spec("WOVRA_CACHE_RATE", "缓存折扣（1/N）", "model", "float", "50", lo=1, hi=1000,
         hint="缓存命中 tok 的计价折扣，影响成本模型"),
    # ---- 检索与抓取 ----
    Spec("Wovra_SEARCH_PROVIDER", "首选检索服务", "search", "str", "",
         hint="tavily / serper / exa / bocha / serpapi / firecrawl；留空=随机挑一家"),
    Spec("Wovra_Tavily", "Tavily 密钥", "search", "secret", ""),
    Spec("Wovra_Serper", "Serper 密钥", "search", "secret", ""),
    Spec("Wovra_Exa", "Exa 密钥", "search", "secret", ""),
    Spec("Wovra_Bocha", "博查 密钥", "search", "secret", ""),
    Spec("Wovra_SerpAPI", "SerpAPI 密钥", "search", "secret", ""),
    Spec("Wovra_Firecrawl", "Firecrawl 密钥", "search", "secret", ""),
    Spec("Wovra_TinyFish", "TinyFish 密钥", "search", "secret", ""),
    Spec("WOVRA_SEARCH_TIMEOUT", "检索超时（秒）", "search", "int", "30", lo=1, hi=600),
    Spec("WOVRA_LOCAL_SEARCH_TIMEOUT", "本地兜底超时（秒）", "search", "int", "5",
         lo=1, hi=600),
    Spec("WOVRA_FIRECRAWL_MIN_CHARS", "抓取最小字符数", "search", "int", "200",
         lo=0, hi=1000000),
    Spec("WOVRA_WEB_CACHE_TTL", "抓取缓存（秒）", "search", "int", "3600", lo=0,
         hi=86400),
    Spec("WOVRA_AGENT_TIMEOUT", "web_automate 超时（秒）", "search", "int", "60",
         lo=1, hi=3600),
    # ---- 上下文与水位 ----
    Spec("WOVRA_CONTEXT_LIMIT", "模型上下文窗口 tok", "ctx", "int", "1000000",
         lo=1000, hi=100000000, hint="顶栏窗口占比的分母；下一轮生效"),
    Spec("WOVRA_ORG_WATERMARK", "水位 tok", "ctx", "int", "100000",
         lo=1000, hi=100000000, hint="到线触发结算/分裂/折档；下一轮生效"),
    Spec("WOVRA_FOLD_TARGET", "折档目标（水位比例）", "ctx", "float", "0.6",
         lo=0.05, hi=1.0, hint="折到水位这个比例之下，一次折够"),
    Spec("WOVRA_COMPRESS_THRESHOLD", "窗口保底阈值", "ctx", "float", "0.8",
         lo=0.05, hi=1.0),
    Spec("WOVRA_MAX_TURNS", "单轮最大步数", "ctx", "int", "200", lo=1, hi=10000),
    Spec("WOVRA_ROUTE_HOPS", "转交跳数上限", "ctx", "int", "3", lo=0, hi=50),
    Spec("WOVRA_STATE_BUDGET", "任务态渲染预算（字符）", "ctx", "int", "8000",
         lo=100, hi=1000000),
    Spec("WOVRA_ORG_GRACE_ROUNDS", "维护宽限轮数", "ctx", "int", "3", lo=0, hi=1000),
    Spec("WOVRA_ORG_COOLDOWN_ROUNDS", "维护冷却轮数", "ctx", "int", "3", lo=0, hi=1000),
    # ---- 维护与结算 ----
    Spec("WOVRA_ROUND_NOTE", "每轮一段话（结算）", "maint", "bool", "1",
         hint="1=开；关掉可省这次调用"),
    Spec("WOVRA_NOTE_TIMEOUT", "结算超时（秒）", "maint", "float", "180",
         lo=1, hi=3600),
    Spec("WOVRA_NOTE_BATCH_MAX", "单批结算轮数", "maint", "int", "12", lo=1, hi=200),
    Spec("WOVRA_MAINT_TIMEOUT", "维护硬上限（秒）", "maint", "float", "900",
         lo=1, hi=7200),
    Spec("WOVRA_V4", "V4 上下文模型", "maint", "bool", "1",
         hint="0=退回旧链路（按域重组 + 整理演进）"),
    Spec("WOVRA_MAINT_NARROW_TOOLS", "维护收窄工具集", "maint", "bool", "0",
         hint="1=维护调用只带出口工具（会打散前缀缓存，默认关）"),
    # ---- 工具与输出 ----
    Spec("WOVRA_OUTPUT_LIMIT", "工具输出上限（字符）", "tools", "int", "200000",
         lo=1000, hi=20000000),
    Spec("WOVRA_PREVIEW_CHARS", "超限预览字符数", "tools", "int", "2000",
         lo=0, hi=200000),
    Spec("WOVRA_IMAGE_MAX_SIDE", "图片边长上限（px）", "tools", "int", "3000",
         lo=100, hi=100000),
    Spec("WOVRA_IMAGE_HARD_MAX_SIDE", "图片硬上限（px）", "tools", "int", "8192",
         lo=100, hi=200000),
    Spec("WOVRA_IMAGE_VIEWS", "看图软线（次/回合）", "tools", "int", "6", lo=1, hi=1000),
    Spec("WOVRA_IMAGE_VIEWS_MAX", "看图硬线（次/回合）", "tools", "int", "12",
         lo=1, hi=1000),
    Spec("WOVRA_BROWSER", "浏览器可执行文件", "tools", "str", "",
         hint="截图用；留空=按常见安装路径找"),
    Spec("WOVRA_READONLY_DIRS", "只读目录清单", "tools", "str", "",
         hint=f"{os.pathsep} 分隔（Windows 上是 ;）"),
    # ---- 服务与环境（多为重启档） ----
    Spec("WOVRA_ASK_TIMEOUT", "问用户超时（秒）", "misc", "int", "1800", lo=10,
         hi=86400),
    Spec("WOVRA_TASKS_ROOT", "会话数据目录", "misc", "str", "", scope=RESTART,
         hint="改了等于换一份数据目录，需重启服务"),
    Spec("WOVRA_WORKSPACE", "默认工作区", "misc", "str", "", scope=RESTART,
         hint="新建会话的默认根目录，需重启服务"),
    Spec("WOVRA_COLOR", "彩色输出", "misc", "bool", "0", scope=RESTART,
         hint="CLI 管道里强制保留颜色"),
)

SPEC_BY_KEY: dict[str, Spec] = {s.key: s for s in SPECS}


def _mask(value: str) -> str:
    """密钥只回尾部几位，够辨认是哪一个，又不成明文。"""
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return "*" * 6 + value[-4:]


def _norm_bool(value: str) -> str:
    return "0" if (value or "").strip().lower() in ("0", "false", "no", "off", "") else "1"


def _current(spec: Spec, file_pairs: dict[str, str]) -> tuple[str, str]:
    """(现值, 来源)。来源：env = 运行中进程生效值；file = .env 里改过还没吃上。"""
    live = os.environ.get(spec.key)
    if live is not None and live != "":
        return live, "env"
    in_file = file_pairs.get(spec.key)
    if in_file not in (None, ""):
        return in_file, "file"
    if live is not None:
        return live, "env"
    return spec.default, "default"


def describe() -> dict[str, Any]:
    """给前端的全量参数表（按组分块）+ 重启待办 + 文件位置。"""
    path = envfile.env_path()
    file_pairs = envfile.load_pairs(path)
    rows: list[dict[str, Any]] = []
    pending: list[str] = []
    for spec in SPECS:
        value, source = _current(spec, file_pairs)
        if source == "file":
            pending.append(spec.key)
        row: dict[str, Any] = {
            "key": spec.key, "label": spec.label, "group": spec.group,
            "kind": spec.kind, "scope": spec.scope, "hint": spec.hint,
            "default": spec.default, "source": source,
            "choices": list(spec.choices),
        }
        if spec.kind == "secret":
            row["value"] = ""
            row["masked"] = _mask(value)
            row["set"] = bool(value)
        else:
            row["value"] = value
            row["masked"] = ""
            row["set"] = value != ""
        if spec.lo is not None:
            row["lo"] = spec.lo
        if spec.hi is not None:
            row["hi"] = spec.hi
        rows.append(row)
    return {
        "groups": [{"id": gid, "title": title} for gid, title in GROUPS],
        "items": rows,
        "env_file": str(path),
        "pending_restart": pending,
        "scopes": {
            LIVE: "立即生效（写入 .env 并同步到运行中进程）",
            RESTART: "重启后生效（进程启动期读取一次）",
        },
    }


def _validate(spec: Spec, raw: str) -> tuple[str | None, str | None]:
    """(规范化后的值, 错误)。空串一律表示"清空 = 用默认值"。"""
    value = (raw or "").strip()
    if value == "":
        return "", None
    if spec.kind == "bool":
        return _norm_bool(value), None
    if spec.kind == "int":
        try:
            n = int(value)
        except ValueError:
            return None, "要整数"
        if spec.lo is not None and n < spec.lo:
            return None, f"不小于 {int(spec.lo)}"
        if spec.hi is not None and n > spec.hi:
            return None, f"不大于 {int(spec.hi)}"
        return str(n), None
    if spec.kind == "float":
        try:
            n = float(value)
        except ValueError:
            return None, "要数字"
        if spec.lo is not None and n < spec.lo:
            return None, f"不小于 {spec.lo}"
        if spec.hi is not None and n > spec.hi:
            return None, f"不大于 {spec.hi}"
        return str(n), None
    return value, None


def apply(values: dict[str, str]) -> dict[str, Any]:
    """校验 → 写盘 → 立即档同步进 `os.environ`。返回保存/待重启/错误三张表。"""
    errors: dict[str, str] = {}
    clean: dict[str, str] = {}
    for key, raw in (values or {}).items():
        spec = SPEC_BY_KEY.get(key)
        if spec is None:
            errors[key] = "未知参数"
            continue
        if spec.kind == "secret" and (raw or "") == "":
            continue                      # 密钥留空 = 不改（清空走清空按钮的显式标记）
        if spec.kind == "secret" and (raw or "") == "\x00":
            clean[key] = ""               # 显式清空
            continue
        norm, err = _validate(spec, str(raw))
        if err:
            errors[key] = err
            continue
        clean[key] = norm
    saved: list[str] = []
    pending: list[str] = []
    if clean:
        saved = envfile.set_values(clean)
        for key, value in clean.items():
            spec = SPEC_BY_KEY[key]
            if value == "":
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
            if spec.scope == RESTART:
                pending.append(key)
    return {"ok": not errors, "saved": saved, "errors": errors,
            "pending_restart": sorted(pending),
            "env_file": str(envfile.env_path())}
