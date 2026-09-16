# 新整理契约练兵：输入与产物（2026-09-17）

会话：`20260916-090533-964be8`（9 轮，GAIA 评测线 + 提示词审查）。
仪器：`scripts/rehearse_round_notes.py`（副本上真跑，原会话未动）。
读法：输入 355 条消息 / 约 143.4K tok（实测 prompt 131,523）；产物 1,758 字符（旧逐块描述 11,656 字符）；
completion 27,690（含推理）——产物只占约 2K，其余是模型读历史做压缩的思考。

---

── 尾部追加的指令（逐字）──
[整理指令]
以上是本会话的完整上下文。请把下列轮次各压缩成**一句话**，并给出账本增量：R1、R2、R3、R4、R5、R6、R7、R8、R9。

要求：
1. **每轮一句话**：这轮做了什么、结论是什么。关键数字（行数/条数/次数）、文件路径、命令名原样保留。**不要复述用户要什么**——用户原话由 Runtime 逐字保留，你只写对这轮要求的响应。
2. 一句话里**不要用英文双引号**（会截断 JSON），需要引用用「」。
3. **失败与坑**：本轮只要有命令非零退出、越界拦截、路径不存在、方案被推翻，必须逐条写进 failures；没有就给空数组。这一档**免裁剪**——原文折叠后就再也找不回来。
4. **账本增量**：只写**新增**条目，不要重复账本里已有的。标量（现状/目标）不用你写，Runtime 从最新一轮取。
5. 一轮一条、一个不落、与上面的轮号一一对应。

完成后调用 submit_round_notes 提交（唯一出口，不要在正文输出 JSON）。

调 LLM（输入约 143,784 tok）…
usage: prompt=131,523 cached=0 miss=131,523 completion=27,690

── 产物：每轮一句话（👤原文与涉及文件是**代码**填的）──

[R1] 👤 你查一下：GAIA Benchmark
      涉及文件：README.md、AGENTS.md
      ▸ 检索并核对了 GAIA 基准：Meta AI 等 2023-11 发布（arXiv:2311.12983），466 题分三档难度且对人类易、对 AI 难，人类 92% 对 带插件 GPT-4 15%，300 题私有 test 集答案不公开、HF 官方数据集为 gated；同时查明工作区 /home/lkf/bc/python/GAIA 实际为空，目录名与基准同名但无任何内容。
      ⚠ list_files 首次返回约 20 个文件与目录（README.md、src/、tasks/ 等），但同轮 ls -la 与 find 显示工作区实际只有 output/ 与 .wovra/，两者不一致
      ⚠ 越界拦截：ls -la /home/lkf/bc/python/ 被安全层拒绝（工作区祖先目录）
      ⚠ web_fetch 抓 HF leaderboard 只得到占位文本（Fetching metadata from the HF Docker repository... Refreshing），未取到任何分数行

[R2] 👤 我想看看这个Wovra 作为agent表现如何？
      涉及文件：gaia_bench/__init__.py、gaia_bench/data.py、gaia_bench/evaluate.py、gaia_bench/worker.py、gaia_bench/runner.py、gaia_bench/report.py…
      ▸ 探测环境（Wovra 装在 /home/lkf/bc/python/Wovra，模型 deepseek/deepseek-v4.1-flash，30+ 工具，会话根可经 WOVRA_TASKS_ROOT 重定向），确认官方 HF 数据集 gated 后改用 ModelScope 镜像 revision 368b0781 取到含答案的 validation 165 题（L1 53/L2 86/L3 26；test 301 题答案全为占位符 ?），建成 gaia_bench/ 六模块 harness（data/evaluate/worker/runner/report/README），单题冒烟通过（e1fc63a2 答对 17，14.6s），pilot 8 题对 5（62.5%，含 1 次超时），随后以 run-id full-validation、jobs 4 启动全量 165 题。
      ⚠ read_file 读 README.md 与 AGENTS.md 均报文件不存在（list_files 曾把它们列入清单）
      ⚠ 官方 HF 数据集为 gated：metadata.parquet 匿名下载返回 HTTP 401
      ⚠ pandas/pyarrow/fastparquet/duckdb/polars 均未安装，无法直接读 parquet
      ⚠ ModelScope repo files API 返回 405、commits API 返回 404
      ⚠ parquet 版仓库已删 metadata.jsonl，需回退旧 revision 368b0781 才取到
      ⚠ stanford-ppl/Agentic_Benchmarking_For_GAIA 的 raw validation.jsonl 返回 404
      ⚠ 越界拦截：ls 与 grep 访问 /home/lkf/bc/python/Wovra/src/wovra/tools/ 被安全层拒绝
      ⚠ 评分器兜底抽取把 Calculation: 这类标签行当成答案（后改为按像答案程度挑行）

