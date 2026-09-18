"""模型渠道商清单与检测：增删改、掩码、迁移、选择解析、请求体带强度。"""

import json
import os
from pathlib import Path

import pytest

from wovra import providers as P


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """清单落到临时目录（绝不碰仓库根那份）。"""
    monkeypatch.setenv("WOVRA_PROVIDERS_FILE", str(tmp_path / "providers.json"))
    # 起点干净：`.env` 的键也清掉，免得迁移逻辑被真实配置触发
    for key in ("Wovra_API_KEY", "Wovra_BASE_URL", "Wovra_MODEL"):
        monkeypatch.delenv(key, raising=False)
    return tmp_path


def test_save_load_and_secret_mask(tmp_path):
    """存一份、读回来；密钥只回掩码、明文不出现在 describe 里。"""
    P.save({"current": "a", "providers": [
        {"id": "a", "name": "甲", "base_url": "https://a.example/v1",
         "api_key": "sk-secret-abcd1234", "models": ["m1", "m2"]},
        {"id": "b", "name": "乙", "base_url": "https://b.example/v1",
         "api_key": "k2", "models": ["n1"]},
    ]})
    data = P.load()
    assert data["current"] == "a" and len(data["providers"]) == 2
    assert P.get("a")["api_key"] == "sk-secret-abcd1234"      # 后端内部拿明文
    view = P.describe()
    blob = json.dumps(view, ensure_ascii=False)
    assert "sk-secret-abcd1234" not in blob                    # 前端拿不到明文
    row = {p["id"]: p for p in view["providers"]}["a"]
    assert row["api_key"] == "" and row["masked"].endswith("1234") and row["set"]


def test_file_is_0600_and_missing_file_is_empty(tmp_path):
    """文件权限 600（里面有密钥）；文件不存在读成空清单而不是抛错。"""
    assert P.load() == {"current": "", "providers": []}
    P.save({"current": "x", "providers": [{"id": "x", "name": "x"}]})
    if os.name != "nt":
        assert (P.providers_path().stat().st_mode & 0o777) == 0o600


def test_upsert_keeps_old_key_when_blank():
    """前端不回明文：密钥留空 = 沿用旧值（与 .env 里的密钥同一套口径）。"""
    P.save({"current": "a", "providers": [
        {"id": "a", "name": "甲", "base_url": "https://a/v1",
         "api_key": "keep-me", "models": ["m1"]}]})
    P.upsert({"id": "a", "name": "甲改名", "base_url": "https://a2/v1", "api_key": ""})
    item = P.get("a")
    assert item["api_key"] == "keep-me"
    assert item["models"] == ["m1"]          # 模型清单没给也沿用
    assert item["name"] == "甲改名"


def test_upsert_assigns_id_and_models_from_text():
    """没给 id 时按名字生成；models 传字符串（前端多行文本）也收。"""
    P.upsert({"name": "火山 方舟", "base_url": "https://ark/v3",
              "models": "doubao-1\ndoubao-2"})
    data = P.load()
    assert len(data["providers"]) == 1
    item = data["providers"][0]
    assert item["id"] and item["id"].isascii()
    assert item["models"] == ["doubao-1", "doubao-2"]


def test_remove_and_current_falls_back():
    """删掉当前项 → 当前切到剩下的第一条；删空 → current 为空。"""
    P.save({"current": "a", "providers": [
        {"id": "a", "name": "甲"}, {"id": "b", "name": "乙"}]})
    P.remove("a")
    assert P.load()["current"] == "b"
    P.remove("b")
    assert P.load() == {"current": "", "providers": []}


def test_set_current_ignores_unknown():
    P.save({"current": "a", "providers": [{"id": "a", "name": "甲"}]})
    P.set_current("不存在")
    assert P.load()["current"] == "a"
    P.set_current("a")
    assert P.load()["current"] == "a"


def test_describe_derives_from_env_without_writing():
    """清单不存在时，`describe()` 照着 .env 现场推导一份，**且不落盘**。

    实测缺陷：推导函数写了却没接到 `describe()` 上——真服务里面板返回
    `from_env: True` 而清单是空的（`.env` 明明配着密钥与端点），用户看到
    "还没有渠道商"要从零填。
    """
    os.environ["Wovra_API_KEY"] = "env-key"
    os.environ["Wovra_BASE_URL"] = "https://api.deepseek.com"
    os.environ["Wovra_MODEL"] = "deepseek-chat"

    data = P.env_derived()
    assert len(data["providers"]) == 1
    item = data["providers"][0]
    assert item["base_url"] == "https://api.deepseek.com"
    assert item["api_key"] == "env-key" and item["models"] == ["deepseek-chat"]
    assert "deepseek" in item["id"]

    view = P.describe()
    assert view["from_env"] is True and len(view["providers"]) == 1
    assert view["providers"][0]["api_key"] == ""            # 前端仍只拿掩码
    assert not P.providers_path().exists()                  # GET 无副作用

    # 保存一条之后文件才出现，此后 describe 走文件
    P.upsert({"name": "手填", "base_url": "https://x/v1", "api_key": "k"})
    assert P.providers_path().exists()
    view2 = P.describe()
    assert view2["from_env"] is False
    # **`.env` 推导的那条被一并落盘**：用户在面板上看到过它，首次改动不该让它消失
    # （否则"我明明看到有一条，点一下新增就没了"）。current 也不被新增动作改掉。
    assert [p["name"] for p in view2["providers"]] == [item["name"], "手填"]
    assert view2["current"] == item["id"]


