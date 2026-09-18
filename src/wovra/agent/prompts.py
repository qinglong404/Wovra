"""Agent 提示词与工具 schema（纯数据，逐字节自原单文件 agent 搬运）。

这些字符串是**模型可见面**：改动会使前缀缓存失效一次，重构期间
只允许搬运、不允许改一个字（重构纪律，见计划文档）。
2026-09-11 重构完成：agent.py 已拆为 agent/ 包，本模块为提示词与
schema 的唯一归属（prompts.py）。
"""


_ORG_SUBMIT_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "submit_organization",
        "description": (
            "提交批量整理产物。仅限后台整理阶段调用（作为整理结果的唯一"
            "出口）；工作对话中调用无效，只返回说明文本。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "rounds": {
                    "type": "array",
                    "description": "与待整理轮一一对应，每个元素对应一轮",
                    "items": {
                        "type": "object",
                        "properties": {
                            "seq": {
                                "type": "integer",
                                "description": "整数轮次号（如 9），必须与待整理轮一一对应",
                            },
                            "normalized_user_input": {
                                "type": "string",
                                "description": (
                                    "该轮用户意图的澄清表述——不是压缩，"
                                    "是把用户想要什么说得更清楚"
                                ),
                            },
                            "key_constraints": {
                                "type": "string",
                                "description": (
                                    "该轮用户立下的红线/硬性约束（禁止什么、"
                                    "必须怎样、明确否决的方向），没有则给空字符串"
                                ),
                            },
                            "block_summaries": {
                                "type": "array",
                                "description": (
                                    "逐块信息量自适应描述。id 逐字取自分块地图，"
                                    "每个块一条、一个不落、与地图同序；每条篇幅"
                                    "与该块承载的实际信息量成正比"
                                ),
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "id": {
                                            "type": "string",
                                            "description": "块ID，如 R9-B14",
                                        },
                                        "summary": {
                                            "type": "string",
                                            "description": (
                                                "篇幅随块的信息量伸缩：信息量大的块"
                                                "（多文件改动/长推理链/关键决策）写"
                                                "完整细节——做了什么、针对哪些文件"
                                                "（路径写全）、为什么做、结果与结论、"
                                                "失败原因与后续修复；信息量小的块"
                                                "（寒暄/一句话问答/零状态轮）两三行"
                                                "甚至一句即可，禁止为低信息量块强行"
                                                "堆字数。关键数字（行数/字节数/条数/"
                                                "次数）与关键命令的目的必须保留；含"
                                                "最终回答的块，把对用户的承诺/交付"
                                                "口径完整写进去；禁止空洞词（调整/"
                                                "修改/处理）单独成描述"
                                            ),
                                        },
                                    },
                                    "required": ["id", "summary"],
                                },
                            },
                            "refined_index": {
                                "type": "array",
                                "description": (
                                    "仅无分块结构的轮使用（替代 block_summaries）："
                                    "一行式事件摘要，id 取自该轮事件流已有的"
                                    "事件 ID，无实质内容的事件（如寒暄）可省略；"
                                    "索引行比截断行更短更准（保留结论：什么可行、"
                                    "什么实测不行、卡在哪）"
                                ),
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "id": {
                                            "type": "string",
                                            "description": "事件ID，如 R3-E02",
                                        },
                                        "line": {
                                            "type": "string",
                                            "description": "一行摘要",
                                        },
                                    },
                                    "required": ["id", "line"],
                                },
                            },
                        },
                        "required": [
                            "seq", "normalized_user_input", "key_constraints",
                            "block_summaries",
                        ],
                    },
                },
                "state_patch": {
                    "type": "object",
                    "description": (
                        "这些轮次合并后的任务状态增量补丁。多轮之间重复交代的"
                        "决策与背景只记一次，已完成的事项不要重复累积。质量锚点"
                        "（zcode-borrowings.md）：整理后的视图必须能回答——用户"
                        "原话要求了什么、立了哪些约束、已做了哪些决策、当前状态"
                        "如何、下一步是什么"
                    ),
                    "properties": {
                        "completed": {
                            "type": "array", "items": {"type": "string"},
                        },
                        "decisions": {
                            "type": "array", "items": {"type": "string"},
                        },
                        "known_issues": {
                            "type": "array", "items": {"type": "string"},
                        },
                        "open_questions": {
                            "type": "array", "items": {"type": "string"},
                        },
                        "escalations": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "决策升级：实测与预期不符、影响方向、需要上级或"
                                "人拍板的事项（写明预期、现实、选项），"
                                "不要擅自改方向"
                            ),
                        },
                        "experiments": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "待办实验：机器无法自行验证、需要人当传感器的事项"
                                "（写明做什么、看什么、什么算对）"
                            ),
                        },
                        "closed": {
                            "type": "array",
                            "description": (
                                "结案清单：把**已经解决**的旧条目从账本里移除"
                                "（2026-09-11 用户拍板）。判据——escalations 已"
                                "被人拍板、experiments 已验证完毕、已知问题已"
                                "解决，都属结案：留在账本里会被下一轮的自己当"
                                "成待办重问一遍。每条给 field（字段名，限 "
                                "constraints/decisions/completed/known_issues/"
                                "open_questions/escalations/experiments）与 "
                                "match（**该条里的一小段原文**，20-40 字，"
                                "须在账本里唯一）。机制要求子串唯一命中才移除，"
                                "匹配不到或多条一律不动并在 history 记一行——"
                                "所以片段要抄得够准。注意：decisions 只在"
                                "**被推翻**时结案（正常决策是历史，留着）。"
                                "没有可结案的给空数组。"
                            ),
                            "items": {
                                "type": "object",
                                "properties": {
                                    "field": {"type": "string"},
                                    "match": {"type": "string"},
                                },
                                "required": ["field", "match"],
                            },
                        },
                        "current_status": {"type": "string"},
                        "goal": {"type": "string"},
                        "is_done": {"type": "boolean"},
                    },
                },
            },
            "required": ["rounds", "state_patch"],
        },
    },
}