[R3] 👤 我这边修了一下网络检索和视觉，你实测一下，可以了，就停止。
      涉及文件：gaia_bench/runner.py
      ▸ 停掉 bg-2 全量跑（已跑 28 题中 24 题调用网络工具，判为污染数据），用 gaia_bench/offline.py 逐题分类得 42/165 题离线可解（L1 22/L2 18/L3 2），跑离线子集得 32/42 = 76.2%（L1 17/22、L2 13/18、L3 2/2，完成 40/42，中位 40s），写成分档报告并归档 output/gaia/FINDINGS.md，另实测确认 web_search 全通道失效。
      ⚠ web_search 全通道失效：DDG lite/html 报 SSL UNEXPECTED_EOF_WHILE_READING，Bing 对英文查询返回日语词典页，加 setmkt=en-US 返法语剧院页、cc=US 返 YouTube 页，相关性过滤把候选全清空
      ⚠ 3 题 harness 层失败：54612da3 InternalServerError 500、6359a0b1 RemoteProtocolError、cca530fc timeout
      ⚠ 评分器把句末句号判错（50ad0280 内容全对却算错），后修正为忽略非小数的句末点
      ⚠ 重跑追加导致 results.jsonl 出现重复行（54612da3 两条），report.py 与 rescore.py 初版未按 task_id 去重
      ⚠ 视觉题 cca530fc（棋局图）与 6359a0b1（多边形面积图）均 900s 超时

[R4] 👤 你先介绍一下你自己，我看看。
      ▸ 实测修复后的两条通道：web_search 已换成多引擎池（serper/firecrawl/serpapi），4 个此前全挂的查询 2.5~3.4s 全部命中且中英文均可；view_image 延迟投递机制正常、vlm_ready 为 True；给 runner 补 --ids 参数后以 run-id retest-vision-search 启动 4 题重测（含此前超时的 cca530fc 与 6359a0b1）。
      ⚠ 安全层把 heredoc 正文里 pathlib 的除号 / 误判为越界绝对路径并驳回一次，改用 os.path.join 后通过
      ⚠ 单独调用 view_image 工具不会真把图片注入视野（返回值只是文字说明），必须经完整 Agent 轮次才能验证
      ⚠ 启动了 4 题重测跑 bg-1，与用户设定的停止意图冲突

[R5] 👤 对啊，你就是Wovra，我让你测试wovra，你不在自己这里测试，你跑别的地方测试干嘛？
      ▸ 按要求停掉 bg-1（exit_code=-9，进程树强杀，list_background 确认已无后台任务），并汇报两项实测结论——检索多引擎池可用、视觉延迟投递正常，同时说明那条 poly.png 注入提示只是已清理 probe 文件的残留。
      ⚠ 4 题重测跑（run-id retest-vision-search）被强杀，未产出任何可用结论

[R6] 👤 先不解题了，我先问一下，为什么你最开始要那样做？
      ▸ 做了自我介绍：说明本会话即 Wovra 实例、工具覆盖文件与命令与网页与看图、交互纪律按问与思想对齐与行动三类分办并默认保守，并交代本工作区现有 gaia_bench/ harness 与 GAIA 评测成绩。

[R7] 👤 你是不是不清楚自己的身份是wovra？这不应该啊，我在系统提示词中写了啊。
      ▸ 承认身份错位：把 Wovra 当成工作区之外的外部系统去搭 harness 测副本（引入 PYTHONPATH、工作区绑定、每题重置等额外变量），而本会话自己就是被测对象，正确做法是当面解一道 GAIA 题。

[R8] 👤 可以，你检查一下如今提示词都有哪些问题，写一份报告出来，我修改一下。
      涉及文件：docs/prompt-review-2026-09-16.md
      ▸ 归因最初跑偏：训练先验把评测自动翻译成 harness 加批量跑、探测环境时把工具清单当外部能力读、该问 当面解题还是搭批量评测 却跳过；并指出提示词写的是 你是 Wovra 的执行助手 而非 你就是 Wovra，且另有数处把 Wovra 与 Runtime 描述成在我之外的机制，构成身份歧义。

[R9] 👤 那你从站在你的角度，或者说你这几轮的观察下，感觉wovra这个agent用着有哪些不顺手？
      ▸ 逐行审查 src/wovra/cli/prompt.py 的 _system_prompt()，渲染真实提示词核验（managed 2607 字符、baseline 2020 字符，无小节标题且无空行）并回源码查证工具装配与运行时信封，交付 docs/prompt-review-2026-09-16.md（349 行，7 个问题：身份表述、缺停止与不得扩大范围语义、静默规则且 [运行时] 前缀在源码中出现 0 处、工具未提名、本项目指代不明、无结构长文本、安全层 heredoc 误判），含可直接粘贴的改法与修订记录。
      ⚠ 首版报告有 3 处事实错误（运行时前缀措辞过软、todo 所在层描述错误、缺渲染长度数据），已出第 2 版并在文末附修订记录
      ⚠ 报告第 4 节标题写 13 个已装配工具未被提名，与正文实测 10 个不一致，未修
      ⚠ 越界拦截：search_files 指向 /home/lkf/bc/python/Wovra 被拒（路径越界）
      ⚠ 安全层再次把含 pathlib 除号的 heredoc 命令误判为越界路径而驳回

