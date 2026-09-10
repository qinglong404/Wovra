# 分裂方案 v1（定稿，2026-09-10）

> 状态：Level 0（只分析不分裂）已实现并实测通过。
> 代码：`agent.py` 的 `_split_rounds` / `_split_hard_data` / `_SPLIT_INSTRUCTIONS` /
> `_ORG_DOMAINS_SCHEMA`。Level 1（分裂执行）未做——产物落 `domains` /
> `thoughts` / `split_assessment` / `proposal`，是未来分裂执行的输入。

## 管线：整理 → 分裂，串行纯追加

```
水位触发 → 维护线程（单线程）：
  org 阶段：base 快照 + org 指令 → submit_organization
    ├─ 失败：批 org_state=failed，split 跳过（分裂依赖整理质量）
    └─ 成功：交换记录 (messages, content, ordered) 传给 split
  split 阶段：org messages 纯追加 assistant(提交调用) + tool 结果 + 分裂指令
    → submit_domains
```

**缓存设计**（实测）：split 是 org 的纯追加延续——org 刚跑完 KV 全热，
分裂白得完整整理产物，只付自己的指令。实测新端点：

```
[organization] prompt=40,623 cached=40,448（99.6%）completion=37,671
[split]        prompt=52,060 cached=40,576              completion=26,236
                └─ 40,623 + 追加 11,437（org 产物 + 分裂指令）
```

- org / split 各自收窄工具集（只留 submit_organization / submit_domains）
- 两边都开思考（实测：不开思考不调工具且质量低）
- 不重试（同 org：重试只是再付一遍生成）

## 分裂判据（三步走，LLM 只做语义判断）

输入里 Runtime 注入**硬数据**（零 LLM）：活性文件清单（路径+状态+块引用）
+ 活性文件数（分裂单元上限）。LLM 只判断语义：

```
1. 保底块归属（thoughts）：无文件交互的纯聊天块逐个判断——
   相关 → related_domain；独立思想 → 留空，归主 agent
2. 文件域聚合（domains）：按**功能语义**（不是路径！路径只是线索，
   同目录可属不同域、不同目录可属同域）→ 域树，parent 表达层级；
   每域必填 description（供路由与多 agent 交流对象选择）
3. 分裂判定：找**能形成有效分裂的最浅层**——顶层 ≥2 节点用顶层；
   顶层 1 个节点但有 ≥2 子节点用该子层；全链单子不可分。
   候选单元数 ≤ 活性文件数上限
```

## 输出契约（submit_domains）

| 字段 | 内容 |
|---|---|
| thoughts | 保底块归属（block_id / related_domain / reason）——独立思想归主 agent |
| domains | 域数组：name / **description** / parent / file_domains / constraints / goal / block_ids / superseded |
| unassigned | 不属于任何域的块（兼容旧字段） |
| split_assessment | splittable / reason / proposal（units = 候选分裂单元，含 description） |

## 实测结果（R1-R28，第一次水位触发点）

两种粒度模型都给出、且都成立（判据稳，非随机）：

| 模型 | 域树 | 判断 |
|---|---|---|
| deepseek-v4-flash | 2 域（文档域含 2 子域 + 前端域） | 用户认可 |
| deepseek-v4.1-flash | 3 域（工具层测试 / 机制评判 / 前端，均顶层） | 用户认可 |

共同点（关键正确性证据）：
- 独立思想正确摘出（R1-2 寒暄 / R3 猜下一步 → 主 agent）
- R5（越狱承认）关联到工具层域（"指向路径约束不一致，属该域的问题发现线"）
- 前端域在 R28 出现后正确识别为第二个独立文件域
- v4.1-flash 主动纠正了 R13 的旧判断："那次的依据是'都落在 agent-test 目录'
  ——属路径导向的合并（新指令明确纠正）"

## 新模型注意点（deepseek-v4.1-flash，experientiallabs 端点）

- **思考内容不流式返回**：reasoning 全在首 token 前完成，最后以
  `completion_tokens_details.reasoning_tokens` 报账——org 的 TTFT
  实测 120s（思考时长），之后输出很快。不要误判为挂流。
- 缓存正常（99.6% 命中）。
- 需要更长耐心：单批 org ~150s、split ~115s（思考不计入流式等待）。
- 调试脚本用 `python -u`（重定向到文件时块缓冲会让人误以为卡死）。

## Level 1（未来）

分裂执行 = 按 `proposal.units` 把块/文件域分给子 agent：
- 主 agent = 独立思想块 + 未分裂出去的剩余
- 子 agent 上下文按块 ID 快速拼装（block_refs 跨轮索引已有）
- 子 agent 再满时：在它的子树里重跑"最浅可分层"算法
