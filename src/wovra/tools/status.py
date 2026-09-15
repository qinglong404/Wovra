"""工具结果成败判定：**唯一权威口径**（纯函数，零 LLM，确定性）。

## 为什么要有这一个模块

"这次调用成没成"是一份**事实**，但此前代码里四处各自猜文本，口径互不相同：

```text
tools/safety.FAILURE_MARKERS    全文子串          → 读源码必中招
webui/index.html::resultStatus  开头锚定 5 个词    → "工具执行出错:"/"截图失败：" 判成成功
lifecycle.op_failed             首行前缀          → 覆盖不全（二进制读取算成功）
blocks/segment._op_failure      首行前缀          → 同上，且自己另维护一份表
```

实测（`scripts/probe_tool_status.py`；tasks/ 全量 2541 条 tool_result，2026-09-14）：
全文子串口径与首行金标准分歧 **75 条**、前端口径分歧 **132 条**，其中
**假阳性 42 条**——样本全是"成功的 `read_file` 读到了本项目源码"（正文里
当然含 `工具执行出错` / `命令执行失败（` 这些字面量，被当成失败证据）。
用户报障原话（2026-09-14）："工具消息块对成功/失败的判断太草率了…老是判断做"。

## 口径（三条规则，按序执行）

1. **只看首行**，绝不扫全文——正文里可以出现任何字符串（README、源码、
   日志里什么都有）。这是 2026-09-11「幽灵误判」与本次 42 条假阳性的同一病根。
2. **结构化信号优先**：首行带 `exit_code=N`（`run_command` / `check_background`
   的固定头）就按它判——`N==0` 成功、否则失败。比任何文案匹配都硬。
3. 首行命中**拒绝表** → `deny`；命中**失败表** → `error`；都不命中 → `ok`。

## 为什么是三分类而不是布尔

`deny`（系统/用户按策略拒绝执行：危险命令、越界、权限、用户否决、钩子拦截、
SSRF）与 `error`（执行了但失败）对用户是两件事——"被挡住"要改做法，"出错"要
修问题。两者的**并集**才是"没成"（`failed()`）。CLI 与前端据此分色。

## 表的来源（不许想当然）

每一条都取自 `tools/*.py` 的失败分支实文 + 语料核对（脚本会打印首行形态
分布）；新增失败文案时把首词补进表即可，**判定只在这里改一处**。

## 消费方（全部委托本模块，禁止各自再写一套）

`truncate`（Event.status / 索引行）、`ui`（CLI 成/败/拒绝行）、`task`（报告
"失败："前缀）、`lifecycle.op_failed`（文件账本）、`blocks/segment`（幽灵/越界
标签）、`webui/index.html::resultStatus`（工具卡状态徽标）；跨语言一致性由
共享夹具 `tests/fixtures/tool_status_cases.json` 钉住（Python 与 node 两侧
跑同一份用例）。
"""

from __future__ import annotations

import re

OK = "ok"
ERROR = "error"
DENY = "deny"

LABELS = {OK: "成功", ERROR: "失败", DENY: "拒绝"}

# ---- 结构化信号 -------------------------------------------------------------

# run_command / check_background 的结果头固定带 exit_code：
#   "exit_code=0（耗时 1.2s）"  /  "命令执行失败（exit_code=1，耗时 0.0s）"
#   "[bg-1] 已退出（exit_code=137）"
EXIT_CODE_RE = re.compile(r"\bexit_code=(-?\d+)")

# task.py 的历史 detail 形如 "工具名 -> 结果" / "工具名(参数) -> 结果"
# （只有内部记录会带这层包装，工具本身返回的文本不带）；判定前先剥掉，
# 否则首行锚定会全部落空。参数里可能含中文与引号，故用非贪婪括号配平。
_WRAPPER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*(?:\([^\n]*?\))? -> ")

# ---- 拒绝表（deny）：系统/用户按策略挡住了，操作没发生 ----------------------

DENY_PREFIXES = (
    "已拒绝执行",          # shell/background：危险命令、越界命令
    "用户拒绝了",          # files 删除/移动、shell 确认门（用户点否）
    "用户取消了",
    "权限拒绝：",          # agent/core._file_permission（别人的文件只读）
    "被用户钩子拦截：",     # interaction.run_pre_hook
    "拒绝访问内网",        # web 的 SSRF 防护
    "路径越界，",          # safety 路径防护（工作区之外）
)

DENY_INFIXES = (
    "位于工作区之外",       # restore_file 只支持工作区内的版本档案
    "请在那个会话中管理",    # background 跨会话管理防护
    "不接受 file://",       # eyes：只收 http(s) 或工作区内相对路径
)

