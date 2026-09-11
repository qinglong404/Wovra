# 运行者体验改进：venv 可用性 + 提示词对账（2026-09-11）

> 触发：执行助手以"运行在 Wovra 上的 agent"身份实测框架，第一轮就撞上
> 自己的安全层拦自己的工具链。本文档是 A+B 两路改进的留痕。

## 0. 一句话

A：`.venv/bin/python` 被安全层拦截（有意行为）但没给出路，现补 `uv run`
提示；B：系统提示词与多处注释里的"单文件时代"过时描述逐一修正。
基线 tag：`pre-improve-baseline`（`4362dbc`）。

## 1. 实测发现的摩擦（运行者第一手）

1. **`.venv/bin/python -m pytest` 被安全层拦截**。uv 建的 venv 解释器
   是指向工作区外系统 Python 的符号链接，命中 `_linked_outside`
   （界内指向界外的链接穿透防护，探针实测的洞）。拦截是**正确且有意**
   的——但拒绝文案只说了"越界"，没给出路，第一次撞上会以为工具坏了。
   实验文档 `experiments/README.md` 自己就推荐 `.venv/bin/python`
   跑安全探针，与安全层自相矛盾。
2. **系统提示词没教模型跑本项目**。`cli/prompt.py` 的 `_system_prompt`
   只写了"环境配置可用 uv / pip / conda"，没说"本项目的解释器由 uv
   管理、要跑命令用 `uv run`"——模型第一次就会去点 `.venv/bin/...`
   撞墙。
3. **多处注释还活在拆分前**。2026-09-11 的重构把 `agent.py`/`cli.py`/
   `tools.py` 拆成了包，但 `prompts.py`/`support.py`/`tools/__init__.py`/
   `safety.py` 的注释仍引用旧单文件名，会误导后续维护者。

## 2. A 路：venv 可用性

### 2.1 `src/wovra/tools/shell.py`
`run_command` 的越界拒绝分支：命令含 `.venv/` 时追加提示——

```
提示：`.venv/bin/...` 被拦是因为 venv 解释器是指向工作区之外的符号链接
——请改用 `uv run ...`（如 `uv run python -m pytest`）。
```

拦截行为不变（安全不松），只补出路。非 `.venv/` 的越界不加此提示，
不污染普通越界信息。

### 2.2 `src/wovra/cli/prompt.py`（系统提示词）
"环境配置"一段追加：本项目的解释器由 uv 管理——跑项目自身的命令用
`uv run`（如 `uv run python -m pytest`、`uv run python -m wovra ...`），
不要直接调 `.venv/bin/...`（会被安全层拦截）。

### 2.3 README
`README.md` 与 `README.zh-CN.md` 各加一节 **Developer Notes / 开发者须知**
（Docs 表之后、Roadmap 之前）：安全层 vs venv 的关系、为什么被拦、
正确用法（`uv run`）、越界要请人代为操作。

### 2.4 测试
`tests/test_tools/test_safety.py` 新增
`test_run_command_venv_python_gets_uv_hint`：造一个指向系统解释器的
`.venv/bin/python` 链接，断言被拦 + 含 `uv run` 提示；普通界外链接
不含该提示。

## 3. B 路：提示词/注释对账

| 文件 | 修正 |
|---|---|
| `src/wovra/agent/prompts.py` | docstring："逐字节自 agent.py 搬运"→"自原单文件 agent 搬运"，补 2026-09-11 重构完成说明 |
| `src/wovra/agent/support.py` | docstring："自 agent.py 原样切出"→ 注明拆分归属 |
| `src/wovra/tools/__init__.py` | 两处注释 `cli.py`→`cli/`、`agent.py`→`agent/core.py` |
| `src/wovra/tools/safety.py` | 白名单注释补注：`.venv/bin/python` 也命中链接拦截，正确用法是 `uv run` |

系统提示词正文其余部分经逐条核对与工具注册清单一致（25+6 常驻工具、
managed 专属 expand_history、本地命令 c/bg/report/todo/undo 等），无
过时描述。

## 4. 验证

```
uv run python -m pytest tests/test_tools/test_safety.py \
  tests/test_tools/test_shell.py tests/test_cli/test_prompt.py -q
# 47 passed

uv run python -m pytest -q
# 285 passed（基线 284 + 新增 1）
```

（CLI 冒烟 `uv run python -m wovra --help` 在基线轮已验证过。）

## 5. 提交与推送

- git 提交：本次改动提交为一条 commit（见提交信息）。
- git push：`git push` 在 `_DENIED_PATTERNS` 黑名单里（对外发布不由
  agent 自主决定）——**由用户在终端手动执行**。

## 6. 已知边界

- 提示词改动会使前缀缓存失效一次（模型可见面，README 已记账惯例）。
- venv 提示按 `.venv/` 子串匹配；若用户用别的 venv 名（如 `env/`），
  仍只会得到通用越界提示——够用，不扩大匹配面以免误报。