def test_env_derived_without_env_is_empty():
    assert P.env_derived() == {"current": "", "providers": []}
    assert P.describe()["providers"] == []


def test_list_models_reports_raw_error(tmp_path, monkeypatch):
    """拉模型失败要报端点原文（检测的用处就在这里），不是翻译后的套话。"""
    P.save({"current": "a", "providers": [
        {"id": "a", "name": "甲", "base_url": "http://127.0.0.1:9/v1",
         "api_key": "k", "models": ["fallback"]}]})
    out = P.list_models("a")
    assert out["ok"] is False and out["models"] == ["fallback"]
    assert out["error"]                                   # 有原文
    # 没填端点 → 明确说缺什么
    P.save({"current": "b", "providers": [{"id": "b", "name": "乙", "models": []}]})
    assert "base_url" in P.list_models("b")["error"]


def test_test_provider_without_model_says_so():
    P.save({"current": "b", "providers": [
        {"id": "b", "name": "乙", "base_url": "http://127.0.0.1:9/v1",
         "api_key": "k", "models": []}]})
    out = P.test_provider("b")
    assert out["ok"] is False and "模型" in out["error"]


def test_llm_uses_session_provider_and_model():
    """会话选的渠道商/模型优先于 .env（切会话即切）。"""
    from wovra.llm import LLM

    P.save({"current": "cur", "providers": [
        {"id": "cur", "name": "当前", "base_url": "https://cur/v1",
         "api_key": "k-cur", "models": ["cur-model"]},
        {"id": "other", "name": "另一个", "base_url": "https://other/v1",
         "api_key": "k-other", "models": ["other-model"]}]})
    os.environ["Wovra_API_KEY"] = "env-key"
    os.environ["Wovra_BASE_URL"] = "https://env/v1"
    os.environ["Wovra_MODEL"] = "env-model"

    llm = LLM()
    assert llm.model == "cur-model" and llm.base_url == "https://cur/v1"
    assert llm._resolve()[0] == "k-cur"

    llm.set_session(provider_id="other", model="other-model", reasoning="high")
    assert llm.model == "other-model" and llm.base_url == "https://other/v1"
    assert llm._resolve()[0] == "k-other"


def test_llm_falls_back_to_env_without_providers():
    from wovra.llm import LLM

    os.environ["Wovra_API_KEY"] = "env-key"
    os.environ["Wovra_BASE_URL"] = "https://env/v1"
    os.environ["Wovra_MODEL"] = "env-model"
    llm = LLM()
    assert llm.model == "env-model" and llm.base_url == "https://env/v1"
    assert llm._resolve()[0] == "env-key"


def test_reasoning_body_by_level_and_field():
    """思考强度 → 请求体字段：字段名按渠道商声明，auto 时按端点域名猜。"""
    from wovra.llm import LLM

    P.save({"current": "p", "providers": [
        {"id": "p", "name": "火山", "base_url": "https://ark.cn-beijing.volces.com/api/v3",
         "api_key": "k", "models": ["m"]}]})
    llm = LLM()
    assert llm.reasoning_body() == {}                      # 默认 auto：不发字段
    llm.set_session(reasoning="high")
    assert llm.reasoning_body() == {"thinking": {"type": "enabled"}}   # 方舟系
    llm.set_session(reasoning="off")
    assert llm.reasoning_body() == {"thinking": {"type": "disabled"}}

    # 通用端点 → reasoning_effort（关 = minimal）
    P.save({"current": "q", "providers": [
        {"id": "q", "name": "通用", "base_url": "https://api.example.com/v1",
         "api_key": "k", "models": ["m"]}]})
    llm2 = LLM()
    llm2.set_session(reasoning="high")
    assert llm2.reasoning_body() == {"reasoning_effort": "high"}
    llm2.set_session(reasoning="off")
    assert llm2.reasoning_body() == {"reasoning_effort": "minimal"}

    # 显式钉字段：覆盖按域名猜的结果
    P.save({"current": "r", "providers": [
        {"id": "r", "name": "钉死", "base_url": "https://api.example.com/v1",
         "api_key": "k", "models": ["m"], "reasoning_field": "thinking"}]})
    llm3 = LLM()
    llm3.set_session(reasoning="high")
    assert llm3.reasoning_body() == {"thinking": {"type": "enabled"}}