_ORG_META_INFO = (
    "[压缩元信息]\n"
    "- 本次整理范围：R{seq_list}。连续纯聊天轮已合并为组（如 R1-2），"
    "组内共用一个块描述、一个综合意图。\n"
    "- 状态定义：LIVE=文件当前有效（内容以磁盘为准，需要细节时重新读）；"
    "DEAD=已删除/已闭环/测试已清理（不是失败）；B1=该轮第一个块。\n"
    "- 原文都在会话历史里，本视图只降分辨率不删事实；后续需要细节时按"
    "块/事件取回原文。\n"
)

_SPLIT_INSTRUCTIONS = (
    "[分裂结构指令]\n"
    "你的**唯一任务**：把[硬数据]里的活性文件梳理成一棵**多层级结构树**，\n"
    "并给**会成为 agent 的节点**写清职责。\n"
    "**分工**（2026-09-15 用户口径：「分裂只需要写结构树和每个 agent 的职责，\n"
    "其它都不需要搞了」）：你只负责**结构与职责**——不相关的隔开、相关的归成\n"
    "一条线、这条线是干什么的；**其余全由代码算**：哪个文件归谁、非 LIVE 文件\n"
    "挂哪、描述文字（取文件开头）、是否分裂、在哪一层分裂、块归属，一律由\n"
    "Runtime 机械决定。**所以你不要逐文件列清单，也不要写文件描述**。\n"
    "树形（节点用**路径**声明它管哪一片），照项目实际组织结构分层，该深就深：\n"
    "    项目架构\n"
    "    ├── 核心业务层            path: src/core/\n"
    "    │   ├── 接口与控制器      path: src/core/api/\n"
    "    │   └── 应用与领域        path: src/core/domain/\n"
    "    └── 支撑与工程层          path: tests/\n"
    "1. **每个节点填 name + parent + path**（顶层 parent 留空）。\n"
    "   `path` 就是它的**范围**：可以是一个目录前缀（`src/wovra/tools/`），\n"
    "   也可以是一个具体文件（`webui/index.html`）——按语义决定切多细，\n"
    "   不必刻意每文件一个节点。文件归属由代码按**最深的 path 前缀**机械\n"
    "   匹配（`src/wovra/tools/` 比 `src/` 更优先），你**不用**列文件清单。\n"
    "   多个路径一片活 → 填 `paths`（数组）；确实没有路径范围的纯分组\n"
    "   节点（只是分类）可以不填，但它的子节点要有。\n"
    "2. **职责写在产物的最后一节 `responsibilities`**（`{节点名: 职责}`，只写给\n"
    "   **会成为 agent 的节点**——顶层那几个，每个 ≤60 字）。**顺序要紧：先写\n"
    "   整棵树（domains），最后写职责**——万一被端点掐断，丢的只是尾部几条描述\n"
    "   （Runtime 会用文件开头兜底），树与路径完好；职责写进节点里则可能连节点\n"
    "   一起丢（实测「抢救出 22 个完整节点」就是这么丢的）。\n"
    "   每个职责写满三件（它是跨 agent 唯一的公共信息，主 agent 只凭它路由，\n"
    "   2026-09-13 用户口径：写松了会出现「同一话题换个人答」）：\n"
    "   ①**干什么 + 产出什么**（点出关键文件/产物名）；\n"
    "   ②**边界**：与最容易混的那个节点怎么分——什么归我、**什么不归我**；\n"
    "   ③**什么信号落到我这里**：用户提到哪些文件/名字、问哪一类问题时找我。\n"
    "   其余节点**一概不写描述**（分组/叶子都不写）：文件级描述由 Runtime\n"
    "   取**文件开头**（首行注释/标题/docstring）机械生成，永远新鲜，\n"
    "   不需要你抄。\n"
    "3. **块例外才写**：文件块按路径自动归属，不必逐块列 block_ids；\n"
    "   跨节点的聊天块/用户块（讨论、决策这条线的纯聊天块，ID 逐字取自\n"
    "   [分块地图]）填 chat_block_ids / user_block_ids；与任何工作线都无关的\n"
    "   放 unassigned（归主 agent）。块 ID 写了地图里不存在的会被 Runtime\n"
    "   判错并带诊断重发一次。\n"
    "4. 结构稳定：同一批数据两次分析应给出同构的树（数量与层级一致）。\n"
    "   **产物务必短**：只有 name/parent/path/顶层职责，几十行而已——\n"
    "   截断是分裂失败的头号来源（实测一次 13,523 字符的产物整批作废，\n"
    "   另一次写满文件编号被截断只抢救出 22 个节点）。**省掉节点＝结构树不完整**。\n"
    "完成后调用 submit_domains 提交（唯一出口，不要在正文输出 JSON）。\n"
)

