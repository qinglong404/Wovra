# Wovra 上下文管理 V2 —— 设计定稿（修订版，含评审意见落地）

> 状态：定稿。本文已吸收外部评审（DeepSeek / ChatGPT）的全部有效意见，
> 修订点在文中以 **[修订]** 标注。取代 `context-runtime-experiment.md`。

---

## 0. 一页结论

```text
目标：在缓存定价（命中 = 1/30 价）的现实下，让长会话（几十到几百轮、
     亿级累计 token）的等效输入成本比"全量回放"低 5-10 倍，
     同时保证模型始终知道"历史里有什么、需要时如何找回细节"。

架构 = 四个分离的生命周期：
     事实存储（History，不可变）
     → 视图生成（Organization，后台维护管线）
     → 上下文装配（Assembly，按变化频率排序的缓存友好布局）
     → 按需展开（Expansion，临时提升分辨率，不改历史）

一句话：历史是磁盘上的事实，上下文是按需组装的临时视图，
       视图永远可以丢弃重建，事实永不修改。
```

---

## 1. 设计目标与成本模型

### 1.1 缓存记账规则

模型 API 按前缀缓存计费：与上次请求完全相同的前缀按缓存价（实测
**1/30**）计，不同的尾部按全价。三条定律：

1. **追加免费** 2. **从尾部裁剪免费** 3. **改写中前部 = 从改写点起全部作废**

由此得到 **Context Layout 原则（上下文布局优化）**：

> **把变化频率映射到上下文位置**——越稳定的信息越靠前，
> 越易变的信息越靠后。这比"压缩 token"更本质：少 token 但破缓存，
> 实际更贵（V1 实测：输入省 32.7%，总成本只省 7.2%）。

### 1.2 逐请求成本模型 **[修订]**

按请求逐次累计（不再是"步数 × B"的粗略近似）：

```text
C_effective = Σᵢ ( Uᵢ + Kᵢ/30 )

Uᵢ = 第 i 次请求中未命中缓存的输入 token
Kᵢ = 第 i 次请求中命中缓存的输入 token
```

其中未命中部分包括：每步新增内容、以及**任何**落在改写点之后的内容
（system 变化、历史视图被重组、Task State 更新都会移动改写点）。
这就是 Context Layout 原则的经济学依据。

### 1.3 成本三分法 **[修订]**

```text
C_total = C_working（干活调用）
        + C_organization（后台整理）
        + C_expansion（展开内容的后续携带成本）
```

三部分分开统计（见第 7 节指标），实验才能回答"管理机制自身贵不贵"。

### 1.4 真实案例：第 69 轮（17 步，每步新增 0.5K，基座 B=50K）

```text
第 1 步：U=0.5K（基座 50K 已被上轮缓存）… 第 17 步：U=0.5K，K=前缀
C = Σ Uᵢ + Σ Kᵢ/30 = 8K（新增） + (1/30)×ΣKᵢ ≈ 8K + (1/30)×860K ≈ 38K 等效
```

对照：baseline 同轮（B=770K 全量回放）≈ 446K 等效。**12 倍差距**。
67 轮全程推算：baseline ≈ 1,000 万+ 等效，managed ≈ 200-300 万等效。

### 1.5 首轮 4 轮实测（V1 机制，V2 逐条修正的依据）

| 问题（V1 实测） | V2 修正 |
|---|---|
| 输入省 32.7% 但总成本只省 7.2% | 整理输入降级到 Truncated 层 + 异步化 + 关思考 |
| 耗时 2.25 倍（同步整理阻塞） | 异步 FIFO 维护管线，对话零阻塞 |
| 轮摘要"既要详细又要短"的内在矛盾 | 删除 Round Summary，拆为 Normalized（意图索引）+ RefinedIndex（事件索引） |
| 滑动窗口每轮制造缓存断点 | 近 3 轮全量 + 更早轮次三档降档（预算触发，批量换入） |

---

## 2. 核心概念

### 2.1 Round（轮）与闭合规则

