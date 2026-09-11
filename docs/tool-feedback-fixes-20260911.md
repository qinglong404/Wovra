# 文件工具误用反馈修复（tool-feedback-fixes-20260911）

日期：2026-09-11
状态：已合并、已推送

## 背景

以 Wovra 运行者视角实测框架时，发现文件工具对"参数误用"的反馈质量差——而这正是运行者（人类 + AI 模型）真实踩过的：

- 用户 R3：`read_file` 对目录调用抛裸 `IsADirectoryError(21, 'Is a directory')`。
- 助手 R5：`search_files` 的 `directory` 参数误传文件路径，**静默返回"无匹配"**，误导模型以为目录里真的没内容。

框架的核心设计哲学是：**工具返回文本就是模型看到的世界**（`agent/core.py` 把异常包成 `工具执行出错: ...`）。裸 OSError / 静默"无匹配"会让模型白跑一轮甚至产生错误结论。

已有先例：`edit_file` 对不存在文件会给友好提示"文件不存在 + glob_files"；`glob_files` 空结果会提示 include_hidden。但 `read_file` / `list_files` / `search_files` / `glob_files` / `write_file` 没跟上。

**第二轮补充（同日）**：修复完上述五个工具后继续以运行者视角实测变更类工具，发现 `edit_file` / `replace_lines` 同样漏掉——对目录参数在 `read_text` 处抛裸 `IsADirectoryError`（files.py read_text 炸）。`delete_file` 有友好提示，唯独这两个编辑工具没跟上。

## 实测复现（修复前）

| 误用 | 修复前行为 | 问题 |
|---|---|---|
| `read_file('src/wovra')`（目录） | 裸 `IsADirectoryError` | 用户 R3 亲自踩过 |
| `read_file('no-such.md')`（不存在） | 裸 `FileNotFoundError` | 无出路提示 |
| `list_files('no-such-dir')` | 裸 `FileNotFoundError` | 无出路提示 |
| `list_files('app.py')`（文件） | 裸 `NotADirectoryError` | 无出路提示 |
| `write_file('adir', 'x')`（目录） | 裸 `IsADirectoryError` | 无出路提示 |
| `search_files('foo', directory='app.py')` | **静默"无匹配"** | 最误导，助手 R5 踩过 |
| `glob_files('*.py', directory='app.py')` | **静默"无匹配文件"** | 同上 |
| `edit_file('adir', 'a', 'b')`（目录） | 裸 `IsADirectoryError` | 第二轮发现（read_text 处炸） |
| `replace_lines('adir', 1, 2, 'x')`（目录） | 裸 `IsADirectoryError` | 第二轮发现 |

## 修复内容（src/wovra/tools/files.py）

七个文件工具加入"参数误用预检"，把裸 OSError / 静默无匹配转成**可行动的提示**（告诉模型：这是什么错、该换哪个工具、给什么参数）：

- `read_file`：目标是目录 → 提示用 `list_files`/`glob_files`；不存在 → 提示"文件不存在 + 解析路径 + glob_files 出路"。
- `list_files`：目录不存在 → 提示确认路径；目标是文件 → 提示用 `read_file`。
- `search_files`：`directory` 指向文件 → 提示用 `read_file`；`directory` 不存在 → 提示路径。
- `glob_files`：`directory` 指向文件 → 提示用 `read_file`；不存在 → 提示路径。
- `write_file`：目标是目录 → 提示给完整文件路径或用 `list_files` 查看。
- `edit_file`（第二轮）：目标是目录 → 提示用 `list_files`/`write_file`，不再在 read_text 处裸炸。
- `replace_lines`（第二轮）：目标是目录 → 提示用 `list_files`/`write_file`，不再裸炸。

## 关键约束：文案保留 lifecycle/blocks 子串判定

`src/wovra/lifecycle.py` 的 `_OP_FAIL_MARKERS` 用**子串匹配**判断文件操作是否真发生：

```python
_OP_FAIL_MARKERS = ("不存在", "FileNotFoundError", "No such file",
                    "路径越界", "工具执行出错", "拒绝", "是目录")
```

新文案刻意保留 `"不存在"` 与 `"是目录"` 子串——否则 `read_file`/`write_file` 对目录/不存在文件的失败会被误判为成功（幽灵文件分类错误，`test_security_probe.py` 和 `test_safety.py` 专门锁过这条）。实施中确实先写成"目标是一个目录"撞上这个判定，测试失败后改回"路径是目录"。

## 测试（+10）

- `tests/test_tools/test_files.py`：read_file 目录/不存在、list_files 目录不存在/文件目标、write_file 目录目标、edit_file 目录、replace_lines 目录 → 各断言友好提示 + 出路工具名。
- `tests/test_tools/test_search.py`：search_files/glob_files 的 directory 指向文件或不存在 → 断言不再静默"无匹配"，且提示 read_file。

## 验证

- `tests/test_tools/test_files.py + test_search.py`：36 passed
- `tests/test_blocks.py + test_lifecycle.py`（子串判定回归）：26 passed
- 全量：**304 passed**（第一轮 302 = 294 + 8；第二轮再 +2 = edit_file/replace_lines 目录预检）

## 提交

- 第一轮 commit `c3b1a34`：read/list/search/glob/write 参数预检。
- 第二轮 commit：`edit_file/replace_lines 目录参数预检（与 read/write 同约定，裸异常→可行动提示；保留 lifecycle 子串判定）`，已推送 origin/main。
