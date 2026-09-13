# 给 LLM 用的免费爬虫/网络检索方案调研

调研日期：2026-09-13　　触发：修完 web 工具四缺陷（worklog §84）后，评估是否该
引入外部方案替代自研的 `src/wovra/tools/web.py`。

取证仪器（本仓库常驻，勿删）：
* `scripts/probe_github_stars.py` —— GitHub API 限流（匿名 60/h）时，直接抓项目页取 star/license。
* `scripts/probe_pypi_meta.py` —— 取 PyPI 版本 / requires-python / **依赖条数** / license。

事实数据均为上述脚本当日实跑所得，非引用二手描述。

## 0. 结论先行

**不建议整体替换自研 web 工具。** 理由：Wovra 的依赖底子是 7 个直接依赖、网络层
零依赖（纯 stdlib `urllib`）；而主流方案要么拖进 38–54 个依赖 + Chromium，要么是
需要额外部署的服务。真正值得做的是三件低风险的事（见 §4），以及**一个已经证伪的前提**：
本机 web_fetch 之前"打不开任何网页"不是设计选择，是 §84 修的 Fake-IP 双栈缺陷。

## 1. 方案分档

### A 档：纯解析/提取库（无浏览器，可 pip 装进 Wovra 进程）

| 项目 | Star | License | PyPI 依赖数 | requires-python | 说明 |
|---|---:|---|---:|---|---|
| trafilatura | 6,802 | Apache-2.0 | **38** | ≥3.10 | 正文提取事实标准，去导航/页脚效果最好 |
| readability-lxml | — | Apache-2.0 | 5 | <3.15,≥3.8.2 | Readability 算法，轻，但久未大更 |
| selectolax | — | MIT | **1** | <3.15,≥3.9 | 极快的 HTML 解析器（C 绑定 Lexbor） |
| lxml | — | BSD-3-Clause | 4 | ≥3.8 | 老牌解析器，BeautifulSoup 后端 |
| html2text | — | **GPL-3.0** ⚠ | **0** | ≥3.9 | 零依赖，但 GPL 传染性需注意 |

**要点**：`trafilatura` 是效果最好的，但 38 个依赖——对一个 7 依赖的项目是量级跃迁。
`selectolax`（1 依赖）或 `readability-lxml`（5 依赖）是"轻量改善正文提取"的可行档。

### B 档：搜索 API 客户端（替换现在的 DDG/Bing 裸抓 HTML）

| 项目 | Star | License | 依赖数 | 说明 |
|---|---:|---|---:|---|
| ddgs (原 duckduckgo-search) | — | MIT | **16** | 维护中的 DDG 客户端，多后端自动回退 |
| searxng/searxng | 36,895 | AGPL-3.0 ⚠ | 服务 | 自托管元搜索引擎，一劳永逸但**要跑服务** |

**要点**：Wovra 现在是自己正则解析 DDG 的 `html.duckduckgo.com`——这正是 §84 里
"Bing 跳转壳解析失败"那类脆弱点的来源（对方改 HTML 就崩）。`ddgs` 16 依赖、MIT，
是相对低成本的替代；SearXNG 要常驻服务，与"单进程 CLI 工具"形态不符。

### C 档：浏览器渲染型爬虫（能跑 JS，但重）

| 项目 | Star | License | PyPI 依赖数 | 说明 |
|---|---:|---|---:|---|
| crawl4ai | 82,851 | Apache-2.0 | **54** | LLM 专用，输出干净 Markdown；需 Playwright/Chromium |
| firecrawl/firecrawl | 179,662 | **AGPL-3.0** ⚠ | — | TS 服务，支持 self-host；API 形态，不是库 |
| browser-use | 114,417 | MIT | — | 让 LLM 操作浏览器，重（要真浏览器） |
| lightpanda-io/browser | 35,313 | AGPL-3.0 ⚠ | — | Zig 写的轻量无头浏览器（替代 Chromium，省内存） |
| markitdown (微软) | 183,365 | MIT | 38 | **文档→Markdown**（PDF/Office），不是爬虫，但同族有用 |
| playwright | — | Apache-2.0 | 2 | 底层；**依赖数少但要下 ~300MB 浏览器** |

**要点**：crawl4ai 的 82.8k star 很诱人，但 54 个依赖 + Chromium。对一个已知要
控制前缀缓存、单进程、Windows cmd 环境的项目，属于形态不匹配。**若真要 JS 渲染**，
`lightpanda` 是内存更省的路线，但 AGPL + 额外二进制。

### D 档：自托管服务（Firecrawl/Tavily 替代）