**一轮 = 用户的一条（或合并的多条）输入，到 AI 产出最终回答为止。**

> **只有 AI 产出最终回答，Round 才闭合。** 中断、异常、无回复的尝试
> 都不闭合——期间的所有用户输入**并入同一个开放 Round**（原文各自
> 保留为事件），跨会话持久化。闭合时才进入整理队列。

举例：网络中断，用户连发三条"继续"（AI 均无回复），恢复后 AI 完成
回答——四条输入 + 期间所有事件 = **一个 Round**，整理时合并成一条
澄清意图。空转的尝试不再产生整理成本。

### 2.2 Event 与三层信息

```text
Event
├── id / type / timestamp / status     ← 元数据
├── Truncated：Runtime 程序生成（零 LLM）
│     [R3-E05] 调用 read_file({"path":"src/icp.cpp"}) → 成功
│     [R3-E06] 失败，3 个测试未通过（首个错误：AssertionError…）
└── Full：原始协议消息（事实来源，永不修改）
```

### 2.3 整理产物

```text
Round（闭合后）
├── UserInput.Original    用户原文（可多条）
├── UserInput.Normalized  合并澄清后的意图（AI 生成，全量显示）
├── Events                Truncated + Full
└── RefinedIndex          精修事件索引（AI 重写的事件一行摘要）
```

这构成一个**索引系统**（而非摘要系统）：

> Original = 事实｜Normalized = 用户到底想干什么｜
> RefinedIndex = 这一轮发生了什么｜Full = 如果不信，自己去查

### 2.4 不可变性与修正 **[修订]**

- 闭合后的 Round 内容（含 Normalized/RefinedIndex）**不可自动修正**——
  修正会移动缓存改写点，且收益存疑
- 人工 override 通道保留：直接编辑 task.json，或未来版本的显式命令
- 后续轮次发现旧理解有误时，通过 Task State（decisions/current_status）
  表达最新认知，不改写历史

---

## 3. History Maintenance Pipeline（历史维护管线）**[修订]**

> **Organization 是维护任务，不是用户请求生命周期的一部分。**
> 用户提问 → Agent 立即工作；与此同时后台 Worker 在维护历史。
> 两个生命周期完全分离。V2 的 Worker 只做 Organization，
> 未来可扩展（索引重建、状态清理、缓存预热……）。

### 3.1 触发与执行

Round 闭合后任务进入**后台单线程 FIFO 队列**，主对话立即继续。

- `chat`：退出时队列未清空 → 等待最多 10 秒（带状态行），
  未完成的 Round 标记 pending，**下次加载会话时惰性补跑**
- `run`（一次性进程）：退出前必须等队列清空
- **多进程防护**：会话目录加锁文件，第二个进程打开同一会话时
  显式拒绝（"该会话正在另一个进程中使用"），不做静默单写者假设

### 3.2 输入与输出 **[修订]**

```text
输入：本 Round 的用户输入原文们
    + 全部事件的 Truncated 索引
    + Final Answer 的完整内容（结论、已尝试方案、遗留问题的主要来源，
      截断上限 2000 字符）
输出 JSON：
{
  "normalized_user_input": "合并澄清后的用户意图（可扩写）",
  "refined_index": ["逐事件的一行精修摘要", "..."],
  "state_patch": { "completed": [...], "decisions": [...],
                   "known_issues": [...], "open_questions": [...],
                   "current_status": "...", "goal": "...", "is_done": bool }
}
```

**[修订]** 原则表述为：**默认不读事件原文（Truncated 已够整理意图与
补丁），但当 Truncated 无法确定关键事实时（如失败的具体原因），
可按上限（`_ORGANIZE_MAX_READS = 3`）选择性展开相关 Event 的 Full**。
架构保留后门，默认路径零原文读取。

Organization 调用**关闭思考**（格式化任务，无需推理——V1 实测
思考 token 是成本抵消的主因）。

### 3.3 举例

一轮 17 步的工具调用轮（ICP 调试），Truncated 流为：

