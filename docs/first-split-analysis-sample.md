# 首次分裂分析实测样本（会话 20260907-141244-17dcb8 · R1-R12 全批）

> 2026-09-08。分裂分析路（`_split_rounds` + 常驻工具 `submit_domains`）
首次真实数据实测：输入 = R1-R12 原始原文（94,627 prompt tok）+ 分块地图
（50 块）+ 分裂分析指令；单次生成、带思考（模型默认，未关）。跑在 /tmp
副本上，原会话未动。**判据（2026-09-08 用户拍板）：归属跟现状走——被
取代的前史归取代者；分裂标准 = 可独立运行，不是话题分类。**

## 1. 时间与 token 账

| 指标 | 值 | 对照（整理路 R9 批） |
|---|---|---|
| 耗时 | **122s** | 330-490s |
| prompt | 94,627 tok | ~90K（同量级） |
| completion | **4,410 tok** | 23,643-31,160 |
| 其中思考 | 2,967 tok（67%） | 75-84% |
| 工具调用 | submit_domains ×1 | submit_organization ×1 |

* 可见输出仅 ~1,400 tok：分裂分析是判断任务不是誊写任务，产出天然小——
  双路并行的增量成本比预想低一个量级。

## 2. 现状清单与归属（逐字）

```json
{
 "domains": [
  {
   "block_ids": [
    "R2-B1",
    "R2-B2",
    "R2-B3",
    "R2-B4",
    "R2-B5",
    "R3-B1",
    "R3-B2",
    "R4-B1",
    "R4-B2",
    "R5-B1",
    "R5-B2",
    "R5-B3",
    "R6-B1",
    "R6-B2",
    "R6-B3",
    "R6-B4",
    "R6-B5",
    "R6-B6",
    "R7-B1",
    "R7-B2",
    "R7-B3",
    "R7-B4",
    "R7-B5",
    "R9-B1",
    "R9-B2",
    "R9-B3",
    "R9-B4",
    "R9-B5",
    "R9-B6",
    "R9-B7",
    "R9-B8",
    "R9-B9",
    "R9-B10",
    "R9-B11",
    "R9-B12",
    "R9-B13",
    "R9-B14",
    "R9-B15",
    "R9-B16",
    "R9-B17",
    "R9-B18",
    "R9-B19",
    "R9-B20",
    "R9-B21",
    "R9-B22"
   ],
   "constraints": [
    "纯前端零依赖：不调用任何 API，所有回复来自本地预设脚本（js/data.js + 可选的 js/agent-pack.js）",
    "不使用 git——用户在 R9 明确否决，无版本管理，改动安全网靠 node tests/run.js（5 套件全绿方可交付）",
    "模块化增量修改：R5 因用户批评「每次新需求就整体重写」而立下规矩，此后一律 edit_file 增量改、禁止整文件覆盖",
    "保持 file:// 双击直开兼容：不用 ES modules，script 加载顺序固定为 data.js → app.js → agent-pack.js（agent-pack 依赖前者的运行时全局 KB/CHIPS/SCRIPT/sessions/cur）",
    "js/agent-pack.js 是可选数据扩展包（R9 新增），只往 KB/CHIPS/SCRIPT 追加内容，可整体删除而核心零改动",
    "交互类消息（ask 追问/confirm 风险确认）的选择与决策状态必须随消息持久化到 localStorage，刷新后不得退回待操作状态；风险命令必须经 CONFIRM_FLOWS 白名单关联执行体",
    "Agent 流程执行器（runAgentSteps）每步检查 gen 代数计数，切换/新建会话或点停止按钮时立即中止，回复只写入发起它的会话"
   ],
   "file_domains": [
    "/home/lkf/bc/python/Wovra/web/index.html",
    "/home/lkf/bc/python/Wovra/web/css/style.css",
    "/home/lkf/bc/python/Wovra/web/js/data.js",
    "/home/lkf/bc/python/Wovra/web/js/app.js",
    "/home/lkf/bc/python/Wovra/web/js/agent-pack.js",
    "/home/lkf/bc/python/Wovra/web/README.md",
    "/home/lkf/bc/python/Wovra/web/tests/run.js",
    "/home/lkf/bc/python/Wovra/web/tests/lib.js",
    "/home/lkf/bc/python/Wovra/web/tests/agent.test.js",
    "/home/lkf/bc/python/Wovra/web/tests/features.test.js",
    "/home/lkf/bc/python/Wovra/web/tests/interactions.test.js",
    "/home/lkf/bc/python/Wovra/web/tests/sessions.test.js",
    "/home/lkf/bc/python/Wovra/web/tests/theme.prelude.js",
    "/home/lkf/bc/python/Wovra/web/tests/theme.test.js"
   ],
   "goal": "纯前端多会话 AI 对话演示页：模拟 Agent 运行全过程（💭 深度思考→🛠️ 工具调用含子步骤→📤 运行结果→❓ 追问卡片→⚠️ 风险命令确认），支持浅/深主题、会话搜索、Markdown 导出、停止生成、编辑重发；会话数据持久化于 localStorage",
   "name": "小悟 AI 助手演示项目（模块化多会话 Agent 演示）",
   "superseded": [
    {
     "block_ids": [
      "R2-B1",
      "R2-B2",
      "R2-B3",
      "R2-B4",
      "R2-B5"
     ],
     "note": "单文件期：全部 HTML/CSS/JS 内嵌于单个 index.html（14KB），被 R3 多会话版整体重写取代"
    },
    {
     "block_ids": [
      "R3-B1",
      "R3-B2"
     ],
     "note": "多会话单文件版（23KB），被 R4 全屏布局+浅色主题版整体重写取代"
    },
    {
     "block_ids": [
      "R4-B1",
      "R4-B2"
     ],
     "note": "全屏+双主题单文件版（25KB），被 R5 模块化拆分（index.html 薄壳 + css/style.css + js/data.js + js/app.js + README.md）取代"
    }
   ]
  }
 ],
 "split_assessment": {
  "proposal": null,
  "reason": "现状清单仅存在 1 个活性文件域——小悟演示项目。index.html/css/js/README/tests 全部强耦合于同一应用：tests/run.js 直接读取并拼接 js/data.js + js/app.js + js/agent-pack.js 执行（离开应用文件测试即失效）；agent-pack.js 依赖 app.js 的运行时全局；README.md 记录的是整个项目的结构与扩展指南。不存在第二个拥有独立文件边界且可独立运行的关注面。R8/R10/R11/R12 的工具反馈、扩展建议等话题虽与应用演进相关，但话题不同永远不构成分裂理由，故不可分。",
  "splittable": false
 },
 "unassigned": {
  "block_ids": [
   "R1-B1",
   "R8-B1",
   "R10-B1",
   "R11-B1",
   "R12-B1"
  ],
  "reason": "R1 为零状态寒暄（自我介绍，未涉及任何文件）；R8（扩展方向建议）、R10（工具缺陷反馈）、R11（上下文压缩效果评价）、R12（工具优化建议补充）均为纯讨论轮，无文件产出、不改变任何文件域状态，归档处理。"
 }
}
```