# 格式纪律（2026-09-11 机制评审：护栏前移，从"治"变"防"）。
# 实测根因（worklog §11.2）：长中文叙述里混进未转义 ASCII 双引号会截断
# JSON 串。此前只在**失败后**的带诊断重发里提示这一点（治）；现在把它
# 写进常规整理指令（防），成本一行，能减少一次畸形产物与一次重发。
_SPLIT_LIVE_INSTRUCTIONS = (
    "[分裂结构指令]\n"
    "以上是本会话的完整上下文。请按**活性文件**把现状梳理成结构树，并给会成为 agent 的节点写职责。\n"
    "1. 每个节点填 name + parent（顶层留空）+ `path`（或 `paths`）。`path` 就是它的**范围**："
    "目录前缀（`gaia_bench/`）或单个文件（`output/gaia/FINDINGS.md`）。文件归属由代码按"
    "**最深的 path 前缀**机械匹配，**你不用列文件清单**。\n"
    "2. 职责只写给**会成为 agent 的节点**（顶层那几个），写成 `{节点名: 职责}`，每个 ≤60 字，"
    "写满三件：①干什么 + 产出什么（点出关键文件）；②边界——什么不归我；③什么信号落到我这里。\n"
    "3. 树要**覆盖全部活性文件**：每个文件都得能被某个节点的路径匹配到。\n"
    "4. **默认粗、被逼才细**：能一条活干完的文件就放同一个域。只有当一个域**内部**本来就分成"
    "几摊、各摊互不需要彼此的文件时，才往下分——**只有后面不得不分时才分这么细，第一次不要这样**"
    "（材料少时的共现可能只是巧合，拆细就是瞎猜）。\n"
    "5. **什么时候不该单独成域**——判据是「**这个域能不能独立接活**」：不翻别人的活性文件，"
    "能不能把**大部分**活干完？不能就别拆。这也不是文件在不在单独的目录里："
    "`docs/`、`output/` 这类**目录名不是职责**；一个文件的**产出工作属于谁，它就归谁**"
    "（例如写 `output/gaia/FINDINGS.md` 要读 GAIA 的全部内容、是跑完评测后的汇总 → 归 GAIA 域）；"
    "自查一句：**这个域要干活时，是不是得频繁去读别的域的文件、或频繁问别的域？** 是 → 别拆。"
    "顶层域一般是「**一个有产出的工作线**」，不是一个文件。」\n"
    "6. 不要管用户发言、环境准备、闲聊、临时脚本——只按文件本身的组织结构分。\n"
    "7. **一个节点可以管多片路径**（`paths` 数组）——同一个职责别写成两个同名节点："
    "名字是路由与执行者索引的**唯一身份**，同名即无法区分（代码会把同名同父的节点合并成一个）。\n"
    "8. **现有域原样沿用**（名字即身份）：`[现有域]` 里列出的名字一个字都不要改——它们是路由、"
    "账本、执行者索引认人的唯一凭据；你只能**新增**节点，或把某个域**再裂一层**（裂出来的用"
    "`父名-1`/`父名-2`）。\n"
    "完成后调用 submit_domains 提交（唯一出口，不要在正文输出 JSON）。"
)

_ORG_FORMAT_DISCIPLINE = (
    "[格式纪律]\n"
    "提交参数的文本里**不要使用英文双引号**——需要引用时用中文引号「」或"
    "“”；英文双引号会截断 JSON 字符串（实测失败根因）。\n"
    "反斜杠只用于 \\\" \\\\ \\/ \\b \\f \\n \\r \\t \\uXXXX 这些合法转义；"
    "不要尾随逗号，不要在字符串里放裸换行。\n"
)