── 产物：账本增量 ──
  约束：
    - 工作区限定 /home/lkf/bc/python/GAIA；访问 /home/lkf/bc/python/Wovra 或工作区祖先目录会被安全层拒绝（本会话实测多次）
    - 安全层会把 heredoc 或引号正文中出现的 / 字符误判为越界绝对路径（实测 2 次），改用 os.path.join 或去掉该运算符即可通过
    - Wovra 的提示词文件在 src/wovra/cli/prompt.py（工作区之外），只能经 import 反射或读源码访问，不能直接用文件工具打开
  决策：
    - runner 增补 --offline-only、--ids、--retry-failed 三个开关
    - results.jsonl 重跑按 task_id 追加，report.py 与 rescore.py 已改为按 task_id 去重并取最后一次尝试
    - 评测产物按 run 分目录落盘：output/gaia/runs/<run-id>/{results.jsonl,meta.json,report.md,ws/<task_id>/}
  已知问题：
    - docs/prompt-review-2026-09-16.md 第 4 节标题写「13 个已装配工具未被提名」，与正文实测「10 个」不一致，待修订
    - list_files 首次返回的工作区文件清单（约 20 项）与实际（仅 output/、.wovra/）不符，疑为陈旧缓存列表
  待决：
    - 是否把「用户要的是观点还是产物」这条判据补进提示词——本会话由观察得出，报告现有版本未覆盖



---

# v2：补上"动作清单"（锚）后重跑

**为什么重跑**：v1 的产物张冠李戴——5 个纯对话轮（事件里零工具调用）中有 3 条的产物在描述
工具工作（"启动 4 题重测跑"、"停掉 bg-1（exit_code=-9，进程树强杀，list_background 确认）"、
"逐行审查 `prompt.py` 并交付 349 行报告"），而那些工作在别的轮。根因：为把产物压到 1.8K，
删掉分块地图时把它的**位置锚**一起删了。修法：锚由**代码**给。

## 锚长这样（指令里新增的一节，逐字）

```
[本轮动作清单]（Runtime 从每轮事件里机械抽出；**只许据此写，不许跨轮借内容**）
R1：工具 glob_files×1、list_agents×1、list_files×1、page_text×1、read_file×2、run_command×1、search_files×1、web_fetch×3、web_search×2｜文件 读:README.md、读:AGENTS.md
R2：工具 check_background×12、edit_file×12、list_files×1、run_background×4、run_command×98、stop_background×1、web_search×1、write_file×10｜文件 写:gaia_bench/__init__.py、写:gaia_bench/data.py、写改:gaia_bench/evaluate.py、写:gaia_bench/worker.py、写改:gaia_bench/runner.py、写改:gaia_bench/report.py、写:gaia_bench/README.md、写改:gaia_bench/offline.py、写改:gaia_bench/rescore.py、写:output/gaia/FINDINGS.md｜非零退出 {"task_id": "bg-2"}(exit=-9)
R3：工具 check_background×2、edit_file×2、list_background×1、run_background×1、run_command×4、stop_background×1｜文件 改:gaia_bench/runner.py｜非零退出 {"task_id": "bg-1"}(exit=-9)；{}(exit=-9)
R4：**无工具动作**（纯对话轮）——一句话只写谈了什么、结论是什么，不得出现文件路径或工具动作。
R5：**无工具动作**（纯对话轮）——一句话只写谈了什么、结论是什么，不得出现文件路径或工具动作。
R6：**无工具动作**（纯对话轮）——一句话只写谈了什么、结论是什么，不得出现文件路径或工具动作。
R7：**无工具动作**（纯对话轮）——一句话只写谈了什么、结论是什么，不得出现文件路径或工具动作。
R8：工具 list_agents×1、run_command×15、search_files×1、write_file×2｜文件 写:docs/prompt-review-2026-09-16.md
R9：**无工具动作**（纯对话轮）——一句话只写谈了什么、结论是什么，不得出现文件路径或工具动作。
```

## 重跑读数

