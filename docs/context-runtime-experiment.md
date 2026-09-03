# Wovra V1 上下文管理：对照组实验设计文档

> 状态：实验进行中（首轮 4 轮对照数据已采集）
> 本文对应代码：`agent.py`（装配/组织/展开）、`truncate.py`（截断）、`task.py`（TaskState）、`cli.py`（--mode）

---

## 一、实验框架

同一套 Agent、工具集、任务持久化（task.json），仅上下文策略不同。
CLI 通过 `--mode managed|baseline` 切换（默认 managed），同一会话可换模式续聊（原始层共享）。

成本核算对两组一视同仁：每一次 LLM 调用（干活的、整理的、展开后的补读）
都流经 `_stream_call` 计入 `usage` 事件，事件里带 mode 标记。

### 两组共同的底座

- **无报告注入**：goal/summary/最近事件等"报告内容"不再给 AI 看，
  `task.context()` 已从装配中移除；时间线只给人看（零成本，程序维护）
- system 提示词 = 人设 + 工具使用指引
- 提交署名、审计记录（写文件/改文件/执行命令）、破坏性命令黑名单两组一致
- 展示层一致（流式思考/回答、工具行极简、行纪律）

---

## 二、对照组（baseline）＝ 正常现状

| 机制 | 行为 |
|---|---|
| 消息装配 | system + **全部历史轮的原文协议消息** + 当前轮，无任何加工 |
| 压缩 | **无**（按实验设计，哪怕逼近窗口上限也不处理——爆掉本身就是数据） |
| Organization | **无**（不产出摘要/意图/状态，goal/status/summary 冻结） |
| 安全阈值 | **无**（超大工具结果原样进上下文） |
| expand_history | 不注册 |
| 时间线 | 照常记录（给人看） |

预期：输入随轮数线性增长，逼近模型窗口（65536）后稳定性劣化。

---

## 三、实验组（managed）机制细节

### 3.1 基本单位

- **Round**：用户一条消息从发出到最终回答的完整过程（工作记忆边界）
- **Event**：Round 内的每条协议消息，结构：
  `id (R{n}-E{nn}) / type (user|tool_call|tool_result|final_answer|assistant) / timestamp / status (ok|error) / truncated / message (Full)`

### 3.2 Truncated（Runtime 生成，零 LLM 成本）

- 通用规则：折叠空白、取前 **120 字符**（`TRUNCATED_LIMIT`）
- `tool_result`：先判定成败（`FAILURE_MARKERS`），失败加"失败，"前缀；
  `run_command` 专用规则：保留 exit_code 行 + 提取含 error/failed/traceback 的关键错误行
- `tool_call`：工具名 + 参数前 80 字符

### 3.3 安全阈值（Context Safety Limit，仅 managed）

单条工具结果 > **2000 字符**（`SAFE_RESULT_LIMIT`）时：
原文存 `event.full`（事实来源），上下文消息只保留前 2000 字 + 
`[输出过大…完整结果：R{n}-E{nn}，可用 expand_history 查看]`。

### 3.4 当前 Round

**全量保留**（除 3.3 安全阈值），不折叠不截断，直到最终回答/中断/失败。

### 3.5 Round Organization（轮末一次，**额外 LLM 调用**）

- 时机：最终回答之后（`finalize_round` 内）；中断/失败有对应变体提示词
- 输入：**Truncated 事件流 + 最终回答的截断版**（不给全量原文）
- 两段式：模型可用 `read_full(event_id)` 按需展开原文，
  上限 3 次读取 / 4 次调用
- 输出 JSON（剥围栏 + 取最外层花括号解析，失败重试一次，仍失败则本轮不更新）：
  - `normalized_user_input`：用户意图澄清版（不是压缩，可扩写）
  - `round_summary`：分步高浓度摘要（每步保留结论，可合并，不过度压缩）
  - `state_patch`：状态增量补丁
- 运行在**与主任务相同的模型上（含思考）**——实测这是成本与耗时的主要抵消项

### 3.6 Task State（增量 Patch，仅 managed）

字段：`goal / constraints / decisions / completed / known_issues / open_questions / current_status / is_done`

- 列表字段追加去重，每类上限 **20 条**（`STATE_LIST_CAP`），超出淘汰最旧（History 仍可展开找回）
- `current_status` / `goal` 直接覆盖；`is_done` 仅接受布尔
- 每轮以文本块注入 system（渲染预算见参数表）

### 3.7 Context Assembly（每轮开始构建请求）

```text
system = 人设 + Task State 渲染块（预算 1/3）+ 未选中轮次的一行索引
消息体 = 选中轮次的浓缩视图（依轮次顺序）+ 当前 Round 全量
```

