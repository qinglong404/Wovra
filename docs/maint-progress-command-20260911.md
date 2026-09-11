# wovra maint：整理与分裂进度查看命令（2026-09-11）

## 背景

Wovra 的上下文维护管线（水位整理 organization + 分裂分析 split）平时对用户
静默：产物暂存、下一轮开启才生效、过程不打扰。运行久了之后用户看不到
"整理进行到哪了、分裂判定是什么、维护批次成败如何"——进度黑盒。

本次新增 `wovra maint` 命令：把整理/分裂进度做成机械渲染视图（与
`report`/`list` 同类，零 LLM 成本，随时可看）。

## 命令

```
wovra maint [task_id]
```

- `task_id`：列表编号（`wovra list` 里的 1、2、3…）或完整任务 id；省略时
  取最近更新的会话（与 list 的排序一致）。
- 任务不存在 → 友好报错（`任务不存在` + 提示用 `wovra list` 查看），不打堆栈。

## 视图内容（全部来自 task.json 的确定性字段，零 LLM）

### 整理（organization）
- 轮次总数与 org_state 分布：已整理（done）／排队中（pending）／失败
  （failed）／未触发（空）；已整理占比。
- 最新整理代次（org_generation 最大值）。
- pending_org 暂存残留轮数（整理产物待下一轮 promote 生效）。
- 水位参考：从 usage 记账（`context=` 字段，与 agent/core.py 落账同格式，
  千分位逗号可解析）取最近一次上下文体量，对比整理触发线
  （WOVRA_ORG_WATERMARK，默认 100,000）。

### 分裂（split）
- 有 split_assessment 的轮次：可分裂/不可分裂判定 + 原因摘要（120 字符截断）。
- 域归属：域数量与名称（前 5 个）、未归属块数（归主 agent）。

### 维护批次（history 记账）
- history 里 kind=="maintenance" 的全部记录逐条列出（时间 + 原文）：
  批次范围、轮数、输入快照条数、硬上限、org/split 成败。

## 实现

- `src/wovra/ui.py`：新增 `maint_view(task)` 机械渲染 + 辅助
  `_org_watermark()`（读环境变量，避免 ui→agent 跨包引用）与
  `_last_context_estimate(task)`（从 usage 记账解析最近一次上下文体量）。
- `src/wovra/cli/main.py`：新增 `cmd_maint` 子命令处理器；argparse 注册
  `maint` 子命令（task_id 可选）。
- `src/wovra/cli/__init__.py`：导出 `cmd_maint`。
- 交互模式不加本地命令：进度用独立命令看，会话内保持 `\report`/`\todo`
  两个机械视图即可。

## 验证

- 新增测试 6 个（tests/test_cli/test_commands.py）：
  - `test_maint_renders_org_distribution`：org_state 分布 + 代次 + 水位参考
    （usage 记账用真实格式 `context=129,611` 验证千分位解析）；
  - `test_maint_shows_split_product_and_batches`：分裂产物（可分裂性/域/
    未归属）+ 批次记账；
  - `test_maint_empty_rounds_friendly`：空任务友好占位（尚无轮次/尚无分裂
    产物/无维护批次）；
  - `test_maint_defaults_to_most_recent_task`：省略 task_id 取最近会话；
  - `test_maint_no_tasks_friendly`：无任务友好提示；
  - `test_maint_missing_task_fails_friendly`：任务不存在友好报错。
- 真实任务实测：`wovra maint 20260911-135751-8ddad1` 渲染正确——24 轮
  88% 已整理、代次 3、水位参考 ≈21,315/100,000、R1 不可分裂（原因 +
  1 个域 + 2 块未归属）、8 条维护批次记账完整。
- 全量测试 318 passed（309 + 9 新增）。

## 备注

- 水位触发线直接读环境变量 `WOVRA_ORG_WATERMARK`，与
  `agent/support.py` 的 `_ORG_WATERMARK_DEFAULT` 同口径，不跨包引用。
- usage 记账的 `context=` 值带千分位逗号（`129,611`），解析时先去掉逗号
  再 int——与 agent/core.py 的落账格式保持一致。
