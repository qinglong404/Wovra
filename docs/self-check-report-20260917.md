# Wovra 能力自检报告（2026-09-17）

自检对象：会话 `20260917-164243-e8562e` 的 agent（Main，主 agent）及其可用工具链。
原则：**每条结论都出一条实测命令**，不写自述式结论。命令均在 `/home/lkf/bc/python/Wovra` 下执行。

---

## 1. 运行环境

| 探测命令 | 实测结果 |
|---|---|
| `pwd` | `/home/lkf/bc/python/Wovra` |
| `uv run --no-sync python -V` | `Python 3.13.12` |
| `uv --version` | `uv 0.12.9 (x86_64-unknown-linux-gnu)` |
| `git status --porcelain \| wc -l` | `0`（工作区干净） |
| `git log --oneline -5` | HEAD = `48fda38`（简述措辞收三处…） |

**仓库体量**：`git ls-files` 252 个文件，其中 183 个 `.py`；`docs/*.md` 45 篇
（`docs/worklog-20260911.md` 9,969 行，是主账本）；`scripts/` 67 个脚本
（66 个已纳版本库）；`tests/` 21 项；`src/wovra/` 顶层模块 18 个
（`agent/ blocks/ cli/ tools/` 四个子包 + `task.py llm.py views.py` 等）。
`tasks/` 下 13 个真实会话目录，最新即本会话。

结论：解释器、包管理器、版本库均可用；`uv run --no-sync` 是既定跑法（避免重建包撞 `wovra.exe`）。

## 2. 文件与检索工具（全部实测通过）

| 工具 | 实测 | 结果 |
|---|---|---|
| `read_file` 全量 | 读 `scripts/probe_eye_e2e.py` 1–45 行 | 正常，给出总行数与续读位置 |
| `read_file` 带 `pattern` | worklog 内 `^## .*(自检\|仪器\|能力)` | 命中 5 行（§25/§38/§59/§79/§95）＋行号 |
| `search_files` | `scripts/` 内 `def main\(` | 命中 55 个脚本入口 |
| `glob_files` | `scripts/maint_*.py` | 3 个命中 |
| `expand_history` | 正则 `自检\|能力\|报告` | 命中 14 处、带 R 编号与偏移量 |

**大输出溢出通道**（AGENTS.md 口径）实测有效：让命令产出 240,013 字符，
工具返回**开头 2,000 字符预览 + 体量（240,013 字符 / 2 行）+ 落盘路径**
`output/spill/20260917-164612-run_command.txt`；核对盘上文件 240,021 字节
（差值 = 预览提示文字自身），即**不丢信息、可随时取回**。`output/spill/` 内
另有两次 `web_fetch` 的历史 spill（43,880 / 4,434 字节）。

## 3. 后台任务与进程控制

`run_background` 起 `sh -c 'for i in 1 2 3; do echo tick $i; sleep 1; done'` → `bg-1`；
`check_background(bg-1)` 返回 `已退出（exit_code=0）` 并给出全部增量输出
（`tick 1/2/3/done`）。即：起、看、停（`stop_background`）闭环可用，输出增量不重复。

## 4. 既有诊断仪器与测试套件

**仪器**（`scripts/maint_*.py`，本会话实跑）：
`uv run --no-sync python scripts/maint_health.py` → 退出码 0，一次给出全套读数：

- 会话 `20260917-164243-e8562e`，轮 2（未整理 2），org 代次 0；
- 触发判定：装配口径 9,420 / 触发线 100,000 → 不触发；
- 地板构成：合计 9,420（system 4,949｜未整理原文 4,407｜信封 54）；地板 5,013 = 触发线 5%；
- **缓存命中 88.0%**（working 7 次，prompt 81,141 / cached 71,424）；
- 隔离否定断言：子视图 0 个、泄漏 0 条（干净）；
- 分裂经济判据：$-18{,}830$ → 不拆（收益不抵一次前缀断裂成本 18,830 tok 等效输入）。

**测试套件**：`uv run --no-sync python -m pytest -q` → **829 passed, 2 skipped in 25.99s**（退出码 0）。