_ORG_TAG_INSTRUCTIONS = (
    "[标签说明]\n"
    "一、块类型 → 产物\n"
    "1. 文件块【路径(状态)】：标签序列 —— 把块内内容综合总结成一段话：\n"
    "   按标签序列所代表的时间序交代：从哪来、做了什么、结果如何；多标签\n"
    "   只做覆盖检查（每项都交代到），不作为分段格式；带「工具」的块 =\n"
    "   吸收了验证/执行类工具（test/build/run）：把工具调用结果融进叙述，\n"
    "   构成因果链（如\"改了 X，跑 pytest 验证通过\"）；只写动作不写结果是\n"
    "   缺失。一段话写完，不拆条、不重复标签。\n"
    "2. 环境块【环境块】：为什么做环境准备（目的）+ 结果，简练一段。\n"
    "3. 用户块【用户块】：轮头**之后**的用户补充/修正输入。先保留\n"
    "   补充原话（引号），再附要点（插在哪段工作之后）。原话取补充\n"
    "   输入本身，不是轮头原文。与文件块独立成段，不混写。\n"
    "4. 保底块【保底块】：（可能带「工具」）结论，简练一段。纯聊天\n"
    "   轮（无「工具」）自由发挥：如实描述这轮实际内容（寒暄/问答/\n"
    "   决策讨论/方案谋划），不要用占位词敷衍。\n"
    "二、文件块标签 → 写什么\n"
    "   创建：文件用途、整体结构、核心设计思想（骨架级，写充实）\n"
    "   重构：重写动机、与旧版的关键差异、现在的结构（差异是重点）\n"
    "   修改：改了什么实现、为什么改、影响什么\n"
    "   读：为什么读、读出的关键结论（它服务于后续的写/改/删）\n"
    "   只读：文件用途/大致结构，细节不展开——需要时重新读\n"
    "   删除：删了什么、为什么删、最终结论\n"
    "   幽灵：路径：动作；幽灵（文件不存在）；结果；无实质内容\n"
    "   越界：路径：动作；越界（被安全拦截）；结果；无实质内容\n"
    "   测试/实验文件块（创建-读-删除的测试矩阵成员）：统一一行模板\n"
    "     \"文件：测[特性]；结果[正常/异常]：[结论]\"（如\n"
    "     \"t2_empty.txt：测空文件读取；read_file 返回'是空文件'，正常\"）。\n"
    "     信息量小，一行即可，禁止跳过。\n"
    "三、状态 → 详略底线\n"
    "   LIVE：当前有效内容/结构写清楚——这是现状，后续工作基于它；\n"
    "   DEAD：只保留死亡原因与最终结论。测试/实验类文件即使 DEAD，路径、\n"
    "     操作序列、结果、失败原因必须保留（它验证了什么、怎么验的、结果\n"
    "     如何，属于结论的一部分）；状态已在标签行给出，正文不要重复解释\n"
    "     状态。\n"
    "四、用户输入（轮头）：👤 用户原文 / 🎯 意图（澄清后）/ 📌 关键约束——\n"
    "   全量保存，最高分辨率。\n"
    "   🎯 意图只提炼该轮轮头用户输入原话的内容：短批准句（如\"可以\"）\n"
    "     的意图 = 批准/同意上文的提议，不扩展不脑补；轮内事件（ask_user\n"
    "     的回复、工具结果）不是意图来源；合并组综合组内所有轮的原话。\n"
    "     意图必须与该轮 👤 用户原文直接对应——原文里没有的内容（如其他\n"
    "     轮的\"可以测试A\"\"23M/458M\"等）一律不得写进意图。\n"
    "   📌 约束标注来源轮次与时效：格式 [R7] 只读限定 agent-test；被后续\n"
    "     轮覆盖的约束注明覆盖关系（如\"R8 用户新指令开放写入，覆盖 R7\n"
    "     只读\"）；同一约束多次出现只保留最新状态并带最新来源轮次。\n"
    "     轮内用户拍板的关键决策（ask_user 的回复、明确的选型如\"选 C\"）\n"
    "     也写入该轮 📌 约束。\n"
    "五、轮头与块描述分工：轮头已承载\"原话、诉求、约束\"，\n"
    "   块描述不得复述用户输入，只写对其的响应——做了什么、\n"
    "   结论是什么。\n"
    "六、合并与覆盖（2026-09-10 用户拍板）\n"
    "   1. 连续纯聊天轮（保底块且无「工具」）在[分块地图]中合并为一组\n"
    "      （如 R1-2）：组内只输出一个块描述（写到组内第一轮的块上，\n"
    "      其余轮不写），同时 🎯 意图/📌 约束综合组内所有轮的用户输入\n"
    "      提炼一次（写在组内第一轮）——禁止只取其中某一轮的内容；\n"
    "      合并组块描述内部按子轮分段标注（R19：…/R20：…/…），便于\n"
    "      后续按轮拉开原文；\n"
    "   2. 每个块都必须给出描述，一个不落：信息量少的块一句带过即可，\n"
    "      禁止跳过不写；\n"
    "   3. 块 ID 与内容必须严格一一对应：从[分块地图]第一个块开始按\n"
    "      顺序写，不跳号、不错位——某块的内容必须写在该块的 ID 下，\n"
    "      禁止把 A 块的内容挂到 B 块的 ID（实测易犯：测试矩阵轮整体\n"
    "      错位一位）。\n"
    "七、篇幅\n"
    "   标签定了分辨率基线，块内实际信息量只做微调：大改动/长推理/关键决策\n"
    "   写满该标签允许的篇幅；低信息量块（寒暄/一句话问答）两三行甚至一句\n"
    "   即可；关键数字（行数/字节数/条数/次数）与关键命令的目的必须保留；\n"
    "   禁止空洞词（\"调整\"\"修改\"\"处理\"）单独成描述；含最终回答的块，对\n"
    "   用户的承诺/交付口径完整写入。\n"
)