# ---- 失败表（error）：执行了但没成 ------------------------------------------

ERROR_PREFIXES = (
    "工具执行出错:",        # agent/core._invoke_tool 异常兜底
    "未知工具:",            # agent/core
    "工具参数不是合法 JSON",  # agent/core（参数被截断）
    "文件不存在:",          # files：read/write/edit/replace/delete/move
    "目录不存在:",          # files：list/search/glob
    "命令执行失败（",        # shell：非零退出 / 超时强杀
    "抓取失败:",            # web
    "duckduckgo 失败",      # web
    "bing 失败",            # web
    "所有搜索通道都失败了",   # web
    "仅支持 http/https",     # web 参数校验
    "URL 缺少主机名",        # web
    "无法解析主机",          # web（DNS）
    "URL 无文本内容",        # web
    "截图失败：",           # eyes
    "启动浏览器失败:",       # eyes
    "截图超时（",           # eyes
    "截图未产出文件",        # eyes
    "看图失败：",           # eyes
    "target 为空：",        # eyes
    "未找到后台任务:",       # background
    "未找到 agent：",        # ledger：路由/协作找不到对象
    "未找到工作项：",        # ledger：todo
    "无进行中的阶段",        # ledger：todo 误用（未 start_stage）
    "未知动作:",            # ledger：todo
    "正则表达式无效:",       # files：search_files
    "目标已存在:",          # files：move 不覆盖
    "版本前缀",             # files：restore 版本前缀歧义
    "route_to：",           # ledger：转交被拒（目标是自己/已转过/超跳数）
    "consult：",            # ledger
    "notify：",             # ledger
    "join_with：",          # ledger
    "update_responsibility：",  # ledger
    "⚠ 防呆拦截：",          # files：write_file 大幅缩小防呆
)

ERROR_INFIXES = (
    "而非目录",             # files：给文件当目录用（list/search/glob 预检）
    "是目录而非",           # files：read/write/edit/replace/delete 的预检
    "是目录（而非",          # files：restore_file
    "是目录：",             # eyes：截图目标是目录
    "无法按文本读取",        # files：二进制文件（此前被算成"读成功"）
    "超出范围",             # files：start_line 越界
    "已被外部修改",          # files：写入/编辑前的过期防护
    "不是 UTF-8 文本文件",    # files
    "没有历史版本",          # files：restore
    "已发送终止信号但未退出",  # background：强杀没死透
    "被拒：",               # ledger：todo verify 等约束性拒绝
    "超过 3 条硬上限",       # ledger：todo acceptance 条数
    "需要 text",            # ledger：todo 参数缺失
    "需要 reason（",         # ledger：drop_stage
    "需要 evidence（",       # ledger：verify_stage
    "需要 goal 与 acceptance",  # ledger：start_stage
)

# ---- 专项标记（供块标签与文件账本做二级分类，同表同源） ----------------------

# "幽灵"：读/删的目标根本不存在（从未存在）
NOT_FOUND_MARKERS = ("文件不存在:", "目录不存在:", "FileNotFoundError", "No such file")

# "越界"：safety 拦下的路径（首行形如 `工具执行出错: ValueError('路径越界，…')`）
ESCAPE_MARKERS = ("路径越界，",)


def first_line(content: str) -> str:
    """结果文本的首行（剥掉 task 内部记录用的 `工具名 -> ` 包装）。"""
    text = (content or "").lstrip("\ufeff \t\r\n")
    head = text.split("\n", 1)[0]
    return _WRAPPER_RE.sub("", head, count=1)


def classify(content: str) -> str:
    """工具结果 → `ok` / `error` / `deny`（见模块 docstring 的三条规则）。"""
    head = first_line(content)
    if not head:
        return OK
    matched = EXIT_CODE_RE.search(head)
    if matched is not None:
        return OK if int(matched.group(1)) == 0 else ERROR
    if head.startswith(DENY_PREFIXES) or any(m in head for m in DENY_INFIXES):
        return DENY
    if head.startswith(ERROR_PREFIXES) or any(m in head for m in ERROR_INFIXES):
        return ERROR
    return OK


def failed(content: str) -> bool:
    """是否"没成"（error 与 deny 的并集）。"""
    return classify(content) != OK


def denied(content: str) -> bool:
    return classify(content) == DENY


def label(content_or_status: str) -> str:
    """给人看的一字状态（'成功' / '失败' / '拒绝'）。"""
    status = content_or_status if content_or_status in LABELS else classify(content_or_status)
    return LABELS.get(status, LABELS[OK])
