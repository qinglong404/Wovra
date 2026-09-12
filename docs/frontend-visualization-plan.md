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
对话流（C 线，默认视图）、轮×块时间线、状态账本、大步/小步计划、agent
注册表、逐调用成本与缓存命中；以及**网页输入**（给会话追加一轮，C4）。
设计标准：**好看是验收项**（用多模态截图自检迭代，不是嘴上说）。

**非目标**：不改 agent/ 功能层；不引入构建链（webpack/vite 均不要）；
升级/拍板类写操作仍走 CLI 交互通道（网页写侧只做对话轮）。

## 2. 信息架构（页面 = 单页五视图）

| 视图 | 内容 | 数据源 |
|---|---|---|
| 时间线（默认） | 轮卡片流：R{seq} + 用户输入 + 状态徽章（end_state / org_state / 代次 / steps_used）+ 块列表（文件 × 生命周期标签 × 操作）| rounds |
| 账本 | TaskState 七列表 + 升级置顶（决策升级=等你拍板，视觉最高权重）| task_state |
| 计划 | 当前大步/小步 + 验收标准 + history（evidence 折叠展开）| todo |
| 域注册表 | agent 卡片（id/名称/状态点/文件域/收件箱），分裂判据摘录 | registry + 近轮 split_assessment |
| 用量 | KPI（Σprompt / 命中率 / 调用数 / TTFT 均值）+ 命中率条 + 逐调用序列（SVG）+ finish 分布 | history.llm_call 逐行解析 |

事件级查看（M2）：点块 → 抽屉拉取该轮事件原文，块内事件 ID 高亮。

## 3. 接口契约（字段只增不改）

传输：`wovra serve [--host 127.0.0.1] [--port 8600]`，stdlib http.server，
默认绑定 loopback——数据含工作区路径与内容，不对外网暴露。

**读侧（GET）**：

| 端点 | 返回 | 说明 |
|---|---|---|
| `GET /api/sessions` | `{scanning, sessions:[…]}` | 摘要列表（后台按 mtime 增量解析，先到先显示） |
| `GET /api/sessions/{id}` | 会话元数据 | 摘要 + task_state + todo + registry + **rounds 元数据（不含 events——task.json 可达 16MB，事件按需取）** |
| `GET /api/sessions/{id}/rounds/{seq}` | `{events, blocks}` | 单轮完整事件与块；`?after=R{n}-E{m}` 只回之后的事件（C3 实时跟随增量） |

摘要条目：`{id, goal, status, workspace, mode, created_at, updated_at,
rounds, steps, org:{done,pending,failed,raw}, escalations, experiments,
todo_milestone, todo_steps_left, last_round:{seq,events,end_state},
usage:{calls,prompt,cached,miss,completion,ttft_sum,finish,rows}}`。
`usage` 由 history 的 `llm_call` 行机械解析（prompt/cached/miss/completion/
ttft/dur/finish）——解析只在 serve 侧做一次，前端不重复实现口径。

**写侧（POST，C4）——唯一的写形态 = 新建会话 / 追加一轮对话**：

| 端点 | 语义 | 互斥 |
|---|---|---|
| `POST /api/sessions` `{goal}` | 新建会话 | — |
| `POST /api/sessions/{id}/turn` `{content}` | 追加一轮对话（复用 CLI agent 管线，懒导入） | 三道：进程内单飞全局锁 / CLI 会话锁文件（与 chat/run 互斥，被占返回 409）/ 任务级 job 去重；返回 202 + job_id，`GET /api/jobs/{job_id}` 轮询状态 |
| 其余一切写路径 | 404 | — |

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

## 7. 大工程：对话流视图（C 线，2026-09-12 用户需求）

> 用户原话：**"我要求你做的是前端页面，就是我平常对话，看到的内容。"**
> 五视图是"仪表盘"（结构与账本的扫视层）；C 线是"现场"（对话本身的
> 阅读层）——把 `wovra chat` 终端里看到的东西搬上网页。C 线建成后成为
> 选中会话时的**默认视图**，五视图退居分析工具位。

### C0 渲染规则表（数据 → 对话消息，全量穷举）