_ROUND_NOTE_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "submit_round_notes",
        "description": (
            "提交这批**每一轮**的一段话 ＋ 账本增量。仅限水位处的结算调用；"
            "工作对话中调用无效，只返回说明文本。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "notes": {
                    "type": "array",
                    "description": (
                        "与锚里的轮**一一对应**，一个不落、顺序一致（每条 seq 写它自己的轮号）"
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "seq": {"type": "integer", "description": "轮次号"},
                            "sentence": {
                                "type": "string",
                                "description": (
                                    "一句话：这一轮做了什么、结论是什么。关键数字（行数/条数/"
                                    "次数）、文件路径、命令名原样保留；不复述用户输入"
                                ),
                            },
                            "failures": {
                                "type": "array",
                                "description": (
                                    "失败与坑：非零退出/越界拦截/路径不存在/方案被推翻。"
                                    "没有就给空数组"
                                ),
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "text": {"type": "string"},
                                        "evidence": {
                                            "type": "string",
                                            "description": (
                                                "证据事件 ID（如 R2-E174），取自失败候选；没有留空"
                                            ),
                                        },
                                    },
                                    "required": ["text"],
                                },
                            },
                        },
                        "required": ["seq", "sentence", "failures"],
                    },
                },
                "awaiting_user": {
                    "type": "string",
                    "description": (
                        "这一轮结束时你在**等用户答复**的事（例如结尾问他「要不要按 A 做」、"
                        "「选项 A/B，等你拍板」、「我不动手，等你说」）。原话要点照写；没有留空"
                    ),
                },
                "ledger_append": {
                    "type": "object",
                    "description": "这批新增的账本条目（整批写一次；只增不减，已有的不要重复写）",
                    "properties": {
                        "constraints": {"type": "array", "items": {"type": "string"}},
                        "decisions": {"type": "array", "items": {"type": "string"}},
                        "known_issues": {"type": "array", "items": {"type": "string"}},
                        "open_questions": {"type": "array", "items": {"type": "string"}},
                        "closed": {
                            "type": "array",
                            "description": (
                                "结案：把**已经过时**的旧条目移出账本。每条给 field 与 match"
                                "（该条里的一小段原文，20-40 字，须在账本里唯一）。"
                                "只能结**更早批次**留下的条目"
                            ),
                            "items": {
                                "type": "object",
                                "properties": {
                                    "field": {"type": "string"},
                                    "match": {"type": "string"},
                                },
                                "required": ["field", "match"],
                            },
                        },
                    },
                },
            },
            "required": ["notes", "ledger_append"],
        },
    },
}

_ROUND_NOTE_INSTRUCTION = (
    "[结算指令]\n"
    "锚里逐条列出要写的机械事实（轮号、执行者、工具与文件、失败候选、结论草稿）。"
    "请给锚里的**每一条**各写一段话，一个不落、与锚的条数一一对应；"
    "标了「第 n/m 段」的只写那一段（一轮由多家执行时，每家写自己那一段）。\n"
    "1. 一句话：这一条做了什么、得出什么；关键数字与路径原样保留。**只写锚里这条的事**，"
    "其余不用你写（用户原话与文件清单由代码补）。\n"
    "   **不复述用户要什么**（用户原话由代码逐字摆在上下文里）；**不照抄结论草稿**，改写它。"
    "每条 **80–250 字符**；信息量小的条更短也行，但不许写空话。\n"
    "2. 失败与坑：每轮以失败候选为底逐条核对（同类可合并），候选里没有但你知道的照样写；"
    "没有就给空数组。尽量带 evidence（候选〔〕里的事件 ID）。\n"
    "3. **等你答复**（`awaiting_user`）：这一轮结尾若在**等用户拍板**（把选项摆给他了、说了"
    "「等你说」），就写进 `awaiting_user`——下一轮隔着一句话也要接得上（防脱节）；没有留空。\n"
    "4. 账本增量：整批写一次，只写新增条目；标量（现状/目标）不用你写。**账本谁都能维护**——"
    "过时的旧条目用 ledger_append.closed 结案（field ＋ 该条里一小段唯一原文），"
    "只能结更早批次留下的。\n"
    "完成后调用 submit_round_notes 提交（唯一出口）。"
)