## 3. 对照用户判据的读点

* **单一域**：R2-R9 的 45 块全部归入"小悟演示项目"——正是用户预判的
  "B–F 是一类，属于项目写"；A-F 旧分类把它拆成 5 类的话题聚法被证伪。
* **生死链完整**：superseded 三条把版本杀链还原到位——R2 单文件期 →
  被 R3 取代 → 被 R4 取代 → 被 R5 模块化拆分取代；被取代的前史归取代
  者所在域，未出现"旧版一类、新版一类"。
* **约束红线全量复原**：不用 git（注明 R9 否决）、模块化增量（注明 R5
  批评起因）、file:// 兼容与 script 加载顺序、agent-pack 零侵入、
  ask/confirm 持久化、CONFIRM_FLOWS 白名单、gen 计数中止——散落 12 轮
  的约束全部收拢，这就是注册表"类别描述+所有权文件域"的直接原料。
* **未归属 5 块归档**：R1-B1（零状态寒暄）+ R8/R10/R11/R12（纯讨论轮，
  "无文件产出、不改变任何文件域状态"）——与机制三"非文件讨论留主
  agent"一致。讨论轮是挂靠项目域还是归档，模型选了后者（判据允许，
  用户已确认）。
* **可分性 = False，给出耦合论证**：tests/run.js 拼接执行三个 js 文件、
  agent-pack 依赖 app.js 运行时全局——强耦合无第二个独立文件域；
  "话题不同永远不构成分裂理由"。

## 4. 遗留观察

* R8/R10-R12 讨论轮若未来催生"runtime 工具演进"域（讨论变代码），
  下次水位分析会自然把它们重新归属——归属渐近精确原则的实例。