"""模型渠道商清单（仓库根 `providers.json`）：多份端点/密钥/模型，可切换。

一份渠道商 = 一个 OpenAI 协议端点 ＋ 它的密钥 ＋ 它可用的模型名清单。
`.env` 里的 `Wovra_BASE_URL`/`Wovra_API_KEY`/`Wovra_MODEL` 仍然有效，作为
**没有渠道商清单时**的退化路径（首次打开配置页会照着它生成一份，免得用户
从零填）。

密钥不进版本库：`providers.json` 与 `.env` 同款（`.gitignore` 里排除），
仓库里只留 `providers.example.json` 模板。前端一律只拿掩码。

文件位置：`WOVRA_PROVIDERS_FILE` 可覆盖（测试/多环境），默认仓库根。
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

# 思考强度的三档（与前端下拉一一对应）：关 / 自动 / 高
REASONING_LEVELS = ("off", "auto", "high")


def providers_path() -> Path:
    """`providers.json` 位置：`WOVRA_PROVIDERS_FILE` 优先，否则仓库根。"""
    override = (os.environ.get("WOVRA_PROVIDERS_FILE") or "").strip()
    if override:
        return Path(override).expanduser()
    return Path(__file__).resolve().parent.parent.parent / "providers.json"


def _mask(value: str) -> str:
    """密钥只回尾部几位（够辨认是哪一个，又不成明文）。"""
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return "*" * 6 + value[-4:]


def _slug(name: str, taken: set[str]) -> str:
    """名字 → 唯一 id（拉丁化退化为序号，中文名也拿得到稳定 id）。"""
    base = re.sub(r"[^A-Za-z0-9_-]+", "-", str(name or "")).strip("-").lower()
    base = base or "provider"
    if base not in taken:
        return base
    i = 2
    while f"{base}-{i}" in taken:
        i += 1
    return f"{base}-{i}"


def _norm(raw: Any) -> dict:
    """一条渠道商记录 → 规范化（缺字段补空，多字段丢弃）。"""
    if not isinstance(raw, dict):
        return {}
    models = raw.get("models")
    if isinstance(models, str):
        models = [m.strip() for m in re.split(r"[,\s]+", models) if m.strip()]
    return {
        "id": str(raw.get("id") or "").strip(),
        "name": str(raw.get("name") or "").strip(),
        "base_url": str(raw.get("base_url") or "").strip().rstrip("/"),
        "api_key": str(raw.get("api_key") or "").strip(),
        "models": [str(m).strip() for m in (models or []) if str(m).strip()],
        # 思考强度用哪家的字段发（auto = 按端点猜，见 llm.reasoning_body）
        "reasoning_field": str(raw.get("reasoning_field") or "auto").strip(),
    }


def load() -> dict:
    """读清单 → `{"current": id, "providers": [...]}`。

    文件不存在时用 `env_derived()` 兜底（照 `.env` 推导，不写盘）——这样
    「首次打开就能看到已有配置」对**所有**读取方一致（面板、检测、会话解析），
    而不是只有 `describe()` 看得见。首次改动（`upsert`/`remove`/`set_current`）
    经由此读到的就是这份推导内容，落盘时自然把它一并写进去。
    """
    path = providers_path()
    if not path.exists():
        return env_derived()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    items = [_norm(p) for p in (data.get("providers") or [])]
    items = [p for p in items if p.get("id")]
    current = str(data.get("current") or "").strip()
    if current not in {p["id"] for p in items}:
        current = items[0]["id"] if items else ""
    return {"current": current, "providers": items}


def save(data: dict) -> None:
    """原子落盘（同目录临时文件 + `os.replace`，权限 600——里面有密钥）。"""
    path = providers_path()
    items = [_norm(p) for p in (data.get("providers") or [])]
    items = [p for p in items if p.get("id")]
    ids = {p["id"] for p in items}
    current = str(data.get("current") or "").strip()
    if current not in ids:
        current = items[0]["id"] if items else ""
    body = json.dumps({"current": current, "providers": items},
                      ensure_ascii=False, indent=2) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, body.encode("utf-8"))
    finally:
        os.close(fd)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def env_derived() -> dict:
    """清单文件不存在时，照着 `.env` 的三个键**推导**一份（不落盘）。

    `Wovra_BASE_URL` / `Wovra_API_KEY` / `Wovra_MODEL` → 一条渠道商，
    名字取端点域名（`api.deepseek.com` → `deepseek`）。这样首次打开配置页
    就能看到自己已有的配置（而不是一片空白要从零填）；真正的落盘发生在
    用户保存某一条时（`upsert`）。
    """
    key = (os.environ.get("Wovra_API_KEY") or "").strip()
    base = (os.environ.get("Wovra_BASE_URL") or "").strip().rstrip("/")
    model = (os.environ.get("Wovra_MODEL") or "").strip()
    if not (key or base):
        return {"current": "", "providers": []}
    host = re.sub(r"^https?://", "", base).split("/")[0] or "env"
    name = host.split(".")[1] if host.count(".") >= 2 else host.split(".")[0]
    pid = _slug(name or "env", set())
    return {"current": pid, "providers": [{
        "id": pid, "name": name or "默认渠道商", "base_url": base,
        "api_key": key, "models": [model] if model else [],
    }]}


def describe() -> dict:
    """给前端的清单：密钥只回掩码，附"缺什么"的提示。

    文件不存在时清单来自 `load()` 的环境推导（**不写盘**——GET 不该有副作用），
    并把 `from_env` 标出来让前端说明这份是从哪来的。
    """
    from_env = not providers_path().exists()
    data = load()
    out = []
    for p in data["providers"]:
        item = dict(p)
        item["api_key"] = ""
        item["masked"] = _mask(p.get("api_key") or "")
        item["set"] = bool(p.get("api_key"))
        item["missing"] = [k for k in ("base_url", "api_key", "models")
                           if not p.get(k)]
        out.append(item)
    return {
        "current": data["current"],
        "providers": out,
        "file": str(providers_path()),
        "levels": list(REASONING_LEVELS),
        "from_env": from_env,
    }


def get(provider_id: str) -> dict | None:
    """按 id 取一条（含明文密钥，仅供后端内部用）。"""
    want = str(provider_id or "").strip()
    for p in load()["providers"]:
        if p["id"] == want:
            return p
    return None


def current() -> dict | None:
    """当前渠道商（含明文密钥）；清单空则 None。"""
    return get(load()["current"])


def upsert(entry: dict) -> dict:
    """新增或按 id 更新一条；`api_key` 留空沿用旧值（前端不回明文）。"""
    incoming = _norm(entry)
    data = load()
    items = data["providers"]
    ids = {p["id"] for p in items}
    name = incoming.get("name") or incoming.get("base_url") or "渠道商"
    if not incoming.get("id"):
        incoming["id"] = _slug(name, ids)
    for i, old in enumerate(items):
        if old["id"] != incoming["id"]:
            continue
        if not incoming.get("api_key"):
            incoming["api_key"] = old.get("api_key") or ""
        if not incoming.get("models"):
            incoming["models"] = old.get("models") or []
        items[i] = incoming
        break
    else:
        items.append(incoming)
    if entry.get("current"):
        data["current"] = incoming["id"]
    save(data)
    return load()


def remove(provider_id: str) -> dict:
    """删一条；它是当前项时，当前切到剩下的第一条。"""
    data = load()
    data["providers"] = [p for p in data["providers"] if p["id"] != provider_id]
    save(data)
    return load()


def set_current(provider_id: str) -> dict:
    """钉当前渠道商（不存在则不动）。"""
    data = load()
    if any(p["id"] == provider_id for p in data["providers"]):
        data["current"] = provider_id
        save(data)
    return load()


# ---- 检测（2026-09-18 用户口径："同时还有检测功能"）--------------------------
# 两件事：**拉模型**（GET /models，填下拉）与**测试**（发一次最小请求，报延迟与
# 错误原文）。都用 httpx/urllib 直连，不经过 LLM 封装——检测要如实报出端点的
# 原始反应，而 LLM 那层会把配置类错误翻译成"请检查 .env"。

def _probe_target(entry: dict | None, provider_id: str = "") -> dict:
    """检测目标：显式传入的那条 → 指定的渠道商 → 当前渠道商 → `.env` 的三个键。"""
    item = entry or get(provider_id) or current()
    if item:
        return {"id": item.get("id") or "", "name": item.get("name") or "",
                "base_url": item.get("base_url") or "",
                "api_key": item.get("api_key") or "",
                "models": list(item.get("models") or [])}
    return {"id": "", "name": "（来自 .env）", "base_url":
            (os.environ.get("Wovra_BASE_URL") or "").strip(),
            "api_key": (os.environ.get("Wovra_API_KEY") or "").strip(),
            "models": [m for m in [(os.environ.get("Wovra_MODEL") or "").strip()] if m]}


def _timeout_seconds() -> float:
    raw = (os.environ.get("WOVRA_READ_TIMEOUT") or "").strip()
    try:
        return float(raw) if raw else 180.0
    except ValueError:
        return 180.0


def list_models(provider_id: str = "") -> dict:
    """拉取可用模型（GET `{base_url}/models`）→ {ok, models, error}。

    端点不提供这个接口是常态（不是错误——很多兼容网关只实现 chat/completions）：
    那时回 `ok=False` 并把清单退回"配置里手填的那些"，同时说明原因。
    """
    import urllib.error
    import urllib.request

    target = _probe_target(None, provider_id)
    base = str(target["base_url"] or "").rstrip("/")
    key = str(target["api_key"] or "")
    if not base:
        return {"ok": False, "models": target["models"],
                "error": "这个渠道商还没填端点地址（base_url）"}
    request = urllib.request.Request(
        base + "/models", headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(request, timeout=min(_timeout_seconds(), 30)) as resp:
            payload = json.loads(resp.read(2_000_000).decode("utf-8", "replace"))
    except urllib.error.HTTPError as error:
        return {"ok": False, "models": target["models"],
                "error": f"端点返回 {error.code}：{_short(error.read(2000))}"}
    except Exception as error:  # noqa: BLE001——检测必须把任何失败如实报出来
        return {"ok": False, "models": target["models"],
                "error": f"{type(error).__name__}: {error}"}
    items = payload.get("data") if isinstance(payload, dict) else None
    models: list[str] = []
    for item in items or []:
        if isinstance(item, dict) and item.get("id"):
            models.append(str(item["id"]))
        elif isinstance(item, str):
            models.append(item)
    if not models:
        return {"ok": False, "models": target["models"],
                "error": "端点没返回模型清单（可手工在下面填模型名）"}
    return {"ok": True, "models": sorted(models), "error": ""}


def test_provider(provider_id: str = "", model: str = "") -> dict:
    """发一次最小请求测连通 → {ok, ttft, model, reply, error}。

    报**首字延迟**（TTFT）而不是总时长：总时长里混着生成长度，看不出端点快慢。
    """
    import time as _time

    from .llm import LLM, reasoning_of

    target = _probe_target(None, provider_id)
    if not (target["base_url"] or target["api_key"]):
        return {"ok": False, "error": "这个渠道商还没填端点或密钥", "ttft": None}
    use_model = str(model or "").strip() or (
        target["models"][0] if target["models"] else "")
    if not use_model:
        return {"ok": False, "error": "还没有模型名——先「拉取模型」或手工填一个",
                "ttft": None}
    llm = LLM(api_key=target["api_key"], base_url=target["base_url"], model=use_model)
    started = _time.monotonic()
    try:
        stream = llm.chat([{"role": "user", "content": "ping"}], stream=True)
        ttft = None
        text = ""
        for chunk in stream:
            choices = getattr(chunk, "choices", None)
            if not choices:
                continue
            delta = choices[0].delta
            if ttft is None:
                ttft = _time.monotonic() - started
            text += str(getattr(delta, "content", "") or "")
            text += reasoning_of(delta)
            if len(text) > 200:
                break
    except Exception as error:  # noqa: BLE001——检测要把原始错误原文给出来
        return {"ok": False, "error": f"{type(error).__name__}: {error}",
                "ttft": None, "model": use_model}
    return {"ok": True, "ttft": round(ttft or (_time.monotonic() - started), 2),
            "model": use_model, "reply": text.strip()[:200], "error": ""}


def _short(raw) -> str:
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw or "")
    return " ".join(text.split())[:200]