_ORG_DOMAINS_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "submit_domains",
        "description": (
            "提交现状结构树（结构 + 每个 agent 的职责）。仅限后台整理阶段"
            "调用（作为分裂分析的唯一出口）；工作对话中调用无效，只返回"
            "说明文本。文件归属、非 LIVE 文件挂载、文件级描述、是否分裂"
            "一律由代码机械决定（2026-09-15 用户口径：分裂只需要写结构树"
            "和每个 agent 的职责，其它都不需要搞了）。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "responsibilities": {
                    "type": "object",
                    "description": (
                        "**产物的最后一节**：`{节点名: 职责}`（每个 ≤60 字）。"
                        "只给**会成为 agent 的节点**（顶层那几个），写满「干什么+"
                        "产出什么 / 边界（什么不归我）/ 什么信号找我」三件。**顺序要紧**："
                        "整棵树写在 `domains` 里、职责写在最后——被端点掐断时只丢"
                        "尾部几条描述（Runtime 用文件开头兜底），树完好"
                    ),
                },
                "domains": {
                    "type": "array",
                    "description": (
                        "现状结构树：按**功能语义**聚合成树，层级用 parent 表达"
                        "（子节点 parent=父节点 name，顶层留空）。每个节点用"
                        "**path（目录前缀或具体文件路径）声明它管哪一片**——"
                        "文件归属由代码按**最深的 path 前缀**机械匹配，你**不要**"
                        "逐文件列清单、也不要写文件描述（文件级描述由 Runtime 取"
                        "文件开头生成）。**职责只写给会成为 agent 的顶层节点**"
                        "（≤60 字）。**是否分裂由代码按这棵树决定**。"
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {
                                "type": "string",
                                "description": "节点名，由功能语义派生（如\"web 演示前端\"）",
                            },
                            "description": {
                                "type": "string",
                                "description": (
                                    "（**可省**，推荐走末尾的 `responsibilities`）"
                                    "节点职责/描述——供路由与多 agent 交流对象选择"
                                    "使用；写在这里的会被原样采用"
                                ),
                            },
                            "parent": {
                                "type": "string",
                                "description": "父节点 name；顶层节点留空",
                            },
                            "path": {
                                "type": "string",
                                "description": (
                                    "**本节点的范围**：一个目录前缀"
                                    "（`src/wovra/tools/`）或一个具体文件路径"
                                    "（`webui/index.html`）。文件按**最深前缀**"
                                    "机械归属（`src/wovra/tools/` 优先于 `src/`）。"
                                    "一个节点管多个不相邻路径时用 `paths` 数组。"
                                    "纯分组节点（只做分类、自己不管文件）可不填，"
                                    "但它的子节点要有"
                                ),
                            },
                            "paths": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "本节点管的多个路径（目录前缀或文件路径）",
                            },
                            "file": {
                                "type": "string",
                                "description": (
                                    "（兼容字段，一般不需要）单文件节点也可直接写"
                                    "这个文件路径或 [硬数据] 的编号（`L00`）；"
                                    "优先用 `path`"
                                ),
                            },
                            "chat_block_ids": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": (
                                    "挂在本节点工作线上的**保底块**（纯聊天/结论块）"
                                    "ID，逐字取自[分块地图]——真在讨论/决策这条线的"
                                    "块写这里（通常挂在对应叶子上，整条线共同的挂"
                                    "上层节点）；与任何节点都无关的（寒暄/身份确认）"
                                    "放 unassigned，归主 agent"
                                ),
                            },
                            "user_block_ids": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": (
                                    "挂在本节点工作线上的**用户块**（轮头之后的"
                                    "补充/修正发言，ID 形如 R25-B1）——属于哪条"
                                    "工作线就挂哪个节点；不属于任何工作线的放"
                                    "unassigned"
                                ),
                            },
                            "history_files": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": (
                                    "（**不必填**）非 LIVE 文件由 Runtime 按"
                                    "最近的活性兄弟机械挂载；确有特殊归属时"
                                    "才写（路径或 H 编号）"
                                ),
                            },
                            "file_notes": {
                                "type": "object",
                                "description": (
                                    "**文件的一句话描述**（≤30 字，格式 "
                                    "{\"路径\": \"描述\"}）——第一次地图就靠它建立："
                                    "之后每轮由干活的 agent 自己改（它读写删了文件就"
                                    "该顺手更新），不必等下一次整理。只写「这个文件是"
                                    "干什么的、给谁用」，不写改动史、不写轮次"
                                ),
                            },
                            "constraints": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "该节点现时有效的约束/红线",
                            },
                            "goal": {
                                "type": "string",
                                "description": "该节点服务的目标",
                            },
                            "block_ids": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": (
                                    "**例外声明**用的块 ID（逐字取自[分块地图]）："
                                    "只列归属与叶子文件不一致的块（跨节点块）。"
                                    "文件块默认按叶子 file 自动归属，不必逐个列；"
                                    "列了就是显式指定，优先于自动归属"
                                ),
                            },
                            "superseded": {
                                "type": "array",
                                "description": "生死标注：本节点内已被取代的工作线",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "block_ids": {
                                            "type": "array",
                                            "items": {"type": "string"},
                                        },
                                        "note": {
                                            "type": "string",
                                            "description": "被什么取代，如\"单文件期，被 R5 模块化拆分取代\"",
                                        },
                                    },
                                    "required": ["block_ids", "note"],
                                },
                            },
                        },
                        "required": ["name"],
                    },
                },
                "unassigned": {
                    "type": "object",
                    "description": (
                        "与任何工作线都无关的**保底块 / 用户块**——"
                        "如寒暄、身份确认、没有落到任何文件工作上的独立讨论——"
                        "放这里，归主 agent。**归宿完整性**：分块地图里的每个"
                        "保底块/用户块要么挂在某个节点上、要么在这里，一个不落"
                    ),
                    "properties": {
                        "block_ids": {
                            "type": "array", "items": {"type": "string"},
                        },
                        "topic": {
                            "type": "string",
                            "description": (
                                "这些闲聊的**主题**（≤20 字，如「工具吐槽与前端"
                                "灵感」）——树里主 agent 那个顶层节点就用它命名"
                                "（Runtime 物化；该节点归主 agent，不生成子 agent）"
                            ),
                        },
                        "reason": {"type": "string"},
                    },
                },
            },
            "required": ["domains"],
        },
    },
}

