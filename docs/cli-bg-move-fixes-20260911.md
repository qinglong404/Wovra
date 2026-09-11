# CLI 后台命令与 move_file 摩擦修复（cli-bg-move-fixes-20260911）

日期：2026-09-11
状态：已合并、已推送

## 背景

以 Wovra 运行者视角一次性实测三个方向（CLI 交互层、move_file 目录语义、后台任务管理细节），发现两处真实摩擦点：

### 1. `\bg stop <数字>` 编号短写不一致（CLI 交互层 × 后台管理）

`src/wovra/cli/interactive.py` 的本地命令 `\bg` 解析逻辑只对**查看分支**做了数字短写转换：

```python
if args_ and args_[0].isdigit():
    args_[0] = f"bg-{args_[0]}"  # bg 1 == bg bg-1
if len(args_) >= 2 and args_[0].lower() == "stop":
    print(stop_background(args_[1]))   # ← stop 分支不转换！
```

实测：`\bg 1` 正常（转成 `bg-1` 查看输出），但 `\bg stop 1` 落到 `stop_background('1')` 查不到任务，报"未找到后台任务: 1"；而 `\bg stop bg-1` 才正常。这违背 `_LOCAL_HELP` 自己承诺的"任务 id 可只写末尾短串或 bg 编号"——用户用数字短写停止后台任务会**静默失败**。

### 2. move_file 目标为已存在目录（move_file 目录语义）

`src/wovra/tools/files.py` 的 `move_file('f.txt', 'bdir')`（目标是已存在目录）返回"目标已存在，先删除或换名"——但用户本意很可能是"把文件移进该目录"，提示完全没给这条路。与已修的文件工具"可行动提示"哲学同类。

## 修复内容

### interactive.py（`\bg stop` 数字短写）

stop 分支补上同样的数字转换：

```python
if len(args_) >= 2 and args_[0].lower() == "stop":
    target = args_[1]
    if target.isdigit():
        target = f"bg-{target}"  # bg stop 1 == bg stop bg-1
    print(stop_background(target))
```

### files.py（move_file 目录目标提示）

目标已存在且是目录时，识别"用户想移进目录"的意图，给出正确写法（目标内完整路径），而不是只让删/换名：

```python
if dst.is_dir() and not dst.is_symlink():
    hint = str(src.name) if src.name else path.split("/")[-1]
    return (
        f"目标是一个已存在的目录：{new_path}（解析为 {dst}）。"
        f"move_file 不覆盖也不并入目录——要把 {path} 移进该目录，"
        f"请把 new_path 写为目标内的完整路径（如 {new_path}/{hint}）。"
    )
```

## 测试（+2）

- `tests/test_cli/test_commands.py::test_local_command_bg_stop_numeric_id`：`\bg stop 1` 停止 `bg-1`（注入 FakeProc 任务 + 真实 stop_background 路径），断言输出含"已停止"与"bg-1"。
- `tests/test_tools/test_files.py::test_move_file_to_existing_dir_suggests_inner_path`：`move_file('f.txt', 'bdir')` 提示"已存在的目录"+"bdir/f.txt"正确写法，且文件未被移动（move 不覆盖不并入）。

实施备注：测试首版用 `monkeypatch.setattr(tools_module.background, "stop_background", ...)` mock，失败——`interactive.py` 里 `from ..tools import stop_background` 是 from-import 绑定，patch background 模块属性无效；改为注入 FakeProc（带 poll/wait）走真实 stop_background 路径，更贴近实际。

## 验证

- `tests/test_cli/test_commands.py + tests/test_tools/test_files.py`：51 passed
- 全量：**308 passed**（306 + 2 新增）

## 提交

- commit：`CLI \bg stop 编号短写一致（stop 分支补数字转换）+ move_file 目录目标提示（给出移进目录的正确写法）`
- git push origin main（git 破坏性操作走确认门）
