"""生成前端"看真容"预览页（纯本地，零 LLM）：配置页与聊天页顶栏。

为什么需要（AGENTS.md 界面/视觉验收纪律）：观感是评分权重最高的项之一，而
`webui_render_check.py` 只回答"渲染函数有没有产出 undefined/NaN"——它**看不见**
配色、重叠、对齐。这台仪器把页面在真浏览器里跑起来、喂假数据，供人眼（或
`view_image`）核对。

用法：
    uv run --no-sync python scripts/webui_preview.py --view config
    uv run --no-sync python scripts/webui_preview.py --view chat
产出 `output/_preview_<view>.html`，再用 screenshot 截图。
"""
import argparse
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]

# 假数据：两张视图各要什么就给什么（密钥一律掩码，与真后端一致）
PROVIDERS = {
    "current": "deepseek",
    "file": str(ROOT / "providers.json"),
    "from_env": False,
    "levels": ["off", "auto", "high"],
    "providers": [
        {"id": "deepseek", "name": "DeepSeek", "base_url": "https://api.deepseek.com",
         "api_key": "", "masked": "******c300", "set": True,
         "models": ["deepseek-chat", "deepseek-reasoner"], "reasoning_field": "auto",
         "missing": []},
        {"id": "ark", "name": "火山方舟", "base_url": "https://ark.cn-beijing.volces.com/api/v3",
         "api_key": "", "masked": "******a1b2", "set": True,
         "models": ["doubao-seed-1-6", "deepseek-v3-1"], "reasoning_field": "thinking",
         "missing": []},
    ],
}

META = {
    "id": "20260918-104931-987e97", "goal": "给前端加一个配置页面", "status": "in_progress",
    "mode": "managed", "workspace": "/home/lkf/bc/python/Wovra",
    "updated_at": "2026-09-18T12:30:00", "rounds": 12, "tools": 84,
    "usage": {"calls": 40, "prompt": 1024000, "cached": 900000, "miss": 124000,
              "completion": 12000, "ttft_sum": 40.0, "ttft_work_sum": 30.0, "ttft_work_n": 10},
    "round_list": [], "registry": [{"id": "Main", "name": "主agent", "status": "active"}],
    "safety_mode": "approve", "approved_tags": [], "escalations": 0,
    "model": "deepseek-reasoner", "reasoning": "high", "provider": "deepseek",
}

STUB = """<script>
(function(){
  const DATA = %s;
  window.fetch = function(url){
    const u = String(url);
    let body = {};
    if (u.indexOf('/api/settings') >= 0) body = DATA.settings;
    else if (u.indexOf('/api/providers') >= 0) body = DATA.providers;
    else if (/\\/api\\/sessions\\/[^/]+$/.test(u)) body = DATA.meta;
    else if (u.indexOf('/api/sessions') >= 0)
      body = {sessions: [], scanning: false, server_started: '2026-09-18 09:00'};
    return Promise.resolve({ok: true, json: function(){ return Promise.resolve(body) }});
  };
  %s
})();
</script>
"""

BOOT = {
    "chat": ("setTimeout(function(){ META = DATA.meta; PROVS = DATA.providers;"
             " CUR = DATA.meta.id; renderHeader(); }, 80);"),
    "config": "setTimeout(function(){ openConfig(); }, 80);",
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--view", choices=sorted(BOOT), default="config")
    args = ap.parse_args()

    if str(ROOT / "src") not in sys.path:
        sys.path.insert(0, str(ROOT / "src"))
    from wovra import settings as settings_module   # 真参数表，别造假

    data = {"settings": settings_module.describe(), "providers": PROVIDERS, "meta": META}
    html = (ROOT / "webui" / "index.html").read_text(encoding="utf-8")
    stub = STUB % (json.dumps(data, ensure_ascii=False), BOOT[args.view])
    marker = '<script>\n"use strict";'
    if marker not in html:
        print("找不到主脚本入口，预览未生成", file=sys.stderr)
        return 1
    out = ROOT / "output" / f"_preview_{args.view}.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html.replace(marker, stub + marker, 1), encoding="utf-8")
    print(f"已生成 {out.relative_to(ROOT)}（view={args.view}，"
          f"{len(data['settings']['items'])} 项参数 / "
          f"{len(PROVIDERS['providers'])} 个渠道商）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
