"""Agent 提示词与工具 schema（纯数据，逐字节自 agent.py 搬运）。

这些字符串是**模型可见面**：改动会使前缀缓存失效一次，重构期间
只允许搬运、不允许改一个字（重构纪律，见计划文档）。
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
    "[分裂分析判据]\n"
    "三步走：\n"
    "1. 保底块归属：无文件交互的纯聊天块（保底块/合并组）逐个判断——\n"
    "   与某文件域相关（真的在讨论/决策该域的事）→ 该 block_id 写进\n"
    "   对应域的 block_ids；无关的独立思想（寒暄/身份确认等没落到任何\n"
    "   文件工作上的讨论）→ 放 unassigned，归主 agent。\n"
    "2. 文件域聚合：把当前活性文件按**功能语义**聚合成层级——域名 +\n"
    "   描述 + 文件 + 目标 + 约束 + 块归属。**不是按路径**！路径只是\n"
    "   线索：同目录可属不同域、不同目录可属同域。被取代的前史归取代\n"
    "   者（superseded，防\"旧版一类新版一类\"）；同一大类下有多条\n"
    "   工作线时往下分层（parent 表达子域）——不能只给顶层。\n"
    "3. 分裂判定：找**能形成有效分裂的最浅层**——顶层节点 ≥2 个就用\n"
    "   顶层；顶层只有 1 个节点但它有 ≥2 个子节点，用该子层；全链单子\n"
    "   （所有文件本质同属一条工作线）则不可分。候选单元数不得超过\n"
    "   硬数据的活性文件数上限。\n"
    "**域归属完整性（强制）**：分块地图里的每个块都必须有归宿——\n"
    "在某个域的 block_ids 里、或放 unassigned（独立思想，归主 agent）；\n"
    "禁止出现无归宿的块。\n"
    "**粒度稳定（强制）**：域 = 可独立推进/交付的工作单元，不是\"每类\n"
    "文档一个域\"。独立成域必须同时满足 ①有独立的后续动作或交付目标\n"
    "②独立的文件域（不与其它域共享文件）。两条不满足 → 并入最相关域。\n"
    "同类收敛：同一阶段、同性质的产出物（评判/笔记/快照类文档）服务\n"
    "同一目标、无独立交付物时收敛为一个域——不要因为\"读的是不同源码\n"
    "文件\"（如提示词 vs 机制）把同一轮知识沉淀工作拆开；块数少的同类\n"
    "工作线尤应并入。同一批数据两次分析应给出同构域树（数量与层级一致）。\n"
    "域描述（description）必填：一句话说清每个域干什么、产出什么——\n"
    "供路由与多 agent 交流对象选择。\n"
    "完成后调用 submit_domains 提交（唯一出口，不要在正文输出 JSON）。\n"
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
    "4. 保底块【保底块】：（可能带「工具」）助手结论，简练一段。纯聊天\n"
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
    "五、轮头与块描述分工：轮头已承载\"用户说了什么、要什么、约束什么\"，\n"
    "   块描述不得复述用户输入，只写助手/工具对它的响应——做了什么、\n"
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

_ORG_DOMAINS_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "submit_domains",
        "description": (
            "提交现状归属与可分性分析。仅限后台整理阶段调用（作为分裂"
            "分析的唯一出口）；工作对话中调用无效，只返回说明文本。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "domains": {
                    "type": "array",
                    "description": (
                        "文件域：按**功能语义**聚合的现状清单（不是按路径！"
                        "路径只是线索——同目录可属不同域、不同目录可属同域）。"
                        "层级用 parent 表达（子域 parent=父域 name，顶层留空）——"
                        "同一大类下有多条工作线时要往下分层，不能只给顶层。"
                        "被后续重写/取代的早期版本不单独成域，作为取代者域的 "
                        "superseded 前史"
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {
                                "type": "string",
                                "description": "域名，由功能语义派生（如\"web 演示前端\"）",
                            },
                            "description": {
                                "type": "string",
                                "description": (
                                    "域描述（一句话说清这个域是干什么的、"
                                    "产出什么）——供路由与多 agent 交流对象"
                                    "选择使用"
                                ),
                            },
                            "parent": {
                                "type": "string",
                                "description": "父域 name；顶层域留空",
                            },
                            "file_domains": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "该域现时拥有的文件/目录（写全路径）",
                            },
                            "constraints": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "该域现时有效的约束/红线",
                            },
                            "goal": {
                                "type": "string",
                                "description": "该域服务的目标",
                            },
                            "block_ids": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": (
                                    "归属本域的块 ID（逐字取自[分块地图]）；"
                                    "被取代的前史块归属取代者所在域"
                                ),
                            },
                            "superseded": {
                                "type": "array",
                                "description": "生死标注：域内已被取代的工作线",
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
                        "required": ["name", "description", "file_domains", "block_ids"],
                    },
                },
                "unassigned": {
                    "type": "object",
                    "description": (
                        "独立思想块（唯一出口，2026-09-10 用户拍板）："
                        "与任何文件域都无关的纯聊天块/合并组——如寒暄、"
                        "身份确认、没有落到任何文件工作上的独立讨论——"
                        "放这里，归主 agent。判断标准：该块是否真的在"
                        "讨论/决策某个文件域的事？是 → 写进该域 block_ids；"
                        "否 → 放这里。**域归属完整性**：分块地图的每个块"
                        "要么在某个域的 block_ids 里、要么在这里，一个不落"
                    ),
                    "properties": {
                        "block_ids": {
                            "type": "array", "items": {"type": "string"},
                        },
                        "reason": {"type": "string"},
                    },
                },
                "split_assessment": {
                    "type": "object",
                    "description": (
                        "可分性判断：找**能形成有效分裂的最浅层**——顶层节点"
                        "≥2 个就用顶层；顶层只有 1 个节点但它有 ≥2 个子节点，"
                        "就用该子层；全链单子（所有文件本质同属一条工作线）"
                        "则不可分。候选单元数不得超过活性文件数（Runtime 上限）"
                    ),
                    "properties": {
                        "splittable": {"type": "boolean"},
                        "reason": {"type": "string"},
                        "proposal": {
                            "type": "object",
                            "description": "splittable=true 时的分裂提案",
                            "properties": {
                                "units": {
                                    "type": "array",
                                    "description": (
                                        "候选分裂单元（最浅可分层的节点），"
                                        "数量 ≤ 活性文件数"
                                    ),
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "name": {"type": "string"},
                                            "description": {
                                                "type": "string",
                                                "description": "单元描述（供路由/交流对象选择）",
                                            },
                                            "file_domains": {
                                                "type": "array",
                                                "items": {"type": "string"},
                                            },
                                            "block_ids": {
                                                "type": "array",
                                                "items": {"type": "string"},
                                            },
                                            "goal": {"type": "string"},
                                        },
                                        "required": ["name", "block_ids"],
                                    },
                                },
                                "rationale": {"type": "string"},
                            },
                        },
                    },
                    "required": ["splittable", "reason"],
                },
            },
            "required": ["domains", "split_assessment"],
        },
    },
}

_TODO_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "todo",
        "description": (
            "大步/小步计划账本（深度恒 1：只存当前大步，验收通过后才写"
            "下一大步）。两层是两个维度（不是平铺的一条清单）：大步 = "
            "阶段——从最小可行起步、逐步增加功能，一次可验收的增量；"
            "小步 = 阶段内的工作拆解——阶段内直接做仍然复杂，必须先 "
            "add_step 拆成能逐步完成、逐步自证的工作项再动手，全部完成"
            "后才能 verify（结构闸门：未拆过小步或带未完成小步的验收"
            "会被拒绝）。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "start_milestone", "add_step", "check_step",
                        "drop_step", "defer_check", "verify_milestone",
                        "drop_milestone", "show",
                    ],
                    "description": "动作",
                },
                "goal": {
                    "type": "string",
                    "description": "start_milestone：本大步要交付什么",
                },
                "acceptance": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "start_milestone：验收标准（可检验），必填，硬上限 3 条"
                        "（1-3 条）——入口是证据不是自述。里程碑驱动轮下验收即"
                        "轮边界（verify 时闭合当前轮），验收标准宽度直接决定轮"
                        "粒度与整理批次大小：按可验收的增量划大步，超 3 条拆成"
                        "下一大步"
                    ),
                },
                "text": {
                    "type": "string",
                    "description": (
                        "add/check/drop_step、defer_check 的条目文本。"
                        "add_step 写阶段内的工作项——开好大步后第一件事"
                        "就是拆小步（结构闸门：没拆过小步的 verify 会被拒）"
                    ),
                },
                "evidence": {
                    "type": "string",
                    "description": (
                        "verify_milestone：验收证据（测试输出/人工确认），"
                        "必填，禁止自述完成"
                    ),
                },
                "reason": {
                    "type": "string",
                    "description": "drop_milestone：作废原因，必填（计划可证伪，留死亡原因）",
                },
            },
            "required": ["action"],
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