- **最近 K 轮**（`max_recent_rounds`，默认 3）恒选
- 更早轮次：与当前输入的相关性 ≥ **2**（`_RELEVANCE_MIN_SCORE`；
  CJK 字符二元组 + ASCII 单词的重合数）才入选，预算内按分数取
- 选中轮次的浓缩视图 = 用户原文全量 + 一条 assistant 消息
  （`[第n轮整理] 意图` + `[摘要]` + 每事件一行截断索引）
- 未选中轮次：只在 system 索引里占一行
- 未整理的轮次（如中断且整理失败）：原文回放兜底
- **当前轮全量**

### 3.8 expand_history（仅 managed 注册）

- `ids`：轮（`R3`）或事件（`R3-E02`），容错逗号分隔字符串
- `level`：truncated / summary / full（容错大小写）
- full 档：事件级取原文（≤4000 字）；轮级遍历全部事件（每事件 ≤2000 字）
- 展开只是读取；展开内容进入本轮上下文保护范围（本轮内不截断），
  本轮结束后照常被 Organization 压缩，下一轮恢复截断头部

---

## 四、参数总表

### 环境变量（进程启动时读取）

| 变量 | 默认 | 作用 |
|---|---|---|
| `WOVRA_MAX_RECENT_ROUNDS` | 3 | 恒定进入上下文的最近轮数 |
| `WOVRA_MAX_HISTORY_TOKENS` | 6000 | 历史加载总预算（Task State 约 1/3 + 选中轮次） |
| `WOVRA_CONTEXT_LIMIT` | 65536 | **当前未生效**（V1 无阈值压缩，占位） |

### 代码常量

| 常量 | 位置 | 默认 | 作用 |
|---|---|---|---|
| `TRUNCATED_LIMIT` | truncate.py | 120 字符 | 索引中每条事件的截断长度 |
| `SAFE_RESULT_LIMIT` | truncate.py | 2000 字符 | 超大工具结果的安全阈值（仅 managed） |
| `STATE_LIST_CAP` | task.py | 20 条 | TaskState 每类列表容量 |
| `_RELEVANCE_MIN_SCORE` | agent.py | 2 | 更早轮次入选的相关性门槛 |
| `_ORGANIZE_MAX_CALLS / _READS` | agent.py | 4 / 3 | 整理的调用与原文展开上限 |
| `max_turns` | Agent 参数 | 10 | 单轮工具循环步数上限 |

---

## 五、首轮对照数据（前 4 轮，同期同任务简报）

| 轮次 | managed：输入/输出/思考/合计/耗时 | baseline：输入/输出/思考/合计/耗时 |
|---|---|---|
| R1 | 6,373 / 4,343 / 3,110 / 10,716 / 134s | 3,712 / 772 / 142 / 4,484 / 43s |
| R2 | 8,148 / 7,897 / 3,044 / 16,045 / 191s | 7,281 / 4,223 / 388 / 11,504 / 93s |
| R3 | 8,187 / 8,228 / 6,740 / 16,415 / 241s | **18,451** / 7,830 / 374 / 26,281 / 144s |
| R4 | **36,304** / 24,606 / 14,617 / 60,910 / 572s | **58,215** / 11,726 / 1,983 / 69,941 / 225s |
| 合计 | 59,012 / 45,074 / 27,511 / 104,086 / 1138s | 87,659 / 24,551 / 2,887 / 112,210 / 505s |

结论：

1. 输入 managed 省 **32.7%**；baseline R4 达 58k，逼近 65536 窗口上限
2. managed 输出反超 +83.6%（思考 27,511 vs 2,887）——**Organization 跑在思考模型上**是主要抵消项
3. 总 token 仅省 **7.2%**；耗时 **2.25 倍**（1138s vs 505s）
4. managed R4 输入尖峰 36,304 需归因（疑似 expand 拉全轮原文 + 大工具输出叠加）

---

## 六、待决策项

| # | 决策 | 建议 |
|---|---|---|
| D1 | Organization 关闭思考（`thinking: disabled`） | **建议做**：预期砍掉大部分 27.5k 思考 token 与一半整理耗时，总省扩大到 25-30% |
| D2 | R4 输入尖峰（36,304）归因与治理 | 先归因（看该轮 events），再决定是否给 expand 累计加预算 |
| D3 | baseline 逼近窗口的行为 | 目前未测到爆点；可选加"接近上限警告" |
| D4 | 相关性匹配召回不足（换说法匹配不上） | 靠 expand_history 兜底，暂不升级匹配算法 |
| D5 | Task State 列表容量 20 是否合适 | 观察实际淘汰频率再定 |
| D6 | Organization 是否换轻量模型 | 需要第二模型端点配置，D1 的低成本替代/补充 |
