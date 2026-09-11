# 安全层误伤修复 + 越界授权机制（2026-09-11）

## 背景

运行者在给框架做改进时，`git commit -m "…prompts.py / support.py / … .venv/bin/python …"`
整条命令被安全层拦下，报"访问工作区之外的绝对路径（/）"。`scripts/probe_cmd_escape.py`
逐项复现定位出两个误伤，并发现第三个（cd 到工作区本身）。

同时用户拍板：**跨工作区访问不再一律拒绝**——先请求用户授权一次，授权后自动放行，
否则"很麻烦"（原话）。

## 误伤修复（safety.py）

| # | 误伤 | 根因 | 修复 |
|---|------|------|------|
| 1 | 文本分隔符 `prompts.py / support.py`、`echo a / b` 被判为访问根目录 `/` | 孤立 `/` 检测只看"前后是空格" | `_has_root_slash_target`：孤立 `/` 只有紧跟**命令词或选项**（`find /`、`ls -la /`）或赋值（`root=/`）才是根目录访问；普通 token 间的 `/` 是分隔符/除法，放行 |
| 2 | commit message 引号文本里的 `.venv/bin/...`（仅引用非执行）被当链接穿透拦 | `_linked_outside` 对引号内的 token 也做链接解析 | `_mask_quoted_text`：把**数据引号段**（echo 参数、`-m` 消息、grep 正则）掩码为占位符再检测；**代码解释器**（bash/sh/python/perl/ruby/node 等）的 `-c/-e` 段是真实执行代码，必须保留给检测器（perl `-e` 的教训：探针看门测试 test_security_probe.py 抓出首版只保护 `-c` 导致 perl 泄漏） |
| 3 | `cd /home/.../Wovra`（工作区本身绝对路径）被拦 | cd 判定只认目标形如 `..`/`/`/`~`，`/` 匹配任意绝对路径 | `_command_escape_targets` 先 `_extract_cd_targets` 归一化，目标全部落在工作区内则放行；`cd /tmp`、`cd ..` 仍拦 |

结构改动：`_command_escape` 重构为 `_command_escape_targets`（返回 `(原因, 越界目标列表)`），
目标列表供授权门使用；原 `_command_escape` 保持兼容。

## 越界授权机制（用户拍板）

**原则**：越界不再一律拒绝——交互环境向用户请求授权一次（y/N），授权路径写入
`.wovra/authorized-paths.json`（gitignored，机器本地状态；与版本归档同目录），
之后访问自动放行；**非交互环境安全拒绝**（越界不是普通敏感操作，绝不自动放行——
`_ask_yes_no` 对脚本自动放行是怕阻塞实验，授权自动放行等于静默打开工作区边界）。

- 授权粒度：**单文件**（精确匹配）或**目录**（前缀匹配 = 其下全部内容）
- 接入通道：
  - `run_command` / `run_background`：`_command_escape_targets` 的越界目标 → `_request_path_authorization`
  - 文件工具：`_safe_write_path` / `_safe_directory` 的界外链接分支 → 授权后放行
    （read/write/edit/replace/restore/list/search/glob 全部经此）
  - 遍历通道 `_walk`：起点为已授权目录时，边界从工作区切换到授权目录本身
    （授权目录内部文件正常产出，不再被"越界即跳过"过滤）
- 配套：
  - `_history_slot` 对界外授权文件返回 None（不纳入工作区版本档案，写入照常）
  - `_display_rel`：授权目录内文件在 search/glob 输出中回退为绝对路径
    （read_file 只收工作区相对路径，模型改用 run_command 访问）
  - `restore_file` 对界外授权文件明确提示不支持

## 验证

- 探针 `scripts/probe_cmd_escape.py` 更新为回归用例：3 个误伤不再拦、真实越界
  （`find /`、`ls /`、`cat /etc/passwd`、`cd /tmp`、`cd ..`）仍拦
- 新增测试（tests/test_tools/test_safety.py）：
  - `test_escape_detection_false_positives_fixed`：误伤修复 + 真实越界对照
  - `test_escape_authorization_flow`：非交互拒绝 → 交互授权 → 放行+落盘 → 持久化 → 未授权仍拦
  - `test_file_tools_authorization_flow`：文件工具界外链接授权后可读可写，未授权仍拒
- 探针看门测试 `test_security_probe.py` 全过（perl `-e` 泄漏被修复后恢复）
- 全量：**288 passed**（基线 285 + 新增 3）

## 文件清单

- `src/wovra/tools/safety.py`：误伤修复 + 授权基础设施 + 授权门
- `src/wovra/tools/shell.py`：run_command 授权接入
- `src/wovra/tools/background.py`：run_background 授权接入
- `src/wovra/tools/files.py`：文件工具/遍历通道授权接入 + 版本档案/展示容错
- `scripts/probe_cmd_escape.py`：探针回归用例
- `tests/test_tools/test_safety.py`：3 个新测试
- 本文档

## 备注

- 授权清单存 `.wovra/authorized-paths.json`（绝对路径 = 机器本地状态，不进版本库）
- 提交推送为敏感操作（`git push` 在黑名单），由用户确认后手动执行