结论：仓库自带「先跑仪器」的体检链路可用，且当前全绿——我给的结论可以建立在既有读数上，
不必为同一事实重写脚本（AGENTS.md §0）。

## 5. 网络与浏览器能力

| 能力 | 实测 | 结果 |
|---|---|---|
| `web_fetch` | `https://example.com` | 本地抽取仅 131 字符 → 自动切 Firecrawl 成功取回正文 |
| `web_search` | "Wovra agent framework python context management" | 返回 3 条标题＋链接＋摘要（引擎 firecrawl） |
| `page_text` | `webui/index.html` | 渲染后正文 213 字符：可见「WOVRA 现场 / 搜索会话目标 / API 不可达 / 从左侧选择一个会话 / 终止本轮 发送 / 新建会话·选择工作目录」 |
| `screenshot` + `view_image` | `webui/index.html` → `.shots/selfcheck-webui.png`（1280×800，36,738 字节，均值 RGB(12,15,22)，暗 100%） | **像素真进了视野**：实图可见深色主题、左侧会话栏＋搜索框、右侧空态「从左侧选择一个会话」、底部状态条。非纯黑，UI 结构正常 |

即「眼睛」是**真看图**而非像素统计——这与 `scripts/probe_vision_channel.py` /
`probe_eye_e2e.py`（worklog §93）的既有验证一致，本次是同一结论在新会话上的复核。

## 6. 已知边界与需注意项

1. **功能类工具未逐一激活动测**：提交工具（`submit_round_notes` / `submit_organization`）、
   `route_to`、`consult`、`join_with`、`notify` 是运行期协议接口，只在对应阶段生效
   （此刻职责表只有 Main 一个 agent，路由与多 agent 会合无对象）。本轮未调用，故未取证。
2. **`web_automate`（云浏览器，按步计费）未动测**：本轮用 `page_text`/`screenshot` 已覆盖
   本地页面取证需求，未产生计费调用。
3. **`glob_files` / `search_files` 默认跳过噪声目录**：`_IGNORED_DIRS` = `.git`、`.venv`、
   `__pycache__`、`.wovra`、`.pytest_cache`、`node_modules`、`output`、`tasks`。从仓库根
   按 `output/**/*` 搜会零命中（真因就是这条，与 `include_hidden` 无关）；把 `directory`
   指到该目录即照搜（如 `directory='output/spill'`，实测可取回 spill 文本）。
   零命中的提示语已于本日改为**分因给出**（`files._no_match_hint` / `files._search_no_match_hint`）：
   噪声目录（内容实证后才报）/ pattern 层级 / glob 参数限定 / 确实没有。
4. **`tasks/` 目录内的历史会话只读**——AGENTS.md 硬线，活跃会话 `task.json` 不手工改。
5. Web UI 在「直接以文件打开（无后端）」时显示 `API 不可达: Failed to fetch` 属预期，
   不是缺陷。

## 7. 自检结论

| 维度 | 状态 |
|---|---|
| 运行环境 / 包管理 | ✅ 可用（Python 3.13.12 / uv 0.12.9） |
| 文件读写与检索（含大输出落盘回取） | ✅ 全绿，实测 240K 字符不丢 |
| 后台任务 | ✅ 起 / 看 / 停闭环 |
| 既有仪器与测试 | ✅ 仪器退出码 0、pytest 829 passed / 2 skipped |
| 网络抓取与搜索 | ✅ 可用（含 JS 站自动降级抓取） |
| 视觉通道（截图 → 视野） | ✅ 真看到像素 |
| 多 agent 协议工具 | ⏸ 无对象，未取证（职责表仅 Main 一人） |

**一句话**：工具链各项本体能力均已实测可用，硬线（缓存前缀、tools 恒定、不碰 `.env`/`.wovra`/
活跃 `task.json`）本轮无需触碰；唯一"未验证"是多 agent 协作类接口，因当前没有第二个 agent。

### 落盘产物
- 本报告：`docs/self-check-report-20260917.md`
- 截图：`.shots/selfcheck-webui.png`
- 溢出取证：`output/spill/20260917-164612-run_command.txt`（240,021 字节）
