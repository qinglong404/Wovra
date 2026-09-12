# Wovra 前端可视化：规划与接口契约（活文档）

> 状态：**意图存档 + 第一大步已实现**（`wovra serve` 只读 API + `webui/`
> 单页仪表盘）。纪律沿用：最终形态可推翻表述，接口字段只增不改。
> 分工（2026-09-12 用户拍板）：本线**只做前端可视化**；功能优化在另一
> 会话进行——两侧以本文的接口契约为界，互不改对方文件。

## 0. 一页结论

Wovra 的人机协同界面目前全在 CLI（report/maint/todo/\命令）。前端化的
原则与上下文管理同构：**task.json 是唯一事实源，前端是它的只读投影**。
接口层（`wovra serve`）零 LLM、零写入、零 agent 依赖——只做"磁盘事实 →
JSON"的机械派生；页面零构建（单文件 HTML + 原生 JS），`wovra serve`
起一个本地-only 的 HTTP 服务即可看。

## 1. 目标与非目标

**目标**：把"长时运行 AI 工作"的现场变成人可扫视的仪表盘——会话列表、
轮×块时间线、状态账本、大步/小步计划、agent 注册表、逐调用成本与缓存
命中。设计标准：**好看是验收项**（用多模态截图自检迭代，不是嘴上说）。

**非目标**：不做任何写操作（拍板/回复升级仍走 CLI 的交互通道）；不改
agent/ 功能层；不引入构建链（webpack/vite 均不要）。

## 2. 信息架构（页面 = 单页五视图）

| 视图 | 内容 | 数据源 |
|---|---|---|
| 时间线（默认） | 轮卡片流：R{seq} + 用户输入 + 状态徽章（end_state / org_state / 代次 / steps_used）+ 块列表（文件 × 生命周期标签 × 操作）| rounds |
| 账本 | TaskState 七列表 + 升级置顶（决策升级=等你拍板，视觉最高权重）| task_state |
| 计划 | 当前大步/小步 + 验收标准 + history（evidence 折叠展开）| todo |
| 域注册表 | agent 卡片（id/名称/状态点/文件域/收件箱），分裂判据摘录 | registry + 近轮 split_assessment |
| 用量 | KPI（Σprompt / 命中率 / 调用数 / TTFT 均值）+ 命中率条 + 逐调用序列（SVG）+ finish 分布 | history.llm_call 逐行解析 |

事件级查看（M2）：点块 → 抽屉拉取该轮事件原文，块内事件 ID 高亮。

## 3. 接口契约（只读，字段只增不改）

传输：`wovra serve [--host 127.0.0.1] [--port 8600]`，stdlib http.server，
**GET-only**（其余 405），静态页托管于 `/`。绑定默认 loopback——数据含
工作区路径与内容，不对外网暴露。

| 端点 | 返回 | 说明 |
|---|---|---|
| `GET /api/sessions` | `{scanning, sessions:[…]}` | 摘要列表（后台按 mtime 增量解析，先到先显示） |
| `GET /api/sessions/{id}` | 会话元数据 | 摘要 + task_state + todo + registry + **rounds 元数据（不含 events——task.json 可达 16MB，事件按需取）** |
| `GET /api/sessions/{id}/rounds/{seq}` | `{events, blocks}` | 单轮完整事件与块（对应 expand 语义） |

摘要条目：`{id, goal, status, workspace, mode, created_at, updated_at,
rounds, org:{done,pending,failed,raw}, escalations, experiments,
todo_milestone, usage:{calls,prompt,cached,miss,completion,ttft_sum,finish:{…}}}`。
`usage` 由 history 的 `llm_call` 行机械解析（prompt/cached/miss/completion/
ttft/dur/finish）——解析只在 serve 侧做一次，前端不重复实现口径。

缓存纪律：serve 进程内按 task.json mtime 缓存摘要（文件没变不重解析）；
16MB 级会话的完整解析只发生在按需取轮时。

## 4. 边界纪律（与功能会话的分界线）

* **只新增**：`src/wovra/serve.py`、`webui/`、`tests/test_serve/`、本文档；
  `cli/main.py` 只加一个子 parser（3 行）。不碰 agent/、task.py、tools/。
* **只读**：serve 不写 tasks/、不写 .wovra/、无任何状态变更端点。
* **口径单一**：成本/命中率口径以 llm.py 的落账行为准，serve 只做字符串
  → 数值的机械解析，不定义新口径。

## 5. 大步切分（todo 大步制）

* **M1（已实现）**：只读 API + 五视图仪表盘 + 视觉自检迭代。
  验收：真实会话（16MB/79 轮）可流畅扫视；截图人工（多模态）验收通过。
* **M2**：事件抽屉（按需取轮事件、块内事件高亮、expand 语义对齐）。
* **M3**：拍板交互（升级项的人尺寸选项、写侧接口设计后另拍板）。

## 6. 待拍板

* 对外暴露（LAN）要不要做——默认拒绝，loopback-only。
* 事件抽屉的默认展开深度（M2 再定）。