```text
[R5-E01] 用户：继续调试 ICP
[R5-E02] 调用 read_file({"path":"src/icp.cpp"}) → 成功
[R5-E03] 调用 run_command("pytest tests/test_icp.py") → 失败
[R5-E04] 调用 edit_file(...) → 成功
[R5-E05] 调用 run_command("pytest tests/test_icp.py") → 成功
```

Final Answer（完整）：误差根源是变换矩阵初值估计不准确，
修正 `initial_guess()` 后 3 个测试全部通过。

整理输出：

```json
{
  "normalized_user_input": "用户要求继续调试 ICP 配准误差问题（衔接上一轮），
     重点是让失败的测试通过",
  "refined_index": [
    "读取 ICP 源码定位误差来源",
    "运行 ICP 测试，3 个用例失败（变换矩阵初值问题）",
    "修改初值估计逻辑",
    "重跑测试全部通过"
  ],
  "state_patch": {
    "completed": ["ICP 测试修复：初值估计修正后全部通过"],
    "known_issues": []
  }
}
```

（"变换矩阵初值问题"来自 Final Answer 完整内容——这就是 3.2 修订
把 Final Answer 纳入输入的原因。）

### 3.4 容错

剥代码围栏 → 取最外层花括号 → 解析失败重试一次 →
仍失败则本 Round 保持 Runtime 视图（Truncated 兜底），状态不更新，
**原始层永远不受影响**。整理调用关闭思考；失败不影响主流程。

### 3.5 Pending Round 的加载 **[修订]**

闭合但整理未完成的 Round（pending），在历史加载时：

- **用 Runtime Truncated 索引临时占位**（零成本、立即可用，
  模型不会"看不到"刚发生的事）
- 整理完成后，在**下一次缓存断点**（见 4.3 批量换入规则）时
  批量替换为精修视图——不单独制造断点

---

## 4. 加载模型（Context Assembly）

### 4.1 排序：变化频率映射到位置

```text
[1] system 人设                      （静态，永不改）
[2] 历史轮次浓缩视图                  （少变：闭合时成形，批量换入，之后不可变）
[3] Task State + 降档轮次的一行索引    （每轮变——放这里）
[4] 当前 Round 的事件                 （追加式）
[5] 新的用户输入                      （追加）
```

**[修订]** 话题表不是独立结构：档 1 的浓缩视图本身携带话题信息
（Normalized 输入），档 3 的一行行就是话题表的极端形式。
第 [3] 位置的"索引"专指**已降档到档 3 的轮次**的集合。

### 4.2 三档分层与预算

对每一轮历史，按以下优先级决定加载档位：

```text
近 3 轮        → 档 0：全部消息原文（单条超长走安全阈值 2000 字符）
其余轮次       → 档 1：用户原文 + Normalized 意图 + RefinedIndex
预算不够       → 档 2：用户原文 + Normalized 意图（去掉事件索引）
再不够         → 档 3：一行话题行（[R12] ICP误差调试（已完成））
永远          → expand_history(ids, level) 可升级到任意档 / 原文
```

- **预算 = 窗口的 30%**（`WOVRA_HISTORY_BUDGET_RATIO`，1M → 30 万
  token），绝对值可用环境变量覆盖。几十轮 × ~700 token/轮 的档 1
  全量加载绰绰有余；**降档只在预算真实吃紧时发生**
- **[修订] 降档排序**：时间优先（最老的先降）+ 状态优先
  （有 open_questions / known_issues 关联的轮次、失败收尾的轮次
  尽量靠后降）；关键词匹配为可选增强，V2 不实现
- **[修订] 话题表/档 3 行**在数百轮后的膨胀是 V3 方向
  （届时按相关性对档 3 再分层），V2 不做

### 4.3 批量换入规则（缓存断点批处理）**[修订]**

三类变化——①降档/升级 ②精修视图换入 ③前缀裁剪——**都不会立即
生效**，而是挂起为"待生效变更"；当任一触发条件满足时（预算越过
阈值、会话重新加载、用户显式刷新）**合并为同一次装配变更**，
即一次缓存断点。两次断点之间是纯追加。