_TODO_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "todo",
        "description": (
            "阶段/工作项计划账本（深度恒 1：只存当前阶段，验收通过后才写"
            "下一阶段）。两层是两个维度（不是平铺的一条清单）：**阶段** = "
            "从最小可行起步、逐步增加功能，一次可验收的增量（带 M 编号）；"
            "**工作项** = 阶段内的拆解——阶段内直接做仍然复杂，必须先 "
            "add_item 拆成能逐步完成、逐步自证的工作项再动手，全部完成"
            "后才能 verify（结构闸门：未拆过工作项或带未完成工作项的验收"
            "会被拒绝）。验收 = 轮内**检查点**（带 {round, event, block} "
            "锚点），**不切轮**——轮 = 一次用户输入到最终回答。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "start_stage", "add_item", "check_item",
                        "drop_item", "defer_check", "verify_stage",
                        "drop_stage", "show",
                    ],
                    "description": "动作",
                },
                "goal": {
                    "type": "string",
                    "description": "start_stage：本阶段要交付什么",
                },
                "acceptance": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "start_stage：验收标准（可检验），必填，硬上限 3 条"
                        "（1-3 条）——入口是证据不是自述。按可验收的增量划"
                        "阶段，超 3 条拆成下一阶段"
                    ),
                },
                "text": {
                    "type": "string",
                    "description": (
                        "add/check/drop_item、defer_check 的条目文本。"
                        "add_item 写阶段内的工作项——开好阶段后第一件事"
                        "就是拆工作项（结构闸门：没拆过工作项的 verify 会被拒）"
                    ),
                },
                "evidence": {
                    "type": "string",
                    "description": (
                        "verify_stage：验收证据（测试输出/人工确认），"
                        "必填，禁止自述完成"
                    ),
                },
                "reason": {
                    "type": "string",
                    "description": "drop_stage：作废原因，必填（计划可证伪，留死亡原因）",
                },
            },
            "required": ["action"],
        },
    },
}

_LIST_AGENTS_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "list_agents",
        "description": (
            "拉取当前所有 agent 的职责划分（注册表机械渲染，零成本）："
            "id、名字、一句话职责、所有权文件域、状态。这是**跨 agent 的"
            "唯一公共信息**——隔离生效后你看不到其他职责域的内容，只"
            "看得到谁负责什么；路由与转交都以此为依据。"
        ),
        "parameters": {"type": "object", "properties": {}},
    },
}