| 数据 | 终端所见 | 网页形态 |
|---|---|---|
| `你> ` 输入 | 用户行 | **用户气泡**（右对齐，浅色底） |
| assistant 正文（final_answer / tool_call 的 content） | 回答横幅 + 流式文本 | **助手消息**（左侧，markdown——C1 纯文本保真，C2 vendored marked + DOMPurify + 代码高亮） |
| assistant 带 tool_calls | "正在<动作>…" + 调用行 | **工具调用卡**（默认折叠）：工具名 + 参数摘要（path/command）+ 状态点（✓ 成功 / ✗ 失败 / 🚫 被拒）+ 耗时 |
| tool 结果行 | 结果行（exit_code/输出） | 并入调用卡展开区：exit_code 徽章 + 输出（截断展示，可全开） |
| 系统行（后台整理/事件唤醒/中断/空响应重试/维护记账） | 灰字提示 | **居中系统行**（muted 小字） |
| `[子agent]` 回复 | 带标签流式 | 带标签的子代理消息块 |
| 轮尾 usage 行 | `耗时… ┃ 轮次… ┃ 步数…` | **轮分隔条**：R{n} · 耗时 · 步数 · 工具调用 · 命中率 · TTFT |
| `empty_stream` / `maintenance` history 行 | 提示 | 系统行 |

* **诚实边界**：思考不落盘（"思考是方法层，不进传递"是既有纪律）——
  回放里没有历史思考；live 模式若要展示，须 llm.py 加思考落账开关，
  **另拍板，不在 C 线默认范围**。

### C1 数据流与配对

* 渲染输入 = rounds 的 events 原文（人的视图），**不是装配流**
  （装配流是模型的视图——两者来源同一份 Full 存档，分辨率规则不同）。
* tool 结果按 `tool_call_id`（缺失时按顺序）配对回调用卡；里程碑检查点
  的跨轮拆分由"按轮连续加载"天然弥合（协议补缝已保证装配层合法，
  对话流直接吃 `/rounds/{seq}` 原文）。
* 分页：默认载最近 5 轮，向上滚动按轮加载更早；`content-visibility:auto`
  先行，C2 视需要上虚拟滚动。16MB task.json 永不整包进前端。

### C2 阅读体验

* vendored 单文件库：marked（markdown）+ DOMPurify（sanitize，模型输出
  必须过净化）+ highlight（代码块）；零构建纪律不变（vendor 静态文件
  ≠ 构建链）。
* 工具卡交互：默认折叠只留一行；点开看参数与输出全文。
* 与时间线联动：对话流里点工具卡 → 时间线对应块高亮（反向亦可）。

### C3 实时跟随

* 轮询（已有 5s 通道）：发现新轮 / `end_state=open` 且 events 增长 →
  增量拉 `/rounds/{seq}`（`?after=R{n}-E{m}` 参数**留接口**，C3 实现）。
* "跟随尾部"开关（默认开，用户上滚即暂停，触底恢复）。
* 运行中指示：顶栏呼吸灯 + 当前步数/工具跳动。
* SSE（`/api/stream`）留接口位（现 405）——轮询够用时不引入复杂度。

### C4 网页输入（另拍板，不在本线）

网页直接发消息 = serve 需要写通道，且与正在运行的 CLI 会话互斥
（session lock 已有基础设施）。这是"前端即界面"的终态，涉及功能层，
单独立项。

### C 线里程碑与验收

| 步 | 内容 | 验收 |
|---|---|---|
| C1 ✅ | 回放对话流：渲染规则表全落地 + 按轮惰性加载 + 轮分隔条 | c34bc5 尾部 5 轮即开即读、无悬空、终端所见皆网页所见（除思考） |
| C2 ✅ | 阅读体验：vendored marked+DOMPurify+highlight（webui/vendor/）、final_answer 走 markdown、content-visibility、对话流⇄时间线双向联动（⇢ 跳转 + flash 定位） | 模型输出的代码/表格可读；两个视图互相一步可达 |
| C3 ✅ | 实时跟随：`?after=` 增量 + 2.5s 轮询（仅 conv 视图 + in_progress + 页面可见时）+ 跟随尾部开关（上滚暂停/触底恢复）+ 运行中呼吸灯 | 对着正在跑的会话开一屏，增长实时可见 |
| C4 ✅ | 网页输入：POST turn（作业队列 + 三道互斥）+ 新建会话 + 底部输入框（Enter 发送/Shift+Enter 换行）+ 乐观气泡与轮询回填 | 网页发一条消息 → 轮真实执行 → 对话流自动长出新轮 |

C4 边界（知情记录）：轮执行跑在 serve 进程内（复用 CLI agent 管线），
与 CLI chat 同时操作同一会话由会话锁文件互斥；升级/拍板类写操作仍走
CLI 交互通道，不在写侧范围。