### 4.4 举例：混合话题会话的档位分布

68 轮会话：R1-R3 ICP 调试，R4-R6 UI 修改，R7 闲聊，
R8-R20 视觉算法重构，R21-R68 手眼标定（近 3 轮 = R66-R68）。

```text
[1] system：人设
[2] 档 1（预算内全量）：R1-R65 的浓缩视图
    ├ [R1] 用户：调试 ICP ｜ 意图：修复配准误差 ｜ 索引：5 行
    ├ [R4] 用户：改按钮颜色 ｜ 意图：UI 微调 ｜ 索引：2 行
    ├ [R7] 用户：闲聊大模型 ｜ （无实质事件，视图极短）
    └ ... 共 65 个浓缩视图
[3] Task State（目标/已完成/已知问题…）——当前无降档轮次，无索引块
[4] R66-R68 全量原文
[5] 本轮用户输入
```

一次请求 ≈ 65 × 700 + 状态 1-2K + 近 3 轮全量 + 当前轮 ≈ **50K**。

用户问"之前 ICP 的误差是怎么解决的"——R1-R3 的浓缩视图已在上下文；
需要当时的**具体代码修改**时，模型调用
`expand_history(["R2-E04"], level="full")`。

### 4.5 expand_history 的生命周期 **[修订]**

- `ids`：轮（`R3`）或事件（`R3-E02`），容错逗号字符串与大小写；
  一次可传多个；**无调用次数上限**（每次计入 C_expansion）
- `level`：truncated / summary（轮级：意图+摘要+索引行）/ full
- **生命周期**：展开内容作为工具结果进入**当前 Round**，
  属于当前轮的临时视图——**Round 闭合时随本轮一起被整理**，
  历史存储不变；后续轮次需要时再次 expand
- 展开走尾部追加，缓存友好；不回插历史中间

---

## 5. Task State（增量 Patch）

- 字段：`goal / constraints / decisions / completed / known_issues /
  open_questions / current_status / is_done`
- `state_patch` 增量合并：列表追加去重、每类上限 **200 条**
  （`STATE_LIST_CAP`，超出淘汰最旧，History 可找回）、
  `current_status`/`goal` 覆盖、`is_done` 仅接受布尔
- **[修订] 去重规则**：V2 只做字符串精确去重，不做语义去重
  （观察到实际重复再引入轻量相似度）；淘汰即处理，去重不是关键路径
- 渲染为文本块放在 [3] 位置，渲染预算 = 历史预算的 1/3
- Normalized 输入的修正：闭合后不可自动修正，人工 override 走
  task.json 直改或未来显式命令（见 2.4）

---

## 6. 对照组（baseline）：常规阈值压缩

- 全量追加；累计输入达 **80% × 窗口（1M → 80 万 token）** 时，
  一次 LLM 调用把较早轮次压成摘要 + 保留最近 2-3 轮原文
- 阈值比例（`WOVRA_COMPRESS_THRESHOLD`）与绝对值均可配置
- **[修订] 定位**：80% 只是 baseline 的实验触发阈值，不是本文的
  核心论证；真正的对照是"历史增长后压缩"vs"持续装配的有界工作集
  + 显式展开"两种范式的比较
- 无分层、无 expand、无 Task State 注入；时间线照常给人看

---

## 7. 指标与实验设计 **[新增]**

### 7.1 成本指标（每轮记录于 usage 事件）

```text
input_tokens / cached_input_tokens / uncached_input_tokens
output_tokens / reasoning_tokens
effective_input = uncached + cached/30
cache_hit_ratio = cached / input
分类账：working / organization / expansion 三桶分开累计
latency（本轮墙钟、整理耗时）
```

### 7.2 性能指标

墙钟延迟、Agent 延迟、Organization 延迟、首 token 延迟。

### 7.3 质量指标（重点新增：Historical Recall）