_JOIN_WITH_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "join_with",
        "description": (
            "**会合**：宣布「这一轮还有谁也得出活」——把目标排进本轮的参与者队列。"
            "你在本轮把活干完后不立刻收轮，而是交给队列里的下一个 agent 接着干；"
            "**所有参与者都干完，这一轮才算闭合**。用它来做「对齐之后各自并行"
            "（当前串行交棒）把活干完」：先把协议谈定（consult/notify），再用 "
            "join_with 约定各自的任务边界，然后自己先干自己的那份。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "agent": {"type": "string", "description": "参与者（id 或名称）"},
                "task": {
                    "type": "string",
                    "description": "它要做的那一份（写清边界：它动哪些文件、产出什么）",
                },
            },
            "required": ["agent", "task"],
        },
    },
}

_RESPONSIBILITY_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "update_responsibility",
        "description": (
            "更新**你自己**的职责描述/目标/文件清单——**立即生效**。"
            "新建了文件、或这摊活的范围变了，就立刻把新文件补进自己的清单；"
            "否则下一次分裂之前它不属于任何域（路由找不到、材料归属也认不出）。"
            "不能把别人的文件写进自己的清单（归属不能抢）。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "description": {
                    "type": "string",
                    "description": (
                        "你这摊活的**职责说明书**（改了就生效，主 agent 只凭它路由）。"
                        "要点：①干什么+产出什么（点出关键文件/产物名）；"
                        "②**边界**——与最容易混的那个域怎么分（什么归我、什么不归我）；"
                        "③**什么信号落到我这里**（用户提到哪些文件/名字、问哪类问题该找我）。"
                        "别只写一句宽泛的话：写松了就会出现「同一话题换个人答」"
                        "（用户 2026-09-13 口径：职责说明书不能松散）"
                    ),
                },
                "goal": {"type": "string", "description": "这摊活的目标（可选）"},
                "add_files": {
                    "type": "string",
                    "description": "要补进自己清单的文件，逗号分隔（全路径）",
                },
                "remove_files": {
                    "type": "string",
                    "description": "要从自己清单移出的文件，逗号分隔（全路径）",
                },
                "file_notes": {
                    "type": "string",
                    "description": (
                        "**文件的一句话描述**（≤30 字，格式：路径=描述，多组用逗号分隔，"
                        "如 src/a.py=工具层边界判定）。你读/写/改了文件、或它的作用变了，"
                        "就顺手更新——**每轮都能改**，改完下一轮即生效（不必等整理）。"
                        "只写「这个文件是干什么的」，不写改动史、不写轮次"
                    ),
                },
                "note": {"type": "string", "description": "变更原因（可选，会留痕）"},
            },
        },
    },
}

_ROUTE_TO_SCHEMA: dict = {    "type": "function",
    "function": {
        "name": "route_to",
        "description": (
            "把**这一条用户消息**转给职责表里对应的 agent，并让它在本回合内"
            "直接接着干活——不用等你下一轮，也不经过你转述。用户原话原封"
            "转过去（这活本来就是它的，你不需要替它解释）。\n"
            "**只转「要动手的活」**（第 3 类：做、改、实现、提交…）。用户只是在"
            "**问**（是什么/为什么/能不能/给我讲讲）或只是在**阐述自己的想法**时"
            "**不要转**——就地回答；需要别的域的判断，用 consult 去问，别把整条"
            "消息转出去。\n"
            "用法：看懂用户要做什么后，对着职责表挑最相关的一个，调用本工具，"
            "然后**就此停手**（不要先自己动手、不要复述用户的话、不要写方案）；"
            "目标 agent 会在这同一个回合里把活干完并把结果直接给用户。\n"
            "挑不出来（没有哪个域的职责对得上、或完全不需要读任何域的代码）"
            "才自己处理。一句话跨两摊活时，挑**关联最大**的那个域转过去，"
            "由它自己去和别的域对齐（它比你有上下文）。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "agent": {
                    "type": "string",
                    "description": "目标 agent 的 id 或名称（职责表里有）",
                },
                "reason": {
                    "type": "string",
                    "description": (
                        "一句话路由理由（账本留痕用；不发给用户）。"
                        "例：改了 tools 层文件 → 工具层"
                    ),
                },
            },
            "required": ["agent", "reason"],
        },
    },
}

_NOTIFY_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "notify",
        "description": (
            "单向通信：把消息转交/通知/交接给另一个 agent，只发不等——"
            "对方下次被激活时收到。用于交接、通知事实、同步状态。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "agent": {"type": "string", "description": "目标 agent 的 id 或名称"},
                "message": {"type": "string", "description": "要转交/通知的内容"},
            },
            "required": ["agent", "message"],
        },
    },
}

_CONSULT_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "consult",
        "description": (
            "双向通信：就某问题与另一个 agent 对齐观点/提问，发并等回——"
            "对方以其职责视角回答（回答直接展示给用户并返回给你）。"
            "用于接口协商、事实核对、方案对齐。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "agent": {"type": "string", "description": "目标 agent 的 id 或名称"},
                "question": {"type": "string", "description": "要对齐/提问的内容"},
            },
            "required": ["agent", "question"],
        },
    },
}
