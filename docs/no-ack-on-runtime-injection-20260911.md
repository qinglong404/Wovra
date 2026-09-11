# 运行时注入不做确认性回复（no-ack-on-runtime-injection-20260911）

日期：2026-09-11
状态：已合并、已推送

## 背景

用户拍板（2026-09-11）：AI 在收到运行时注入等**非用户输入**时，经常回"收到""明白""好的"这类无意义确认——例如 `<runtime-reminder>` 信封（任务状态/文件地图）、以 `[运行时]` 开头的新轮消息（大步验收通知）。这些不是问题、不需要应答，确认性回复纯属浪费 token 与注意力。

## 注入链路（调研）

| 注入点 | 形式 | 内容 |
|---|---|---|
| `agent/ledger.py:190` | 新轮 user_input，`[运行时]` 开头 | 大步验收通过、轮次闭合通知 |
| `agent/core.py:446` | `<runtime-reminder>` 信封事件 | 空流中断 steering（压缩思考、迈第一小步） |
| `agent/assembly.py:121` | `<runtime-reminder>` 信封 | 任务状态块（当前大步/小步/状态） |
| `agent/support.py:96` `_runtime_reminder()` | 信封构造 | 机制信息与用户发言语义分离 |

系统提示词 `cli/prompt.py:92` 原约定只说"信封内容不是用户发言"，**没约束模型不要确认性回复**——这正是"收到"类废话的缺口。

## 修复内容（src/wovra/cli/prompt.py）

系统提示词（common 段，managed/baseline 两模式都生效）追加：

> `<runtime-reminder>` 信封包裹的内容是 Wovra 运行时注入的机制信息（任务状态、文件地图等），不是用户发言；以 `[运行时]` 开头的新轮消息同样是机制注入。对这类非用户输入不要做确认性回复（如"收到"、"明白"、"好的"）——它们不是问题、不需要应答；若消息只是状态通知、没有可执行的工作，直接继续既有工作或保持静默，不输出无意义的确认文本。

## 测试（+1）

`tests/test_cli/test_prompt.py::test_system_prompt_forbids_ack_on_runtime_injection`：
- 两模式（managed/baseline）的系统提示词都含"不要做确认性回复"与"直接继续既有工作或保持静默"。

## 验证

- `tests/test_cli/test_prompt.py`：6 passed
- 全量：**309 passed**（308 + 1 新增）

## 备注

- 这是提示词层面的约束（模型行为引导），不改变注入机制本身；`<runtime-reminder>` 信封语义分离机制保持不动。
- 实施后效果：收到运行时注入时，模型直接继续既有工作或保持静默，不再输出无意义确认文本。