| 项 | v1（无锚） | v2（带锚） |
|---|---|---|
| usage | `prompt 131,523 / cached 0 / miss 131,523`（冷） | `prompt 132,102 / cached 131,072 / miss 1,030` |
| completion | 27,690 | 13,638（含推理 11,555） |
| 机械校验全部告警 | 13 条 | 3 条（均为粗规则误报） |
| **零动作轮上的违例（硬门）** | **6 条（R4/R5/R9）** | **0 条** |

三条剩余告警的成因（都不算错位）：`README.md/AGENTS.md` 被正则当成一条路径、`runner.py`
是 `gaia_bench/runner.py` 的简称、`web_search` 指"检索通道"而非一次工具调用。

## v2 产物（R4/R5/R9 —— v1 里张冠李戴的三轮）

```
[R4] 👤 你先介绍一下你自己，我看看。
      ▸ 介绍自己在会话里的角色与能力边界，说明会先区分用户是在问、在表达想法还是让动手，并承认此前擅自超出授权范围那次失误。
[R5] 👤 对啊，你就是Wovra，我让你测试wovra，你不在自己这里测试，你跑别的地方测试干嘛？
      ▸ 认同自己就是 Wovra 本身的活实例，此前把 Wovra 当外部系统去搭副本测试属于舍近求远；验证它的正确做法是直接当面解题、由用户亲眼看到卡在哪，而批量评测所需的隔离是另一件事。
[R9] 👤 那你从站在你的角度，或者说你这几轮的观察下，感觉wovra这个agent用着有哪些不顺手？
      ▸ 给出用起来不顺手的八条体感：最伤的是不知道自己就是 Wovra（身份歧义叠加源码在工作区外、提示词又拿 wovra 举例）；其余为范围规则缺「不得擅自扩大范围」与「停止是终局指令」、运行时信封描述与实际不符且静默规则未禁点评通知本身、13 个已装配工具未被提名、「本项目」指代不明且缺就地做还是搭基建的判据、长段落里后半句强指令压过前半句、可见范围与被测对象错位、缺要观点还是要产物的判据。
```

（R4/R5/R9 的事件里只有 `user + final_answer`，所以产物只可能谈"谈了什么、结论是什么"——
硬门就是拿这条当判据。）


---

# v3：动作清单改分块（轮号独占一行，块间空行）

**为什么改**：v2 的清单把"工具 / 文件 / 非零退出"塞进一条长行——R2 那条 400+ 字符，
人看着是墙，模型也得自己在一行里找边界。而锚的全部作用就是**边界清楚**，轮号独占一行、
块间空行更醒目（与提示词审查里"无小节标题的长文本，关键规则埋在中段"同一类问题）。

**只改这里**：产物里模型写的句子**保持纯句子**，布局归代码（渲染时各自排版）——
理由见 worklog §139。

```
（Runtime 从每轮事件里机械抽出；**只许据此写，不许跨轮借内容**）

R1
  工具：glob_files×1、list_agents×1、list_files×1、page_text×1、read_file×2、run_command×1、search_files×1、web_fetch×3、web_search×2
  文件：读 README.md；读 AGENTS.md

R2
  工具：check_background×12、edit_file×12、list_files×1、run_background×4、run_command×98、stop_background×1、web_search×1、write_file×10
  文件：写 gaia_bench/__init__.py；写 gaia_bench/data.py；写改 gaia_bench/evaluate.py；写 gaia_bench/worker.py；写改 gaia_bench/runner.py；写改 gaia_bench/report.py；写 gaia_bench/README.md；写改 gaia_bench/offline.py；写改 gaia_bench/rescore.py；写 output/gaia/FINDINGS.md
  非零退出：{"task_id": "bg-2"}(exit=-9)

R3
  工具：check_background×2、edit_file×2、list_background×1、run_background×1、run_command×4、stop_background×1
  文件：改 gaia_bench/runner.py
```

## v3 读数（前缀还热，几乎免费）

| 项 | v1 | v2（带锚） | v3（分块锚） |
|---|---|---|---|
| usage | prompt 131,523 / cached 0 / miss 131,523 | prompt 132,102 / cached 131,072 / miss 1,030 | prompt 132,100 / cached 131,200 / **miss 900** |
| completion | 27,690 | 13,638（推理 11,555） | **8,020（推理 6,527）** |
| 零动作轮违例（硬门） | 6 条（R4/R5/R9） | 0 条 | 0 条 |

v3 剩下 2 条软告警，都是同一类误报（简称）：R2 的 `FINDINGS.md` 是 `output/gaia/FINDINGS.md`
的简称、R3 的 `web_search` 指检索通道而非一次工具调用。

completion 三次递减（27,690 → 13,638 → 8,020，推理 25.7K → 11.6K → 6.5K）**不是受控对照**
（模型随机性未排除），只作观察记录；若这条成立，它比"省输入"值钱得多——推理 token 才是成本主体
（§137）。待办：同批材料重复 3 次取均值再下定论。