成本之外必须验证质量，否则"删掉历史"无从辩护。核心实验：

> **Historical Recall 测试**：第 5 轮埋入事实（"ICP 初值最终确定为
> X"），第 30 轮追问"当时为什么确定 X？"——对比 baseline 与 Wovra
> 能否找回。Wovra 的核心承诺正是"历史不进上下文，但仍可找回"，
> 此实验直接验证承诺。

其余：任务成功率、正确性、完整性、指令遵循。

### 7.4 实验维度

成本（effective tokens / API 费用）× 性能（延迟）× 质量（召回/成功）
三轴同时比较，任何单轴优势不构成结论。

---

## 8. 参数表

### 环境变量

| 变量 | 默认 | 作用 |
|---|---|---|
| `WOVRA_HISTORY_BUDGET_RATIO` | 0.3 | 历史加载预算 = 窗口 × 此比例 |
| `WOVRA_MAX_RECENT_ROUNDS` | 3 | **Recent Full-Resolution Window**（经验参数，非理论最优） |
| `WOVRA_CONTEXT_LIMIT` | 1,000,000 | 模型窗口大小 |
| `WOVRA_COMPRESS_THRESHOLD` | 0.8 | baseline 压缩触发阈值 |
| `WOVRA_OPEN_ROUND_EVENT_LIMIT` | 50 | 开放 Round 的软限制（见 4.6） |

### 代码常量

| 常量 | 默认 | 作用 |
|---|---|---|
| `TRUNCATED_LIMIT` | 120 字符 | Runtime 截断行长度 |
| `SAFE_RESULT_LIMIT` | 2000 字符 | 单条消息安全阈值 |
| `STATE_LIST_CAP` | 200 条 | TaskState 每类列表容量 |
| `_ORGANIZE_MAX_READS` | 3 | 整理时展开原文次数上限（后门，默认路径不用） |

---

## 9. 其他实现守则

### 9.1 开放 Round 的规模软限制 **[修订]**

开放 Round 是"当前 Round"，全量加载——但反复中断可能积累大量事件。
软限制：开放 Round 事件数超过 `WOVRA_OPEN_ROUND_EVENT_LIMIT`（50）时，
加载时只保留**最近 30 条**全量，更早的事件以 Truncated 行占位
（Full 不变，可展开）。防止一个跨会话的开放 Round 让所有预算失效。

### 9.2 工具截断模板的质量 **[新增]**

RefinedIndex 的质量上限由 Truncated 决定。每种工具的截断规则
（`describe(event)`）必须有测试覆盖关键信息不丢失：
`run_command`（exit_code/错误行）、`read_file`（行数概览）、
`write_file`/`edit_file`（动作与对象）。

### 9.3 单写者防护 **[修订]**

会话目录锁文件；第二个进程打开同一会话时显式拒绝并提示，
不做静默单写者假设。

---

## 10. 与当前实现的差异（实现路线）

1. 轮闭合规则重做（开放轮合并、跨会话持久化）
2. Organization 异步 FIFO 队列 + 输入含 Final Answer + 关思考
   + 输出含 refined_index（删除 round_summary）
3. 装配重写：近 3 轮全量（档 0）+ 三档自动降档（窗口 30% 预算，
   时间+状态排序）+ Task State/降档索引移至 [3] 位置 + 批量换入
4. baseline 80% × 窗口阈值压缩
5. 指标落地：usage 事件记录三分账（working/organization/expansion）、
   CHR、effective tokens
6. 会话锁文件、开放轮软限制、截断模板测试
7. 保留不变：审计、破坏性防护、展示层行纪律、prompt_toolkit 输入

---

## 11. 已知限制与 V3 方向

* 多进程并发写（锁文件拒绝而非合并）
* 档 3 行在数百轮后的膨胀（届时对档 3 再分层）
* 语义检索缺失（换说法的召回靠 Normalized 全量在档内缓解）
* 步数失控的单轮预算（V3）
* 关键词相关性增强（V3 可选）