| 项目 | Star | License | 说明 |
|---|---:|---|---|
| us/crw | 979 | AGPL-3.0 | Rust 单二进制，内嵌 MCP server，DROP-in Firecrawl 兼容 API（/scrape /crawl /search）。自述 6MB RAM。**厂商自报**性能数（2.3x Tavily / 1.5x Firecrawl）未独立复核 |
| vakra-dev/reader | 560 | Apache-2.0 | TS，scrape/crawl + 浏览器会话 |
| GramosoftAI/GcrawlAI | 52 | MIT | Python，stealth 模式 + 分布式 + WebSocket 进度 |
| raintree-technology/docpull | 25 | MIT | Python CLI + MCP，**local-first**，带版本与引用（cited context） |

**要点**：这些都要求"跑一个服务"。crw 的单二进制 + Firecrawl 兼容 API 是最省事的
自托管路线，但把网络出口交给第三方进程 = 安全边界外移，与 Wovra 现在
`_assert_public_url` 那套 SSRF 防护（§84 刚补的逐跳校验）需要重新对齐。

### E 档：不是爬虫、但改变抓取效率的协议侧做法

| 项目 | Star | 说明 |
|---|---:|---|
| yazinsai/site-md (58) / JakubKontra/next-markdown-mirror (12) | 小 | **内容协商**：Agent 请求时站点直接返回 Markdown，人类拿 HTML |
| （llms.txt 约定） | — | 站点在 `/llms.txt` 声明 llms 友好入口 |

**要点**：成本最低、收益明确的**客户端**部分——抓取前先探 `Accept: text/markdown`
与 `/llms.txt`。不需要引入任何依赖。

### 参照：专用文档站爬虫（小项目，可读源码学思路）

`paulpierre/markdown-crawler`（470★, MIT, Python）、`Sriram-PR/doc-scraper`
（99★, Apache-2.0, Go）、`xVc323/omnidocs`（12★, MIT, Python）。

## 2. 关键取舍

| 维度 | 自研（现状） | A 档轻库 | C 档浏览器 | D 档服务 |
|---|---|---|---|---|
| 新增依赖 | 0 | 1–38 | 54 + Chromium | 0（但要跑进程） |
| 能跑 JS 站点 | ✗ | ✗ | ✓ | ✓ |
| SSRF 防护可控性 | 完全自主（§84 已逐跳校验） | 同左 | 需重做 | **边界外移** |
| 前缀缓存影响 | 无（docstring 未变） | 无 | 无 | 需新 tools schema |
| 脱网/内网可用 | ✓ | ✓ | ✓ | ✗（要连服务） |
| 维护成本 | 自己扛（§84 就是自己扛的代价） | 上游扛 | 上游扛 | 上游扛 |

## 3. 对 Wovra 的具体建议（按性价比排序）

1. **先做零依赖的改进（本次已部分完成）**：§84 的四修已经拿回了实质能力——
   Fake-IP 双栈打通全网、逐跳 SSRF 校验、跳转壳解包、编码兜底。剩余零依赖项：
   正文提取改用启发式密度打分（去导航/页脚）、结果缓存、`Accept: text/markdown`
   与 `/llms.txt` 探测。**不需要引入任何依赖。**
2. **正文提取可选加 `selectolax`（1 依赖）**：若自研启发式不够好，这是最小侵入的
   升级（1 依赖 vs trafilatura 的 38）。**建议先自研，不够再上**——因为 import
   时机影响前缀缓存，换库要重算一次，值得等有实测差距再做。
3. **搜索通道可选加 `ddgs`（16 依赖，MIT）**：比正则抓 DDG HTML 稳。但同样——
   现在的 DDG→Bing 双通道在 §84 后是可用的，除非再遇到限流再换。
4. **不建议引入**:crawl4ai（54 依赖 + Chromium）、SearXNG（要服务）、
   crw/reader（服务化 + 安全边界外移）。**除非出现明确的 JS 渲染需求**
   （比如要抓 SPA 文档站），那时优先评估 lightpanda 而非 Chromium。

## 4. 未验证项（诚实标注，勿当结论引用）

* crw 自报的性能倍数（2.3x Tavily）——**厂商页面数据，未独立复核**。
* lightpanda 的实际兼容性——未测。
* 各方案的"提取质量"排序——本次只核了 star/license/依赖数等**可机械取证**的维度，
  **没有做同题对比抓取**。若要认真选型，应拿 10 个真实 URL（含中文 GBK 页、
  SPA 页、长文档页）做同题实测。
* `pysearx` 在 PyPI 返回 HTTPError（可能已改名/下架），未深究。
