"""Agent 运行时 + V2 Context Runtime。

设计文档：docs/context-runtime-v2.md（定稿）。核心内容：

* Round/Event：Round 只在 AI 产出最终回答时闭合（开放轮会合并
  中断/无回复期间的多条用户输入，跨会话持久化）；Event 的 message
  原样保存（执行期不截断内容），Truncated 是零 LLM 成本的一行索引
  （供降档索引与整理输入）
* Organization（水位批量整理）：轮闭合不再逐轮整理——实测轻轮的整理
  费可以比干活成本还高。水位按**当前上下文窗口体量**（最近一次装配
  的估算，轮闭合时即本轮峰值）计量：达标且轮闭合才触发一次批量整理
  （调用次数 N→1 摊薄、State Patch 跨轮去重），后台线程执行不阻塞
  对话；产物先暂存，**下一轮开启时才生效**——本轮装配纹丝不动
  （连贯性 + 缓存前缀稳定），过程对用户静默；
  输入 = 触发时刻的装配原文快照（一字不动，纯追加骑前缀缓存）+ 尾部
  追加"分块地图 + 整理指令"；单次生成，与工作对话同一 tools 数组
  （submit_organization 常驻提交，read_full 退役——原文本来就在眼前）；
  输出 = Normalized 用户意图 + 关键约束 + **逐块信息量自适应描述** + Task
  State 补丁（2026-09-08 用户拍板：块描述承载完整细节；2026-09-09 修订
  ：篇幅随信息量伸缩——细节多字数多、细节少不长篇，低信息量块不强行
  堆字数；事件级精修索引只在无分块结构的旧轮回退使用）；
  **并行分裂分析**（2026-09-08 用户拍板）：同一装配快照第二路追加
  "分块地图 + 分裂分析指令"——现状清单（可运行单元）+ 块 → 域归属
  （生死标注：被取代的前史归取代者，防"旧版一类、新版一类"）+ 可分
  性判断（≥2 个互不重叠的活性文件域才算可分，话题不是理由），经
  submit_domains 提交；两路墙钟 ≈ max、失败互相独立，产物同批暂存
  同批生效；Level 0 只分析不分裂，分裂执行是 Level 1 的事
* Context Assembly：未整理轮次**全量原文**在上下文（执行期零分辨率
  损失的自然延伸），已整理轮次渲染为紧凑视图（👤用户原文 + 🎯意图 +
  📌关键约束 + 逐块细节描述；无块描述的旧整理轮回退精修事件索引）；
  视图替换只发生在整理生效（新轮开启）那一刻——**只有整理才破坏
  前缀**（2026-09-07 用户拍板，V2 三档滑窗废除：它每轮在装配中部
  改写历史，实测把命中率砸到 49.9%）；窗口保底是唯一天花板
* baseline 对照组：全量追加 + 80% × 窗口阈值压缩（市面惯例）
"""

import inspect
import json
import os
import queue
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Optional

from . import blocks as blocks_module
from . import lifecycle as lifecycle_module
from . import task as task_module
from . import tokens
from . import tools as tools_module
from . import truncate
from .llm import LLM, LLMStreamError, cached_tokens_of, reasoning_of
from .llm import APIError as _llm_APIError
from .task import Task, sanitize_surrogates
from .tools import (
    ask_user,
    check_background,
    delete_file,
    edit_file,
    get_current_time,
    glob_files,
    list_background,
    list_files,
    move_file,
    read_file,
    replace_lines,
    restore_file,
    run_background,
    run_command,
    search_files,
    stop_background,
    web_fetch,
    web_search,
    write_file,
)

MODE_MANAGED = "managed"
MODE_BASELINE = "baseline"

# 上下文预算：全部按窗口百分比设计（亿级使用规模），绝对值可覆盖
_DEFAULT_CONTEXT_LIMIT = int(os.environ.get("WOVRA_CONTEXT_LIMIT", "1000000"))
_COMPRESS_THRESHOLD = float(os.environ.get("WOVRA_COMPRESS_THRESHOLD", "0.8"))

# 维护性用途：异步执行、可能跨越轮次边界，成本单独记账（不混入 last_stats）
_MAINTENANCE_PURPOSES = ("organization", "compaction", "split")

# 纯只读工具：互不依赖，可在同一批工具调用里并发执行、按序记录
# （ask_user 会阻塞等用户输入，不参与并行）
_READ_ONLY_TOOLS = frozenset(
    {"read_file", "search_files", "list_files", "get_current_time",
     "glob_files", "web_fetch", "web_search", "list_background"}
)

# 水位批量整理：水位口径 = **当前上下文窗口体量**（最近一次装配的估算，
# 轮闭合时即本轮峰值）——2026-09-07 用户拍板，替代 V3 初版的"未整理
# 积压量"口径。达标且轮闭合才触发一次批量整理，轮进行中永不打扰；
# 产物暂存、下一轮开启才生效。小会话可能全程不触发——整理成本归零。
_ORG_GRACE_ROUNDS_DEFAULT = int(os.environ.get("WOVRA_ORG_GRACE_ROUNDS", "3"))
# 2026-09-09 用户拍板：3 轮全豁免（含巨轮），双条件否决已回滚
_ORG_COOLDOWN_ROUNDS_DEFAULT = int(os.environ.get("WOVRA_ORG_COOLDOWN_ROUNDS", "3"))
_ORG_WATERMARK_DEFAULT = int(os.environ.get("WOVRA_ORG_WATERMARK", "100000"))
# 维护路硬上限（2026-09-09 F 组实测）：超大基座上整理调用可深度思考
# 细水长流 23 分钟不完成（读超时不触发——token 在流），一次挂起借冷却
# 计数堵死整条管线。硬上限到点判 failed 解锁，冷却后重试。
_ORG_MAINT_TIMEOUT_DEFAULT = float(os.environ.get("WOVRA_MAINT_TIMEOUT", "900"))

# 整理产出的提交契约（工具常驻：整理调用与工作对话共用同一 tools 数组，
# 前缀序列化恒定，缓存才能常骑——2026-09-08 用户拍板）。字段语义写在
# schema description 里，是唯一事实源；整理指令只补质量要求与批次声明。
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

# 压缩元信息：整理指令头部的静态说明（轮次范围动态注入）。给整理模型
# 和后续续跑 agent 提供术语定义——LIVE/DEAD/B1 这类机制词不解释会让
# 上下文之外的读者误判（评审反馈：DEAD 被理解为失败，实际是"已闭环/
# 清理"）。
_ORG_META_INFO = (
    "[压缩元信息]\n"
    "- 本次整理范围：R{seq_list}。连续纯聊天轮已合并为组（如 R1-2），"
    "组内共用一个块描述、一个综合意图。\n"
    "- 状态定义：LIVE=文件当前有效（内容以磁盘为准，需要细节时重新读）；"
    "DEAD=已删除/已闭环/测试已清理（不是失败）；B1=该轮第一个块。\n"
    "- 原文都在会话历史里，本视图只降分辨率不删事实；后续需要细节时按"
    "块/事件取回原文。\n"
)


def _clip_quote(text: str, limit: int = 60) -> str:
    """用户原话锚点：压平换行并截断，供分块地图每轮首行展示。"""
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[:limit] + "…"


# 分裂分析判据（2026-09-10 用户定稿）：三步走 + 语义聚合（不按路径）+
# 最浅可分层。硬数据（活性文件清单/上限）由 Runtime 注入，LLM 只做
# 语义判断（块归属、域命名/描述、层级关系）。
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

# 整理指令的静态标签说明（2026-09-09 用户定稿，docs/org-prompt-v1.md）。
# 标签 = 分辨率指令不是重要性评分：写多细、写什么由标签决定，模型不做
# "哪些重要"的判断——消除 org 的"应该怎么写"思考（15K 思考撞输出上限
# 的病根）。模块级常量不随批次变化：追加式指令，前缀缓存稳定。
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

# 分裂分析产出契约（第二个常驻工具）：现状归属 + 可分性判断。
# 判据（2026-09-08 用户拍板）：分裂单位 = 可独立运行的关注面；块的归属
# 跟着它触达工件的现世走（被取代的前史归取代者，防止"旧版一类、新版
# 一类"）；话题不同永远不构成分裂理由——只有现状清单出现 ≥2 个互不
# 重叠的活性文件域才算可分。粗分裂优先：首分裂只分零状态 vs 工作。
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
# 大步/小步计划账本（2026-09-08 用户拍板，设计稿 todo-milestone-tool.md）：
# 深度恒 1——只存当前大步，验收通过后才写下一大步（滚动计划，化解
# "计划两层论"）。人工验收分两型：阻塞型（不验收进行不下去）停轮等
# 反馈；非阻塞型（美观等主观项）defer 挂起继续干，大步收尾一次性呈交，
# 未决项转 experiments 不搁置。
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
# 跨 agent 通信（机制五，2026-09-08 实现为 Level 0.5 骨架）：
# 单向 notify（只发不等，落收件箱、激活时送达）+ 双向 consult（发并
# 等回，目标以其职责视角回答）。展示纪律：子 agent 的回答打标签直接
# 流式进用户窗口（不回路由主 agent 转述），思考全局单行。
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
# 单轮工具循环的默认步数上限：安全网而非配额——尽量不在步数上限制
# LLM，真超限也只是开放轮等待 \继续，不废工作。2026-09-08 用户实测
# 连续两次撞 60（DeepSeek harness 同任务用过 112 步），上调至 200
_DEFAULT_MAX_TURNS = int(os.environ.get("WOVRA_MAX_TURNS", "200"))

# 工具名 → 进度提示的动作词（"正在<动作>…"）
_ACTION_WORDS = {
    "write_file": "写入文件",
    "edit_file": "修改文件",
    "replace_lines": "按行替换文件",
    "delete_file": "删除文件",
    "move_file": "移动文件",
    "restore_file": "回滚文件版本",
    "run_command": "运行命令",
    "read_file": "读取文件",
    "search_files": "搜索内容",
    "list_files": "查看目录",
    "get_current_time": "获取当前时间",
    "expand_history": "展开历史",
    "run_background": "后台启动命令",
    "check_background": "查看后台输出",
    "stop_background": "停止后台任务",
    "glob_files": "按模式找文件",
    "web_fetch": "抓取网页",
    "web_search": "网页搜索",
    "ask_user": "询问用户",
    "list_background": "列出后台任务",
}

# 相关性筛选在 V2 中不实现（预算充足时所有浓缩视图直接加载），
# 保留函数体注释占位：V3 方向见设计文档第 11 节

_JSON_TYPES = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
}


class Agent:
    """Agent 运行时（工具循环）+ V2 上下文生命周期管理。"""

    def __init__(
        self,
        llm: Optional[LLM] = None,
        system_prompt: str = "",
        tools: tuple = (),
        max_turns: Optional[int] = None,
        task: Optional[Task] = None,
        context_mode: str = MODE_MANAGED,
        context_limit: Optional[int] = None,
        async_organization: bool = False,
        org_watermark: Optional[int] = None,
        org_grace_rounds: Optional[int] = None,
        org_cooldown_rounds: Optional[int] = None,
        org_maint_timeout: Optional[float] = None,
        on_tool_call: Optional[Callable[[str, str], None]] = None,
        on_tool_result: Optional[Callable[[str, str], None]] = None,
        on_progress: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.llm = llm or LLM()
        self.max_turns = max_turns or _DEFAULT_MAX_TURNS
        # 同步实时进度回调（主线程执行）：等待模型、工具动作的即时提示
        self.on_progress = on_progress
        self.task = task
        self.context_mode = context_mode
        self.context_limit = context_limit or _DEFAULT_CONTEXT_LIMIT
        # 整理是否异步执行：chat 模式开（不阻塞对话），run/测试用同步（确定性）
        self.async_organization = async_organization
        # V3 水位批量整理参数（水位 = 未整理轮原始内容体量阈值；2026-09-09
        # 用户拍板：批次上限删除——触发只看窗口是否到水位，到水位收编全部
        # 未整理轮）
        self._org_watermark = (
            _ORG_WATERMARK_DEFAULT if org_watermark is None else org_watermark
        )
        # 保护机制（2026-09-08 用户拍板）：宽限期 + 冷却间隔。
        # 宽限 = 会话前 N 轮硬豁免维护——开头几轮（项目导览/目标陈述）是
        # 判据二"解释现状的最小历史"的核心，且大项目首查容易瞬间装满，
        # 不豁免会刚开场就压缩（窗口保底紧急折叠不受豁免，是另一条线）。
        # 冷却 = 两次维护之间的最小轮距，适配大小不同的起点、防高频。
        # 分裂后水位按 agent 各自计量、有效容量随分裂增长，阈值无需上调
        # ——保护旋钮主要服务分裂前的单体阶段。
        self._org_grace = (
            _ORG_GRACE_ROUNDS_DEFAULT if org_grace_rounds is None else org_grace_rounds
        )
        self._org_cooldown = (
            _ORG_COOLDOWN_ROUNDS_DEFAULT
            if org_cooldown_rounds is None
            else org_cooldown_rounds
        )
        self._org_maint_timeout = (
            _ORG_MAINT_TIMEOUT_DEFAULT if org_maint_timeout is None else org_maint_timeout
        )
        # 已入队/整理中的轮次 seq：命中率的计量口径里它们不算"未整理"，
        # 避免批量整理排队期间被下一次触发重复收编
        self._org_inflight: set[int] = set()
        # 整理代次（2026-09-10 用户拍板：视图只保留最近 3 批整理，更早的
        # 按文件状态折叠）：每次成功整理 +1，批次轮打 org_generation 落盘；
        # 旧轮无该字段视为第 1 代（最老，优先折叠）。计数在本类里就地
        # 恢复（见 rounds 加载处）——重启后从 0 重来会与磁盘旧批次撞号，
        # 折叠判定反转（新批次被当最老折叠、旧批次反倒全量）。
        self._org_generation = 0
        # 子任务派发板：每轮刷新的机械状态行（进程/账本/升级计数），
        # 注入装配尾部——主 agent 每轮都"看得见"子任务进展
        self.on_tool_call = on_tool_call
        self.on_tool_result = on_tool_result
        # 后台动作（如 Round 整理）耗时较长，状态消息进入 feed，
        # 由主线程在安全时机（提示输入前）统一打印——后台线程绝不直接
        # print：会打碎输入行，且 patch_stdout 会吞掉 ANSI 颜色码（踩过的坑）
        self._status_feed: list[str] = []

        self._bind_globals()

        self.tools: dict[str, Callable] = {}
        # 传给子 agent 的工具集（纯模块函数）。expand_history 与组织工具
        # 由各 Agent 自己注册、绑定各自的 Task——不能从父级继承，否则
        # 子 agent 的 expand_history 会读到父任务的历史
        self._tool_fns = tuple(tools)
        self._schemas: list[dict] = []
        for fn in tools:
            self.register(fn)

        self.turn_count = 0
        self.rounds: list[dict] = [dict(r) for r in (task.rounds if task else [])]
        # 代次计数就地恢复：每次成功整理 +1，重启后必须从磁盘上的最大值
        # 续起——从 0 重来会与旧批次撞号，折叠判定随即反转（新批次被当
        # 最老折叠、旧批次反倒全量）。口径与装配端一致：_assemble_messages
        # 把无 org_generation 的已整理轮视为第 1 代，故恢复时同样按
        # "done 且无字段 → 1"计入，不能只读字段。
        self._org_generation = max(
            (
                r.get("org_generation")
                or (1 if r.get("org_state") == "done" else 0)
                for r in self.rounds
            ),
            default=0,
        )
        self.current_round: Optional[dict] = None
        self.messages: list[dict] = []
        self.system_prompt = system_prompt

        # 异步整理：单线程 FIFO 维护管线（History Maintenance Pipeline）
        self._org_queue: queue.Queue = queue.Queue()
        self._org_thread: Optional[threading.Thread] = None
        self._save_lock = threading.Lock()
        # 维护账本：整理/压缩的用量单独累计（异步、跨轮次边界），
        # 由每次 usage 记账时统一取走；last_maint 存最近一次快照供展示
        self._maint_usage = {
            "organization": {"prompt": 0, "completion": 0, "total": 0, "seconds": 0.0},
            "compaction": {"prompt": 0, "completion": 0, "total": 0, "seconds": 0.0},
            "split": {"prompt": 0, "completion": 0, "total": 0, "seconds": 0.0},
        }
        self._maint_lock = threading.Lock()
        self.last_maint: dict = {}
        # 最近一次装配的上下文体量估算（每步更新；轮结束时即该轮峰值）
        self.last_context_estimate = 0
        # 端点不支持整理参数的降级提示：每次会话只提示一次，避免每轮刷屏
        self._degrade_warned = False
        # 最近一次流式调用的 finish_reason（stop/length/tool_calls/未返回）：
        # 流被掐断时 usage 也缺失，这是唯一诊断线索（空响应防护用）
        self._last_finish_reason: Optional[str] = None

        # baseline 记账：累计输入 token（触发阈值压缩）
        self._baseline_prompt_used = task.baseline_prompt_used if task else 0

        self.last_stats = self._fresh_stats()

        if self.context_mode == MODE_MANAGED:
            self.register(self.expand_history)
            # 整理提交工具常驻：所有请求的 tools 数组恒定（工作对话与整理
            # 调用同序列化），前缀缓存才不会在 tools 区分叉。工作期误调用
            # 由方法体守卫拒绝（见 submit_organization / submit_domains）。
            self.register(self.submit_organization, schema=_ORG_SUBMIT_SCHEMA)
            self.register(self.submit_domains, schema=_ORG_DOMAINS_SCHEMA)
            # 大步/小步计划账本（工作工具，深度恒 1，见 todo-milestone-tool.md）
            self.register(self.todo, schema=_TODO_SCHEMA)
            # 跨 agent 通信（机制五）：单向 notify / 双向 consult
            self.register(self.notify, schema=_NOTIFY_SCHEMA)
            self.register(self.consult, schema=_CONSULT_SCHEMA)
            # 水位批量整理：上次会话遗留的未整理轮（含崩溃时的 pending /
            # failed）由下一次轮闭合触发时一并收编——加载时不立即补跑
            # （小会话可能永远不需要整理）

    def _bind_globals(self) -> None:
        """把进程级全局绑定对准本会话（审计记录器、后台任务归属）。

        在 Agent 构造时对准本会话；后台任务按会话归属治理。
        """
        tools_module.set_audit_recorder(
            lambda detail: self.task.record("file_change", detail) if self.task else None
        )
        # 后台任务按会话归属：启动/查看/停止都限定在本会话内
        tools_module.set_current_session(self.task.id if self.task else None)

    def _fresh_stats(self) -> dict:
        return {
            "seconds": 0.0,
            "turn": self.turn_count,
            "llm_calls": 0,
            "tool_calls": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "reasoning_tokens": 0,
            "total_tokens": 0,
            "cached_tokens": 0,
            "cache_miss_tokens": 0,
            # 首 token 延迟（TTFT）：基座的第二张面孔（延迟乘数）的仪器。
            # seconds = 本轮各步 TTFT 之和（用户感知的等待），max = 最卡的一步
            "ttft_seconds": 0.0,
            "ttft_max": 0.0,
            "mode": self.context_mode,
            "purpose": {
                "working": {"prompt": 0, "completion": 0, "total": 0, "seconds": 0.0},
                "organization": {"prompt": 0, "completion": 0, "total": 0, "seconds": 0.0},
                "compaction": {"prompt": 0, "completion": 0, "total": 0, "seconds": 0.0},
            },
            "prompt_breakdown": dict.fromkeys(tokens.CATEGORIES, 0),
        }

    def register(self, fn: Callable, schema: Optional[dict] = None) -> None:
        """把一个 Python 函数注册为模型可调用的工具。

        schema 缺省由函数签名自动生成；传入则整体覆盖（submit_organization
        的嵌套产出契约用手工 schema，_schema_of 只会生成扁平结构）。
        """
        if fn.__name__ in self.tools:
            raise ValueError(f"工具重复注册: {fn.__name__}")
        self.tools[fn.__name__] = fn
        self._schemas.append(schema or _schema_of(fn))

    # ---- Round 生命周期：开放 → 闭合 ----------------------------------------

    def submit_organization(
        self, rounds: Optional[list] = None, state_patch: Optional[dict] = None
    ) -> str:
        """[守卫] 整理产出的提交工具。

        整理阶段（_organize_rounds）捕获其 tool_call 参数直接解析，不经
        工具执行；这里的方法体只在**工作对话误调用**时跑到——返回说明
        文本，不产生任何副作用。
        """
        return (
            "submit_organization 仅由后台整理流程消费（整理阶段捕获其调用参数，"
            "不经过工具执行）。当前处于工作对话，本次调用被忽略，无副作用。"
        )

    def submit_domains(
        self, domains: Optional[list] = None,
        unassigned: Optional[dict] = None,
        split_assessment: Optional[dict] = None,
    ) -> str:
        """[守卫] 分裂分析的提交工具。

        整理阶段（_split_rounds）捕获其 tool_call 参数直接解析，不经
        工具执行；工作对话误调用只返回说明文本，无副作用。
        """
        return (
            "submit_domains 仅由后台分裂分析消费（分析阶段捕获其调用参数，"
            "不经过工具执行）。当前处于工作对话，本次调用被忽略，无副作用。"
        )

    def todo(
        self,
        action: str,
        goal: str = "",
        acceptance: Optional[list] = None,
        text: str = "",
        evidence: str = "",
        reason: str = "",
    ) -> str:
        """大步/小步计划账本（深度恒 1 的滚动计划）。

        两层是两个维度（2026-09-11 用户拍板）：大步 = 阶段（最小可行 →
        逐步增加功能），小步 = 阶段内的工作拆解——阶段内工作直接做仍然
        复杂，必须拆小步推进。与"平铺 todo（一个任务拆几步）"的本质
        区别即在此，故 verify 设结构闸门：从未拆过小步、或有未完成小步
        未交代 → 拒绝（E/F 组实测 8 个阶段 0 次小步，模型会直接跳过）。

        人工验收两型（2026-09-08 用户拍板）：阻塞型 = 不验收进行不下去，
        停轮等反馈（开放轮语义，\\c 续跑）；非阻塞型（美观等主观项）=
        defer_check 挂起继续干，大步收尾一次性呈交，未决转 experiments
        不搁置。
        """
        if self.task is None:
            return "todo：当前无任务绑定。"
        todo = self.task.todo or {}
        milestone = todo.get("milestone")
        steps = todo.get("steps") or []
        deferred = milestone.get("deferred") or [] if milestone else []

        if action == "start_milestone":
            if milestone:
                return (
                    f"已有开启中的大步：{milestone['goal']}（深度恒 1："
                    "先 verify_milestone 或 drop_milestone，再开新大步）"
                )
            if not goal.strip() or not (acceptance or []):
                return "start_milestone 需要 goal 与 acceptance（验收标准必填——入口是证据不是自述）"
            if len(acceptance) > 3:
                return (
                    f"acceptance 共 {len(acceptance)} 条，超过 3 条硬上限（1-3 条）"
                    "——一个大步只承载一次可验收增量：里程碑驱动轮下验收即轮"
                    "边界，大步越宽轮越晚闭合、整理批次越大（F 组实测 4-6 条"
                    "打包的大步把上下文堆到 325K）。把超出的验收标准拆成下一"
                    "大步，重试 start_milestone。"
                )
            todo["milestone"] = {
                "goal": goal.strip(),
                "acceptance": [str(a) for a in acceptance],
                "started_seq": (
                    self.current_round["seq"]
                    if self.current_round is not None
                    else len(self.rounds) + 1
                ),
                "deferred": [],
                # 是否拆过小步（结构闸门的判据）：verify 前必须为 True。
                # 与 steps 是否为空分开——全部完成后 steps 会被清空/删除，
                # 用 planned 记录"规划过"这一事实，模型 add→drop 全清后
                # 仍可验收（有据可查），但从未拆过会被拒。
                "planned": False,
            }
            todo["steps"] = []
        elif action == "add_step":
            if not milestone:
                return "无开启中的大步——先 start_milestone。"
            if not text.strip():
                return "add_step 需要 text。"
            steps.append({"text": text.strip(), "done": False})
            todo["steps"] = steps
            todo["milestone"]["planned"] = True
        elif action in ("check_step", "drop_step"):
            if not milestone:
                return "无开启中的大步。"
            hit = next((s for s in steps if s["text"] == text.strip()), None)
            if hit is None:
                listing = "\n".join(
                    f"  [{'x' if s['done'] else ' '}] {s['text']}" for s in steps
                ) or "  （空）"
                return f"未找到小步：{text.strip()}\n当前小步：\n{listing}"
            if action == "check_step":
                hit["done"] = True
            else:
                steps.remove(hit)
            todo["steps"] = steps
        elif action == "defer_check":
            if not milestone:
                return "无开启中的大步。"
            if not text.strip():
                return "defer_check 需要 text（待人工验收项）。"
            deferred.append(text.strip())
            todo["milestone"]["deferred"] = deferred
            return (
                f"已挂起人工验收（非阻塞）：{text.strip()}\n"
                f"本大步累积 {len(deferred)} 项，将在 verify_milestone 时一次性呈交；"
                "期间继续工作。"
            )
        elif action == "verify_milestone":
            if not milestone:
                return "无开启中的大步。"
            if not evidence.strip():
                return (
                    "verify_milestone 需要 evidence（验收证据：测试输出/人工确认）"
                    "——禁止自述完成。"
                )
            # 结构闸门（2026-09-11 用户拍板）：大步 = 阶段、小步 = 阶段内
            # 的拆解——阶段内的活直接做仍然复杂，必须先拆。E/F 组实测
            # 8 个阶段 0 次小步：不加门模型会跳过拆解直接闷头做。
            # 判据用 planned（拆过小步这件事）而非 steps 非空——后者在
            # 全部完成/作废后为空，会误伤正常验收。
            if not milestone.get("planned"):
                return (
                    "verify 被拒：本大步还没有拆过小步。大步 = 阶段（最小可行"
                    " → 逐步增加功能），阶段内的工作直接做仍然复杂——先用 "
                    "add_step 拆成能逐步完成、逐步自证的工作项，全部完成后再"
                    "验收（账本要能说明这个阶段做了什么，哪怕只有一条）。"
                )
            undone = [s["text"] for s in steps if not s["done"]]
            if undone:
                listing = "\n".join(f"  [ ] {t}" for t in undone)
                return (
                    f"verify 被拒：还有 {len(undone)} 条未完成小步：\n{listing}\n"
                    "先 check_step 完成它们，或 drop_step 说明为什么不用做了"
                    "（计划可证伪，废弃留痕），再验收。"
                )
            entry = f"[大步] {milestone['goal']}（验收：{evidence.strip()}）"
            self.task.apply_state_patch({"completed": [entry]})
            if deferred:
                # 非阻塞人工验收未决项不搁置：转 experiments（人当传感器
                # 通道），会话收尾一次性呈交
                self.task.apply_state_patch({
                    "experiments": [f"[待人工验收] {t}" for t in deferred]
                })
            todo.setdefault("history", []).append({
                "goal": milestone["goal"],
                "evidence": evidence.strip(),
                "started_seq": milestone.get("started_seq"),
                "closed_seq": len(self.rounds),
            })
            tail_note = (
                f"另有 {len(deferred)} 项非阻塞人工验收已转入待办实验"
                "（未验收不搁置）" if deferred else ""
            )
            todo["milestone"] = None
            todo["steps"] = []
            self.task.todo = todo
            self.task.save()
            # 里程碑驱动轮（2026-09-08 用户拍板）：大步验收 = 检查点 =
            # 轮边界——闭合当前轮并开新轮续写同一回合。D 组实证：一个
            # 229 步巨型轮跑完全程无闭合，水位机制全场未出力。闭合触发
            # 水位检查。说明文本放新轮 user_input 与工具结果（不在
            # assistant tool_call 与 tool 消息之间插事件——严格端点会拒）。
            if self.current_round is not None:
                closed_seq = self.current_round["seq"]
                checkpoint_note = (
                    "[运行时] 大步验收通过，轮次在此闭合"
                    "（里程碑驱动轮：检查点 = 轮边界）。"
                )
                self.close_round()
                if self._open_or_reuse_round(checkpoint_note):
                    self._promote_org_results()
                self._persist_rounds()
                # 轮边界必须在窗口里可见——否则用户体感"一轮"与账本的
                # 多轮对不上（F 组实测：5 次里程碑闭合全程静默）
                if self.on_progress:
                    self.on_progress(
                        f"🏁 大步『{milestone['goal']}』验收闭合——轮次 R{closed_seq} 归档，开新轮续写"
                    )
            return (
                f"大步已验收：{milestone['goal']}\n证据：{evidence.strip()}\n"
                + (tail_note + "\n" if tail_note else "")
                + "轮次已在此闭合并开启新轮（里程碑驱动轮）。"
                "现在可以 start_milestone 写下一大步。"
            )
        elif action == "drop_milestone":
            if not milestone:
                return "无开启中的大步。"
            if not reason.strip():
                return "drop_milestone 需要 reason（作废原因——计划可证伪，留死亡原因）。"
            todo.setdefault("history", []).append({
                "goal": milestone["goal"],
                "evidence": f"作废：{reason.strip()}",
                "started_seq": milestone.get("started_seq"),
                "closed_seq": len(self.rounds),
            })
            if deferred:
                self.task.apply_state_patch({
                    "experiments": [f"[待人工验收·大步作废遗留] {t}" for t in deferred]
                })
            todo["milestone"] = None
            todo["steps"] = []
        elif action == "show":
            if not milestone:
                hist = todo.get("history") or []
                return "无开启中的大步。" + (
                    f"\n已验收 {len(hist)} 个大步。" if hist else ""
                )
            lines = [
                f"当前大步：{milestone['goal']}（自 R{milestone['started_seq']}）",
                "验收标准：\n" + "\n".join(f"  - {a}" for a in milestone["acceptance"]),
            ]
            if steps:
                lines.append("小步：\n" + "\n".join(
                    f"  [{'x' if s['done'] else ' '}] {s['text']}" for s in steps
                ))
            if deferred:
                lines.append("挂起人工验收：\n" + "\n".join(f"  - {t}" for t in deferred))
            return "\n".join(lines)
        else:
            return f"未知动作: {action}"

        self.task.todo = todo
        self.task.save()
        cur = todo.get("milestone")
        if cur:
            done_n = sum(1 for s in todo.get("steps") or [] if s["done"])
            return f"OK：{action}（{cur['goal']}｜小步 {done_n}/{len(todo['steps'])}）"
        return f"OK：{action}"

    def _registry_entry(self, ref: str) -> Optional[dict]:
        """按 id 或名称查注册表条目。"""
        registry = (self.task.registry if self.task is not None else None) or []
        for entry in registry:
            if entry.get("id") == ref or entry.get("name") == ref:
                return entry
        return None

    def notify(self, agent: str, message: str) -> str:
        """单向通信（机制五）：转交/通知/交接，只发不等——落目标收件箱，
        对方下次被激活（consult/路由）时送达。"""
        if self.task is None:
            return "notify：当前无任务绑定。"
        entry = self._registry_entry(agent)
        if entry is None:
            known = ", ".join(
                f"{e.get('id')}({e.get('name')})" for e in (self.task.registry or [])
            )
            return f"未找到 agent：{agent}。现存：{known}"
        entry.setdefault("inbox", []).append({
            "from": "主agent", "message": message.strip(),
        })
        self.task.save()
        if self.on_progress:
            self.on_progress(f"📨 单向 → {entry.get('name')}：{message.strip()[:60]}")
        return f"已单向送达 {entry.get('name')} 的收件箱（只发不等，对方激活时收到）。"

    def consult(self, agent: str, question: str) -> str:
        """双向通信（机制五）：发并等回——切到目标职责视角回答一次，
        回复打标签直接流式进用户窗口（不回路由主 agent 转述），同时
        返回给调用方。目标收件箱随激活送达。"""
        if self.task is None:
            return "consult：当前无任务绑定。"
        entry = self._registry_entry(agent)
        if entry is None:
            known = ", ".join(
                f"{e.get('id')}({e.get('name')})" for e in (self.task.registry or [])
            )
            return f"未找到 agent：{agent}。现存：{known}"
        if entry.get("id") == "A":
            return "不要 consult 主 agent（那就是你自己）——需要用户输入请用 ask_user。"

        system = (
            f"你是 {entry.get('name')}（{entry.get('id')}）——"
            f"{entry.get('description', '')}。"
            f"所有权文件域：{', '.join(entry.get('file_domains') or []) or '未划定'}。"
            "主对话正就以下问题与你对齐：用你的职责视角回答，只答职责内"
            "的内容，简明扼要，不要客套。"
        )
        msgs: list[dict] = [{"role": "system", "content": system}]
        for item in entry.get("inbox") or []:
            msgs.append({
                "role": "user",
                "content": f"[收件箱·来自{item.get('from')}] {item.get('message')}",
            })
        if entry.get("inbox"):
            entry["inbox"] = []  # 已送达
        state = self.task.get_state()
        if state.goal or state.current_status:
            msgs.append({
                "role": "user",
                "content": f"[任务背景] 目标：{state.goal}；现状：{state.current_status}",
            })
        msgs.append({"role": "user", "content": question.strip()})

        # 子 agent 流式展示：思考沿用全局单行；回答打标签直达用户窗口
        base = getattr(self, "_stream_cbs", None) or {}
        think_cb, answer_cb = base.get("thinking"), base.get("answer")
        label = f"[{entry.get('name')}]"
        first = [True]

        def sub_answer(text: str) -> None:
            if answer_cb:
                if first[0]:
                    answer_cb("\n" + label + " ")
                    first[0] = False
                answer_cb(text)

        reply, _ordered, _usage = self._stream_call(
            msgs, tools=None, purpose="working",
            on_thinking=think_cb, on_answer_delta=sub_answer,
        )
        self.task.save()
        return f"{entry.get('name')} 的回复：{reply.strip()}"

    def _open_or_reuse_round(self, user_input: str) -> bool:
        """开启新 Round，或续上未闭合的开放 Round（V2 闭合规则）。

        上一轮若因中断/异常/无回复而未闭合（end_state=open），
        本轮输入并入同一个 Round——直到 AI 产出最终回答才算完整一轮。
        返回是否开启了新 Round：开启时要把上一轮暂存的整理产物生效
        （_promote_org_results），续轮则不动——本轮装配必须保持原样。
        """
        last = self.rounds[-1] if self.rounds else None
        if last is not None and last.get("end_state") in ("", "open"):
            self.current_round = last
            # 协议消息从事件的 Full 中重建（它们就是事实来源）
            self.messages = [e["message"] for e in last["events"]]
            return False
        seq = len(self.rounds) + 1
        self.current_round = {
            "seq": seq,
            "user_input": {"original": user_input, "normalized": ""},
            "events": [],
            "refined_index": {},
            "end_state": "open",
            "org_state": "",
        }
        self.rounds.append(self.current_round)
        self.messages = []
        return True

    def _record_event(self, type: str, message: dict, tool_name: str = "") -> dict:  # noqa: A002
        """把一条协议消息登记为 Event（生成 ID 与 Truncated 索引行）。"""
        if self.current_round is None:
            self.messages.append(message)
            return {"id": "", "message": message}
        seq = len(self.current_round["events"]) + 1
        event_id = f"R{self.current_round['seq']}-E{seq:02d}"
        event = truncate.make_event(event_id, type, message, tool_name=tool_name)
        self.current_round["events"].append(event)
        self.messages.append(event["message"])
        return event

    def _emit_status(self, text: str) -> None:
        """后台线程往状态队列里投递一条消息（线程安全：list.append 原子）。"""
        self._status_feed.append(text)

    def drain_status(self) -> list[str]:
        """主线程取走全部待打印的后台状态（打印时机由主线程决定）。"""
        out = []
        while self._status_feed:
            out.append(self._status_feed.pop(0))
        return out

    def _persist_rounds(self) -> None:
        if self.task is not None:
            with self._save_lock:
                self.task.rounds = self.rounds
                self.task.baseline_prompt_used = self._baseline_prompt_used
                self.task.save()

    def close_round(self) -> None:
        """闭合当前 Round（仅最终回答路径调用）；managed 模式做水位检查。

        水位口径 = 当前上下文窗口体量（本轮装配峰值 last_context_estimate）：
        达标且轮已闭合才批量整理，轮进行中永不打扰；整理产物暂存到
        下一轮开启才生效（_promote_org_results），对用户静默。
        """
        if self.current_round is None:
            return
        self.current_round["end_state"] = "completed"
        # Block 结构化（机制一，零 LLM）：以写/改为截止的确定性分块，
        # 随轮次落盘——整理输入、文件地图、追溯导航都吃这份结构
        self.current_round["blocks"] = blocks_module.segment_round(self.current_round)
        self.current_round = None
        self._persist_rounds()
        if self.context_mode == MODE_MANAGED and self.task is not None:
            self._maybe_organize_batch()

    def finalize_round(self, end_state: str = "open") -> None:
        """CLI 异常/中断路径：Round 保持开放（不闭合、不整理），仅持久化。

        中断/超限轮的成本照记（带"轮未闭合"标记）——失败尝试花的
        也是真金白银，而且正是上下文管理最该优化的对象。
        """
        if self.current_round is None:
            return
        self.current_round["end_state"] = "open"
        self._usage_record_and_drain(closed=False)
        self._persist_rounds()
        self.current_round = None

    # ---- 主循环 ---------------------------------------------------------------

    def run(
        self,
        user_input: str,
        on_thinking: Optional[Callable[[str], None]] = None,
        on_answer_delta: Optional[Callable[[str], None]] = None,
    ) -> str:
        """处理一条用户输入（开启/续上 Round 并完成工作），返回最终回答。"""
        new_round = self._open_or_reuse_round(user_input)
        if new_round:
            # 新 Round 开启才让上一轮暂存的整理产物生效（精修索引/
            # Normalized/状态补丁）；续上开放轮则不动——本轮对话期间
            # 装配必须保持原样（连贯性 + 缓存前缀稳定）
            self._promote_org_results()
        # 轮次与会话绑定（rounds 的 seq 随会话持久化）——进程内计数会在
        # 退出重开后归零，长会话的"第 N 轮"就错了（实测教训）
        self.turn_count = self.current_round["seq"]
        self._record_event("user", {"role": "user", "content": user_input})
        if self.task is not None:
            self.task.record("user_input", user_input)
            self._persist_rounds()
        return self._work_loop(on_thinking, on_answer_delta)

    def resume(
        self,
        on_thinking: Optional[Callable[[str], None]] = None,
        on_answer_delta: Optional[Callable[[str], None]] = None,
    ) -> str:
        """续上最近一个开放 Round（\\继续 命令）：不注入任何新的用户消息。

        步数超限 / Ctrl+C 中断后，轮保持开放但没有新信息——再发一句
        "继续"只会往历史里塞一条噪音用户消息。本方法重建协议消息后
        直接进工作循环，装配与轮内上下文原样继续。没有开放轮时报错。
        """
        last = self.rounds[-1] if self.rounds else None
        if last is None or last.get("end_state") not in ("", "open"):
            raise RuntimeError("没有可继续的开放轮次")
        self.current_round = last
        # 协议消息从事件的 Full 中重建（它们就是事实来源）
        self.messages = [e["message"] for e in last["events"]]
        self.turn_count = last["seq"]
        return self._work_loop(on_thinking, on_answer_delta)

    def _work_loop(
        self,
        on_thinking: Optional[Callable[[str], None]] = None,
        on_answer_delta: Optional[Callable[[str], None]] = None,
    ) -> str:
        """单轮的工具调用主循环：run 与 resume 共用。"""
        self.last_stats = self._fresh_stats()
        # 空响应护栏：流被端点/代理掐断时只有思考没有正文，连续空响应计数
        empty_streak = 0
        # 回调暂存：跨 agent 通信工具（consult）流式展示时借用同一管线，
        # 让子 agent 的输出直接进用户窗口（不打回主 agent 再路由）
        self._stream_cbs = {"thinking": on_thinking, "answer": on_answer_delta}

        # 步数按轮累计（2026-09-09 用户拍板：同一轮被打断后 \c 续跑要
        # 续上——预算属于轮而不属于段；记在 round 上随持久化，进程重启
        # 后也续。里程碑轮开新轮 = 新预算）
        steps_used = (self.current_round or {}).get("steps_used", 0)
        self.last_stats["llm_calls"] = steps_used  # 展示口径同步续上
        while steps_used < self.max_turns:
            steps_used += 1
            if self.current_round is not None:
                self.current_round["steps_used"] = steps_used
            if self.on_progress:
                self.on_progress("等待模型响应…")
            messages = self._assemble_messages()
            try:
                content, ordered, _usage = self._stream_call(
                    messages,
                    tools=self._schemas or None,
                    purpose="working",
                    on_thinking=on_thinking,
                    on_answer_delta=on_answer_delta,
                    on_progress=self.on_progress,
                )
            except LLMStreamError as error:
                # 服务端流中途报错：与"流被掐断"同一失败家族——都是没有
                # 正文的异常终止，并入空响应护栏自动重试。真实错误文本
                # （含 request id）落 history，finish_reason 记 stream_error
                if self.task is not None:
                    self.task.record("empty_stream", f"stream_error: {error}")
                content, ordered, _usage = "", [], None

            if ordered:
                empty_streak = 0
                self._record_event(
                    "tool_call",
                    {
                        "role": "assistant",
                        "content": content,
                        "tool_calls": [
                            {
                                "id": tc["id"],
                                "type": "function",
                                "function": {
                                    "name": tc["name"],
                                    "arguments": tc["arguments"] or "{}",
                                },
                            }
                            for tc in ordered
                        ],
                    },
                )
                self.last_stats["tool_calls"] += len(ordered)
                self._run_tool_batch(ordered)
                continue

            # 模型不再请求工具 → 产出最终回答 → Round 闭合。
            answer = content
            if not answer.strip():
                # 流被端点/代理中途掐断：思考流完了，正文与 usage 都没到
                # （2026-09-08 实测：活跃思考流 13 分钟后被干净掐断，空串
                # 曾被当成 final_answer 闭合轮次）。空串不是最终回答。
                # length = 输出上限（重试必再撞，直接上报）；其余按瞬时
                # 断流自动重试，仍空则轮保持开放交回调用方。
                empty_streak += 1
                fr = self._last_finish_reason or "未返回"
                if self.task is not None:
                    self.task.record("empty_stream", f"finish_reason={fr}")
                # 中断通知记为持久轮事件（runtime-reminder 信封，每轮只记
                # 一次）：重试与 \c 续跑时模型都知道此前响应失败过，重试
                # 不再是盲目的从头再想——思考无法回传（协议 400），能给的
                # 只有 steering：压缩思考、直接迈第一小步
                if empty_streak == 1:
                    self._record_event(
                        "runtime_note",
                        _runtime_reminder(
                            "上一次响应在生成中途被异常终止，未产出任何正文"
                            "（长思考流被服务端掐断）。请大幅压缩思考，不要"
                            "重做完整规划，直接迈出第一小步（一次工具调用），"
                            "分多步边做边验证。"
                        ),
                    )
                if self._last_finish_reason == "length" or empty_streak > 2:
                    self._persist_rounds()
                    raise RuntimeError(
                        f"流式响应异常结束（finish_reason={fr}，正文为空）。"
                        f"Round 保持开放，\\c 可直接接着干"
                    )
                if self.on_progress:
                    self.on_progress("响应为空（流被中断），自动重试…")
                continue
            self._record_event("final_answer", {"role": "assistant", "content": answer})
            if self.task is not None:
                self.task.record("final_answer", answer)
            self.close_round()
            if self.task is not None:
                self._usage_record_and_drain(closed=True)
                self._persist_rounds()
            return answer

        # 步数超限：Round 保持开放（失败尝试并入本轮，不产生整理成本），
        # 由调用方决定后续（重试/人工介入）。
        self._persist_rounds()
        raise RuntimeError(
            f"本轮已累计工作 {steps_used} 步（达到上限 {self.max_turns}）仍未给出"
            f"最终回答（Round 保持开放，\\c 续跑会接着这个步数计数）"
        )

    def label_blocks(self, rounds: Optional[list[dict]] = None) -> dict:
        """机制二（上下文分化运行时）：低频批量语义标注——零截断的块摘要
        输入 + 一次 LLM 调用，产出每块路由式摘要与大类归类。

        与水位整理的关系：机制二发生在整理时（同一口锅），本方法是其
        可独立试跑的形态（scripts/label_blocks.py 离线检查用）。
        返回 {"categories": [...], "labels": {block_id: {...}}}，不落盘。
        """
        rounds = rounds if rounds is not None else self.rounds
        digests = []
        for r in rounds:
            for b in r.get("blocks") or []:
                digests.append(blocks_module.block_digest(r, b))
        if not digests:
            return {"categories": [], "labels": {}}
        prompt = (
            "你是工作块标注器。以下是一个长会话里全部工作块的摘要（按时间序）。\n"
            "职责（最后只输出一个 JSON 对象，不要代码块围栏）：\n"
            '1. "categories"：把本质同域的块归成少数几个大类（通常 3-8 个，'
            '如"页面搭建/布局主题/多会话功能/测试建设"），每个为 '
            '{"id": "A", "name": "大类名", "description": "一句话范围说明"}；\n'
            '2. "blocks"：每个块一条 {"id": "块id", "category": "大类id", '
            '"summary": "路由式一句话"}——summary 写明动作与对象'
            "（如 write_file → css/style.css（14KB 初版）、edit_file → "
            "js/app.js:448（+720B）、run_command → node tests/run.js（5 套件通过）），"
            "不要复制内容。块id 必须原样使用输入里给出的 id。\n"
            "分类只分大类，宁少勿多；相邻块同域是常态。\n\n"
            + "\n\n".join(digests)
        )
        state: Optional[dict] = None
        for _ in range(2):  # 解析失败重试一次
            content, _ordered, _usage = self._stream_call(
                [{"role": "user", "content": prompt}],
                tools=None,
                purpose="organization",
                extra_body={"thinking": {"type": "disabled"}},
            )
            state = self._parse_state_json(content)
            if isinstance(state, dict) and state.get("blocks"):
                break
        if not isinstance(state, dict):
            return {"categories": [], "labels": {}}
        labels = {
            item["id"]: item
            for item in state.get("blocks") or []
            if isinstance(item, dict) and item.get("id")
        }
        categories = [c for c in state.get("categories") or [] if isinstance(c, dict)]
        return {"categories": categories, "labels": labels}

    def _invoke_tool(self, name: str, arguments: str) -> str:
        """解析参数并执行工具，返回结果文本（不含展示与落盘）。"""
        try:
            parsed = json.loads(arguments or "{}")
        except json.JSONDecodeError as error:
            return f"工具参数不是合法 JSON: {error}"
        # 模型偶发把 emoji 拆成不成对 \uD83D 转义：解析合法但无法
        # 编码落盘——进工具与进历史前一律清洗（实测崩溃教训）
        parsed = _sanitize_json_strings(parsed)
        fn = self.tools.get(name)
        if fn is None:
            return f"未知工具: {name}，可用工具: {list(self.tools)}"
        # 用户钩子（zcode-borrowings.md 1.4）：前置可拦截（理由回传模型），
        # 后置可附反馈——扩展者的规则与观测不进 Wovra 代码
        blocked = tools_module.run_pre_hook(name, parsed)
        if blocked:
            return blocked
        try:
            result = fn(**parsed)
        except Exception as error:  # noqa: BLE001——错误回传给模型而不是中断循环
            return f"工具执行出错: {error!r}"
        if not isinstance(result, str):
            result = json.dumps(result, ensure_ascii=False, default=str)
        feedback = tools_module.run_post_hook(name, parsed, result)
        if feedback:
            result = f"{result}\n[hooks 反馈] {feedback}"
        return sanitize_surrogates(result)

    def _execute(self, call_id: str, name: str, arguments: str) -> None:
        """执行单个工具调用，并把结果作为 tool 消息追加到当前 Round。"""
        if self.on_tool_call:
            self.on_tool_call(name, arguments)
        self._finish_tool_result(
            call_id, name, arguments, self._invoke_tool(name, arguments)
        )

    def _finish_tool_result(self, call_id: str, name: str,
                            arguments: str, result: str) -> None:
        if self.on_tool_result:
            self.on_tool_result(name, result)

        event = self._record_event(
            "tool_result", {"role": "tool", "tool_call_id": call_id, "content": result},
            tool_name=name,
        )
        result_for_context = event["message"]["content"] if event.get("id") else result

        if self.task is not None:
            self.task.record("tool_call", f"{name}({arguments})")
            self.task.record("tool_result", f"{name} -> {result_for_context[:500]}")
            self._persist_rounds()

    def _run_tool_batch(self, ordered: list[dict]) -> None:
        """执行一批工具调用。

        纯只读批次（互不依赖）并发执行、按序记录——独立读取串行只是
        白等；含变更类调用时保持顺序执行（写与写之间存在顺序依赖，
        并行写同一文件是竞态）。"""
        if len(ordered) > 1 and all(tc["name"] in _READ_ONLY_TOOLS for tc in ordered):
            if self.on_tool_call:
                for tc in ordered:
                    self.on_tool_call(tc["name"], tc["arguments"])
            with ThreadPoolExecutor(max_workers=min(4, len(ordered))) as pool:
                results = list(pool.map(
                    lambda tc: self._invoke_tool(tc["name"], tc["arguments"]), ordered))
            for tc, result in zip(ordered, results):
                self._finish_tool_result(tc["id"], tc["name"], tc["arguments"], result)
            return
        for tc in ordered:
            self._execute(tc["id"], tc["name"], tc["arguments"])

    # ---- 流式调用（所有 LLM 交互的唯一通道，按用途分账） ------------------------

    def _stream_call(
        self,
        messages: list[dict],
        tools: Optional[list[dict]] = None,
        purpose: str = "working",
        extra_body: Optional[dict] = None,
        on_thinking: Optional[Callable[[str], None]] = None,
        on_answer_delta: Optional[Callable[[str], None]] = None,
        on_progress: Optional[Callable[[str], None]] = None,
    ) -> tuple[str, list[dict], Any]:
        """发一次流式补全，聚合分片，返回 (内容, 工具调用列表, usage)。

        purpose ∈ working / organization / compaction：成本按用途分账，
        实验才能回答"管理机制自身贵不贵"。
        """
        # "步数"只统计干活的步（agent 循环）；整理/压缩是维护开销，
        # 在用途分账与"整理 X tok"里单独体现，不混进步数
        if purpose == "working":
            self.last_stats["llm_calls"] += 1
        start = time.monotonic()
        try:
            stream = self.llm.chat(messages, tools=tools, stream=True, extra_body=extra_body)
        except Exception:
            if not extra_body:
                raise
            # 部分端点不支持 extra_body 里的参数（如关闭思考的 thinking 开关），
            # 降级为不带该参数重发——宁可让整理调用多思考，也不能直接失败。
            # 提示每次会话只发一次，避免每轮刷屏
            if not self._degrade_warned:
                self._degrade_warned = True
                self._emit_status(
                    "后台整理：当前模型不支持整理用的参数，已自动降级重试（本次会话仅提示一次）"
                )
            stream = self.llm.chat(messages, tools=tools, stream=True)
        content_parts: list[str] = []
        tool_calls_acc: dict[int, dict] = {}
        usage = None
        first_token_at: Optional[float] = None
        finish_reason: Optional[str] = None
        chunk_iter = iter(stream)
        while True:
            try:
                chunk = next(chunk_iter)
            except StopIteration:
                break
            except _llm_APIError as error:
                # 服务端在流中途报错（实测：internal error 打断思考流，
                # openai.APIError 不是 RuntimeError，穿透 CLI 直接打崩进程）。
                # 只捕获迭代器自己抛的错——回调/清洗里的 bug 不能被伪装成
                # 流错误触发重试。转 LLMStreamError：主循环并入空响应护栏，
                # 重试耗尽后 CLI 按"轮保持开放"收尾。
                self._last_finish_reason = "stream_error"
                raise LLMStreamError(str(error)) from error
            if getattr(chunk, "usage", None):
                usage = chunk.usage
            if not getattr(chunk, "choices", None):
                continue
            choice = chunk.choices[0]
            if getattr(choice, "finish_reason", None):
                finish_reason = choice.finish_reason
            delta = choice.delta
            if delta is None:
                continue

            thinking = reasoning_of(delta)
            if thinking:
                thinking = sanitize_surrogates(thinking)
                if on_thinking:
                    on_thinking(thinking)

            if delta.content:
                text = sanitize_surrogates(delta.content)
                content_parts.append(text)
                if on_answer_delta:
                    on_answer_delta(text)

            for fragment in delta.tool_calls or []:
                index = fragment.index or 0
                if index not in tool_calls_acc:
                    tool_calls_acc[index] = {"id": "", "name": "", "arguments": ""}
                acc = tool_calls_acc[index]
                if fragment.id:
                    acc["id"] = fragment.id
                if fragment.function and fragment.function.name:
                    acc["name"] = fragment.function.name
                    # 名字一分片到达就提示"正在<动作>…"——用户要的是
                    # 等待时刻的即时反馈，而不是等参数全部输完
                    if on_progress:
                        on_progress(f"正在{_action_word(acc['name'])}…")
                if fragment.function and fragment.function.arguments:
                    acc["arguments"] += fragment.function.arguments

            if first_token_at is None and (
                thinking or delta.content or (delta.tool_calls or [])
            ):
                # 首 token 延迟（TTFT）：prefill + 排队时间，基座的第二张
                # 面孔（延迟乘数）靠它测量——与总耗时分开记
                first_token_at = time.monotonic()

        elapsed = time.monotonic() - start
        ttft = (first_token_at - start) if first_token_at is not None else elapsed
        self._last_finish_reason = finish_reason
        if usage is not None:
            self._accumulate_usage(usage, purpose)
            # 逐调用用量落账（2026-09-09 缓存法医的产物）：usage 行按轮
            # 聚合，逐调用粒度缺失正是 D/E 归因要做事件重建的原因——
            # 从今往后每次调用自带 prompt/cached/miss/ttft 对账数据，
            # provider 上报的可信度可直接用 TTFT 交叉验证
            if self.task is not None:
                cached, _miss = cached_tokens_of(usage)
                self.task.record(
                    "llm_call",
                    f"[{purpose}] prompt={usage.prompt_tokens or 0:,} "
                    f"cached={cached:,} miss={(usage.prompt_tokens or 0) - cached:,} "
                    f"completion={usage.completion_tokens or 0:,} "
                    f"ttft={ttft:.1f}s dur={elapsed:.1f}s finish={self._last_finish_reason or '未返回'}",
                )
        if purpose in _MAINTENANCE_PURPOSES:
            # 维护性开销异步执行、可能跨越轮次边界，混进 last_stats 会
            # 漏记（会话结束丢失）或错记进下一轮（实测教训）
            with self._maint_lock:
                self._maint_usage[purpose]["seconds"] += elapsed
        else:
            self.last_stats["seconds"] += elapsed
            self.last_stats["ttft_seconds"] += ttft
            self.last_stats["ttft_max"] = max(self.last_stats["ttft_max"], ttft)
            self.last_stats["purpose"].setdefault(
                purpose, {"prompt": 0, "completion": 0, "total": 0, "seconds": 0.0}
            )["seconds"] += elapsed
        ordered = [tool_calls_acc[i] for i in sorted(tool_calls_acc)]
        return "".join(content_parts), ordered, usage

    def drain_maintenance_usage(self) -> dict:
        """取走并清零维护账本（整理/压缩的累计用量），线程安全。

        快照交给调用方记账；同时存入 last_maint 供展示层（usage_line）。
        """
        with self._maint_lock:
            snapshot = {k: dict(v) for k, v in self._maint_usage.items()}
            for v in self._maint_usage.values():
                for key in v:
                    v[key] = 0
        self.last_maint = snapshot
        return snapshot

    def _usage_record_and_drain(self, closed: bool) -> None:
        """把本轮成本写入任务历史并清空维护账本。

        开放轮（超限/中断）同样记账——失败尝试的成本恰恰是上下文
        管理最该优化的对象，不能因为轮没闭合就在账本上隐身。
        """
        if self.task is None:
            return
        if self.context_mode == MODE_BASELINE:
            self._baseline_accounting()
        maint = self.drain_maintenance_usage()
        stats = self.last_stats
        prompt = stats["prompt_tokens"]
        cached = stats["cached_tokens"]
        miss = stats["cache_miss_tokens"]
        cache_info = ""
        if prompt:
            cache_info = (
                f" 缓存命中 {cached:,} tok（{cached / prompt:.1%}）"
                f" 未命中 {miss:,} tok（{miss / prompt:.1%}）"
                f" 等效输入 {miss + cached / tokens.CACHE_RATE:,.0f} tok"
            )
        suffix = "" if closed else "（轮未闭合：超限/中断，成本照记）"
        ttft_info = ""
        if stats.get("ttft_max"):
            ttft_info = f" ttft={stats['ttft_seconds']:.1f}s（峰值 {stats['ttft_max']:.1f}s）"
        self.task.record(
            "usage",
            f"[{self.context_mode}] steps={stats['llm_calls']:,} "
            f"context={self.last_context_estimate:,} "
            f"working={stats['purpose']['working']['total']:,} "
            f"org={maint['organization']['total']:,} "
            f"compaction={maint['compaction']['total']:,} "
            f"prompt={prompt:,} completion={stats['completion_tokens']:,} "
            f"total={stats['total_tokens']:,}（思考 {stats['reasoning_tokens']:,}）"
            f"{cache_info}{ttft_info}{suffix}",
        )

    def _accumulate_usage(self, usage, purpose: str) -> None:
        """把一次调用的 usage 记进账本。

        working 记入本轮 last_stats（最终回答即记账）；整理/压缩是
        维护性开销且可能异步跨越轮次边界，记入独立维护账本，由
        _usage_record_and_drain 在记账时统一取走，不与干活的成本混账。
        """
        details = getattr(usage, "completion_tokens_details", None)
        reasoning_tokens = getattr(details, "reasoning_tokens", None)
        cached, _miss = cached_tokens_of(usage)

        if purpose in _MAINTENANCE_PURPOSES:
            with self._maint_lock:
                bucket = self._maint_usage[purpose]
                bucket["prompt"] += usage.prompt_tokens or 0
                bucket["completion"] += usage.completion_tokens or 0
                bucket["total"] += usage.total_tokens or 0
            return

        self.last_stats["prompt_tokens"] += usage.prompt_tokens or 0
        self.last_stats["completion_tokens"] += usage.completion_tokens or 0
        self.last_stats["total_tokens"] += usage.total_tokens or 0
        bucket = self.last_stats["purpose"].setdefault(
            purpose, {"prompt": 0, "completion": 0, "total": 0, "seconds": 0.0}
        )
        bucket["prompt"] += usage.prompt_tokens or 0
        bucket["completion"] += usage.completion_tokens or 0
        bucket["total"] += usage.total_tokens or 0

        if reasoning_tokens:
            self.last_stats["reasoning_tokens"] += reasoning_tokens
        self.last_stats["cached_tokens"] += cached
        self.last_stats["cache_miss_tokens"] += max(
            0, (usage.prompt_tokens or 0) - cached
        )

    # ---- Context Assembly（设计文档第 4 节） ------------------------------------

    def _assemble_messages(self) -> list[dict]:
        """装配本轮请求的上下文，并记录体量估算（终端展示与窗口保底同口径）。"""
        msgs = self._assemble_messages_impl()
        self.last_context_estimate = self._estimate_messages(msgs)
        return msgs

    def _assemble_messages_impl(self) -> list[dict]:
        """按变化频率排序装配上下文（缓存友好布局）。

        [1] system 人设（静态）
        [2] 历史轮次视图（少变：闭合时成形，之后不可变）
        [3] Task State + 降档轮次的一行索引 + 文件地图（每轮变——放尾部）
        [4] 当前 Round 事件（追加式全量，轮内赦免）
        """
        past = [r for r in self.rounds if r is not self.current_round]

        if self.context_mode == MODE_BASELINE:
            msgs: list[dict] = []
            persona = self.system_prompt
            if self.task is not None and self.task.baseline_summary:
                # 阈值压缩产生的历史摘要（市面惯例：摘要 + 最近几轮原文）
                persona = (persona + "\n\n[历史压缩摘要]\n" + self.task.baseline_summary).strip()
            if persona:
                msgs.append({"role": "system", "content": persona})
            for r in past:
                if r.get("compacted"):
                    continue  # 已并入压缩摘要
                msgs.extend(e["message"] for e in r["events"])
            msgs.extend(self._current_round_messages())
            return msgs

        # ---- managed：未整理全量，已整理紧凑视图 --------------------
        # 前缀纪律（2026-09-07 用户拍板）：**只有整理生效才破坏前缀**。
        # 未整理轮次原文全量在上下文里；已整理轮次渲染为紧凑视图——
        # 视图替换只发生在 promotion（新轮开启）那一刻，除此之外装配
        # 严格追加。V2 三档滑窗废除：它每轮在装配中部改写历史，
        # 实测把命中率从 83.7% 砸到 49.9%（R7→R8）。
        state_render = ""
        if self.task is not None:
            state_render = self.task.get_state().render()

        msgs: list[dict] = []
        if self.system_prompt:
            msgs.append({"role": "system", "content": self.system_prompt})

        view_msgs: list[dict] = []
        organized_rounds: list[dict] = []
        # 折叠判定（2026-09-10 用户拍板）：视图只保留最近 3 批整理
        # （org_generation 最大的 3 代）；更早的轮按文件当前状态折叠——
        # ledger 现场重算（含当前轮），零 LLM、状态不过期
        ledger = lifecycle_module.FileLedger()
        for rr in past:
            ledger.update(rr, blocks=blocks_module.segment_round_by_file(rr))
        if self.current_round is not None:
            ledger.update(
                self.current_round,
                blocks=blocks_module.segment_round_by_file(self.current_round),
            )
        done_gens = sorted(
            {r.get("org_generation", 1) for r in past
             if r.get("org_state") == "done"}
        )
        keep_min = done_gens[-1] - 2 if done_gens else 0
        for r in past:
            if r.get("org_state") == "done":
                # 已整理：紧凑视图（原文+意图+精修索引；细节可 expand_history 取回）
                organized_rounds.append(r)
                if r.get("org_generation", 1) >= keep_min:
                    compact = self._render_compact(r)
                else:
                    compact = self._render_collapsed(r, ledger)
                if compact is None:
                    continue  # 合并组非组首轮：由组首轮合并显示
                view_msgs.append({"role": "user", "content": r["user_input"]["original"]})
                view_msgs.append({"role": "assistant", "content": compact})
            else:
                # 未整理：原文全量——分辨率损失只允许来自整理，不来自装配
                view_msgs.extend(e["message"] for e in r["events"])

        block = []
        if state_render:
            block.append(state_render)
        file_map = self._file_map_lines(organized_rounds)
        if file_map:
            block.append(
                "[历史涉及文件]（这些轮次的原文已整理收纳，修改前先 read_file "
                "获取现状，通读时按 num_lines=400 连续分段）"
            )
            block += file_map
        todo_lines = self._todo_tail_lines()
        if todo_lines:
            block = todo_lines + block

        msgs.extend(view_msgs)
        msgs.extend(self._current_round_messages())
        # 信封绝对尾部（2026-09-08 用户拍板，D 组实证）：todo/TaskState/
        # 文件地图是高频变化状态，放在当前轮事件之前时每次变化都作废其
        # 后全部前缀——D 段1 26 次 todo 变化把命中率砸到 77.5%（段2/3
        # 零变化 99.2-99.5%）。放尾部后状态变化只作废信封本身（~1-2K），
        # 事件追加与状态变化互不破坏；跨轮边界时前缀还能保留到整段历史
        # 末尾。位置仍在响应之前，信息与时机不变，纯缓存布局修正。
        if block:
            # 运行时专属通道（zcode-borrowings.md 1.1）：机制信息用
            # <runtime-reminder> 信封注入，与用户发言语义分离——模型
            # 分得清"用户要的"和"机制给的"（系统提示词里声明该约定）
            msgs.append(_runtime_reminder("\n\n".join(block)))
        return msgs

    def _todo_tail_lines(self) -> list[str]:
        """当前大步/小步进度的尾部展示（跨轮续跑的工作记忆；空则不占位）。"""
        todo = (self.task.todo or {}) if self.task is not None else {}
        milestone = todo.get("milestone")
        if not milestone:
            return []
        steps = todo.get("steps") or []
        done_n = sum(1 for s in steps if s["done"])
        lines = [f"[当前大步] {milestone['goal']}（小步 {done_n}/{len(steps)}）"]
        if not milestone.get("planned"):
            # 结构闸门是拒绝式反馈，但模型看不到"还没拆"这件事——尾部
            # 用一行提醒把它变成可操作项，避免 verify 时才发现被拒
            lines.append("[待办] 本大步尚未拆小步——先 add_step 拆出阶段内工作项")
        deferred = milestone.get("deferred") or []
        if deferred:
            lines.append(f"[挂起人工验收] {len(deferred)} 项，大步收尾一次性呈交")
        return lines

    def _milestone_map_lines(self, rounds: list[dict]) -> list[str]:
        """轮 ↔ 大步映射（整理/分裂指令的现状归属辅助）。

        大步 = 可验收单元（todo.history 已验收 + 在飞 milestone）。同一
        大步的轮通常同属一个功能域，映射帮整理/分裂分析判断现状归属；
        历史条目缺 started_seq（迁移前的旧数据）时按"上一个大步闭合轮
        +1"推断，保证区间无重叠。无大步记录返回空列表。
        """
        todo = (self.task.todo or {}) if self.task is not None else {}
        spans: list[tuple[int, Optional[int], str, str]] = []
        prev_end = 0
        for e in todo.get("history") or []:
            end = e.get("closed_seq")
            if end is None:
                continue
            start = e.get("started_seq")
            if not start or start <= prev_end:
                start = prev_end + 1
            spans.append((start, end, str(e.get("goal") or ""), "已验收"))
            prev_end = end
        m = todo.get("milestone")
        if m and m.get("started_seq"):
            start = m["started_seq"]
            if start <= prev_end:
                start = prev_end + 1
            spans.append((start, None, str(m.get("goal") or ""), "进行中"))
        if not spans:
            return []
        lines = [
            "[轮↔大步映射]（大步 = 可验收单元；同一大步的轮通常同属一个功能域，"
            "辅助现状归属判断）"
        ]
        for r in rounds:
            seq = r["seq"]
            tag = "未进入大步"
            for start, end, goal, status in spans:
                if (end is None and seq >= start) or (
                    end is not None and start <= seq <= end
                ):
                    tag = f"大步『{goal[:36]}』（{status}）"
                    break
            lines.append(f"  R{seq} ← {tag}")
        return lines

    def _current_round_messages(self) -> list[dict]:
        """轮内赦免：当前 Round 事件全量进入上下文，不做任何内容截断。

        执行期截断曾两次被实测证明适得其反：折叠诱发"读 → 失忆 →
        重读"死循环（39 次 read_file 烧穿 40 步上限）；2000 字符
        安全截断把模型刚读到的文件内容挡在上下文外。唯一的例外是
        模型窗口本身：估算超过 context_limit 时，把最老的事件折叠
        为索引行直到回线——最后手段，正常任务永远碰不到。
        """
        msgs = list(self.messages)
        if self.current_round is None:
            return msgs
        events = self.current_round["events"]
        if len(events) != len(msgs):
            return msgs  # 结构对不上时不动手（宁超限，不坏数据）
        budget = int(self.context_limit * 0.9)  # 给最终回答留余量
        # 廉价预检：最坏 1 字 ≈ 1 tok（CJK），字符数不超预算必在窗内
        total_chars = sum(len(str(m.get("content") or "")) for m in msgs)
        if total_chars <= budget:
            return msgs
        sizes = [self._estimate_messages([m]) for m in msgs]
        total = sum(sizes)
        if total <= budget:
            return msgs
        # 每条索引行按 150 tok 保守计价（120 字符 CJK 的上界），宁多折不少折
        fold, kept = 0, total
        while fold < len(events) - 1 and kept + fold * 150 + 200 > budget:
            kept -= sizes[fold]
            fold += 1
        lines = [truncate.event_index_line(e) for e in events[:fold]]
        block = {"role": "user", "content": (
            f"[紧急折叠：当前轮上下文估算已超模型窗口（{self.context_limit:,} tok），"
            f"最老 {fold} 条事件折叠为索引；需要细节可用 expand_history 按事件 ID 展开]\n"
            + "\n".join(lines)
        )}
        return [block] + list(msgs[fold:])

    @staticmethod
    def _estimate_messages(msgs: list[dict]) -> int:
        """估算一组协议消息的 token 数（正文 + 工具调用参数）。"""
        total = 0
        for m in msgs:
            total += tokens.estimate(str(m.get("content") or ""))
            for call in m.get("tool_calls") or []:
                total += tokens.estimate(
                    (call.get("function") or {}).get("arguments") or ""
                )
        return total

    def _render_compact(self, r: dict) -> Optional[str]:
        """已整理轮次的紧凑视图：👤用户原文 + 🎯意图 + 📌关键约束 + 逐块细节。

        整理生效后轮次以此形态常驻上下文——它是"水位折叠"的落点，
        细节永不丢失（expand_history 按块 ID/事件 ID 取回原文）。
        2026-09-08 用户拍板：块描述承载完整细节；2026-09-09 修订为信息量
        自适应（低信息量块两三行即可）。轮首保留第一版视图的
        用户意图三行式。无块描述的旧整理轮回退到精修事件索引。
        2026-09-10 合并显示：连续纯聊天轮合并为组——非组首轮返回
        None（调用方跳过，不产生视图），组首轮头部显示 [R1-2] 锚点。
        """
        if r.get("merged_skip"):
            return None
        ui = r["user_input"]
        if r.get("merged_anchor"):
            anchor = r["merged_anchor"]
            lines = [f"[{anchor}]"]
            # 合并组：组内所有轮的用户原文都保留（R1-2 里 R2 的 👤 不能丢）
            seqs = [r["seq"]]
            seqs += [
                rr["seq"] for rr in self.rounds
                if rr.get("merged_skip") == anchor
            ]
            seqs.sort()
            for seq in seqs:
                rr = next(x for x in self.rounds if x["seq"] == seq)
                lines.append(f"👤 用户: \"{rr['user_input']['original']}\"")
        else:
            lines = [f"[R{r['seq']}]"]
            lines.append(f"👤 用户: \"{ui['original']}\"")
        if ui.get("normalized"):
            lines.append(f"🎯 意图: {ui['normalized']}")
        if ui.get("key_constraints"):
            lines.append(f"📌 关键约束: {ui['key_constraints']}")
        summaries = r.get("block_summaries") or {}
        if summaries:
            lines.append("块细节：")
            blocks = r.get("blocks") or []
            if blocks:
                for b in blocks:
                    s = summaries.get(b["id"])
                    if s:
                        lines.append(f"▸ {b['id']}: {s}")
            else:
                for bid in sorted(summaries, key=lambda x: int(x.rsplit("-B", 1)[-1])):
                    lines.append(f"▸ {bid}: {summaries[bid]}")
        else:
            idx = self._round_index_lines(r)
            if idx:
                lines.append("事件索引：")
                lines += idx
        return "\n".join(lines)

    def _render_collapsed(self, r: dict, ledger) -> Optional[str]:
        """早期轮折叠视图（2026-09-10 用户拍板：视图只保留最近 3 批整理）。

        折叠行 = 👤 原文 + 🎯 意图 + 当前仍 LIVE 的文件的块摘要；其余块
        折叠成一行说明（防止模型误以为该轮只有这些块）。文件状态用装配
        时的实时 ledger（整理时的状态会过期，新会话文件可能重建/删除）。
        两级展开：expand_history 轮号 → 视图（summary 档）→ 原文（full 档）。
        """
        if r.get("merged_skip"):
            return None
        lines = [f"[R{r['seq']}]（折叠）"]
        ui = r["user_input"]
        lines.append(f"👤 用户: \"{ui['original']}\"")
        if ui.get("normalized"):
            lines.append(f"🎯 意图: {ui['normalized']}")
        summaries = r.get("block_summaries") or {}
        # 现场重算 v3 块（不信持久化 v2：旧轮 blocks 是 kind=work，无 file）
        v3_by_id = {b["id"]: b for b in blocks_module.segment_round_by_file(r)}
        kept = []
        for bid, s in summaries.items():
            b = v3_by_id.get(bid)
            if b is None or b.get("kind") != "file":
                continue
            if ledger.state_of(b.get("file", "")) == "live":
                kept.append(f"▸ {bid}: {s}")
        if kept:
            lines.append("（以下块涉及的文件当前仍存活）")
            lines += kept
        folded = len(summaries) - len(kept)
        if folded > 0:
            lines.append(
                f"（其余 {folded} 块已折叠，expand_history 可按轮/块展开）"
            )
        return "\n".join(lines)

    def _round_index_lines(self, r: dict) -> list[str]:
        """事件的索引行：优先用精修索引，未整理的事件用 Runtime 截断行。"""
        refined = r.get("refined_index") or {}
        out = []
        for e in r["events"]:
            line = refined.get(e["id"]) or e["truncated"]
            status = f"[{e['status']}] " if e.get("status") else ""
            out.append(f"[{e['id']}] {status}{line}")
        return out

    def _file_map_lines(self, rounds: list[dict]) -> list[str]:
        """把已整理轮次里出现过的文件整理成一张"文件地图"。

        文件内容随轮次降档后，模型曾经只能盲目分片重爬（实测一个
        轮里 39 次 read_file 重读同一文件）。地图只给"哪些文件、
        在哪些轮被写过/读过"，指引精准定位，不携带内容成本。
        """
        touched: dict[str, dict[str, list[int]]] = {}
        for r in rounds:
            for e in r.get("events", []):
                if e["type"] != "tool_call":
                    continue
                for tc in e["message"].get("tool_calls", []) or []:
                    fn = tc.get("function", {})
                    if fn.get("name") not in ("write_file", "edit_file", "read_file"):
                        continue
                    try:
                        path = json.loads(fn.get("arguments") or "{}").get("path")
                    except ValueError:
                        continue
                    if not path:
                        continue
                    info = touched.setdefault(path, {"w": [], "r": []})
                    if fn["name"] == "read_file":
                        info["r"].append(r["seq"])
                    else:
                        info["w"].append(r["seq"])
        lines = []
        for path, info in touched.items():
            parts = []
            if info["w"]:
                wrote = "R" + ",R".join(dict.fromkeys(map(str, info["w"])))
                parts.append(f"写于 {wrote}")
            if info["r"]:
                read = "R" + ",R".join(dict.fromkeys(map(str, info["r"])))
                parts.append(f"读于 {read}")
            lines.append(f"- {path}（{'；'.join(parts)}）")
        return lines

    @staticmethod
    def _head_text(text: str, limit: int) -> str:
        text = " ".join((text or "").split())
        return text if len(text) <= limit else text[:limit] + "…"

    # ---- History Maintenance Pipeline（水位触发的批量整理） -------------------

    def _unorganized_rounds(self) -> list[dict]:
        """已闭合且尚未整理完成的轮次（按时间正序）。

        org_state ∈ ""（从未整理）/ "pending"（排队或上次崩溃遗留）/
        "failed"（上次解析失败）都算未整理；整理中（inflight）的不算，
        防止批量排队期间被下一次触发重复收编。
        """
        return [
            r for r in self.rounds
            if r.get("end_state") == "completed"
            and r.get("org_state") != "done"
            and r["seq"] not in self._org_inflight
        ]

    def _maybe_organize_batch(self) -> None:
        """水位检查：当前上下文窗口体量达阈值且本轮已闭合时，批量入队整理。

        水位口径 = last_context_estimate（最近一次装配的估算，轮闭合时
        即本轮峰值）——不是未整理积压量（2026-09-07 用户拍板）。每次轮
        闭合到水位则收编**全部**未整理轮（2026-09-09 用户拍板：批次上限
        删除，触发只看水位；最老的先整理）。产物暂存不直写：本轮装配
        保持原样，下一轮开启才生效（_promote_org_results）；过程对用户
        静默。
        """
        if self.last_context_estimate < self._org_watermark:
            return
        # 保护机制：宽限期（3 轮全豁免）+ 冷却间隔（2026-09-09 用户拍板：
        # 双条件不认可，恢复全豁免——提出 3 轮时已考虑 229 步巨轮，巨轮
        # 在宽限期内同样豁免；代价知情：超水位推迟的整理由冷却后的批次
        # 补上）。窗口保底（紧急折叠）不在豁免范围，是独立的生存线。
        current_seq = self.rounds[-1]["seq"] if self.rounds else 0
        if current_seq <= self._org_grace:
            return  # 宽限期：开头几轮是"解释现状的最小历史"，全豁免
        last_maintained = max(
            (
                r["seq"]
                for r in self.rounds
                if r.get("org_state") == "done"
                or (
                    r.get("org_state") == "pending"
                    and r["seq"] in self._org_inflight
                )
            ),
            default=0,
        )
        if last_maintained and current_seq - last_maintained < self._org_cooldown:
            # 冷却口径只认"真正维护过"：done（完成）或本进程在飞（pending
            # 且 seq ∈ _org_inflight）。崩溃遗留的 pending（上个进程维护
            # 线程被中断的半程状态）不算已维护——否则冷却把它们当刚维护
            # 过，挡掉本次会话第一次补整理（F 组实证：6 轮 pending 续跑，
            # R7/R8 闭合被 8-6=2 < 3 连挡两轮，压缩迟迟不开始）。
            return  # 冷却间隔：两次维护之间的最小轮距，防高频
        unorganized = self._unorganized_rounds()
        if not unorganized:
            return
        batch = unorganized
        for r in batch:
            r["org_state"] = "pending"
            self._org_inflight.add(r["seq"])
        if self.async_organization:
            # 快照在入队瞬间取：它就是"活前缀"——维护调用原样追加指令，
            # 生产环境里这次调用的输入端骑满前缀缓存
            self._org_queue.put((batch, self._assemble_messages()))
            self._ensure_worker()
        else:
            # 同步模式（run 命令/测试）：立即整理，结果随轮次落盘
            try:
                self._parallel_maintenance(batch, self._assemble_messages())
            finally:
                for r in batch:
                    self._org_inflight.discard(r["seq"])

    def organize_backlog(self) -> None:
        """立即整理全部未整理轮（同步）——run 模式进程收尾用。

        chat 模式不调用：水位设计允许小会话全程不整理（成本归零）；
        run 是一次性任务单元，退出前补整理，TaskState 才能跟得上
        下一次自主推进（"根据任务状态决定下一步"依赖这本账）。
        """
        if self.context_mode != MODE_MANAGED or self.task is None:
            return
        while True:
            unorganized = self._unorganized_rounds()
            if not unorganized:
                return
            batch = unorganized
            for r in batch:
                r["org_state"] = "pending"
                self._org_inflight.add(r["seq"])
            try:
                org_ok, _split_ok = self._parallel_maintenance(
                    batch, self._assemble_messages()
                )
            except Exception:  # noqa: BLE001——收尾整理失败不阻塞任务退出
                for r in batch:
                    r["org_state"] = "failed"
                self._persist_rounds()
                return
            finally:
                for r in batch:
                    self._org_inflight.discard(r["seq"])
            if not org_ok:
                return

    def _ensure_worker(self) -> None:
        if self._org_thread is not None and self._org_thread.is_alive():
            return
        self._org_thread = threading.Thread(
            target=self._org_worker, name="wovra-organization", daemon=True
        )
        self._org_thread.start()

    def _org_worker(self) -> None:
        while True:
            batch, base_messages = self._org_queue.get()
            try:
                self._parallel_maintenance(batch, base_messages)
            except Exception:  # noqa: BLE001——整理失败不影响主对话
                for r in batch:
                    r.pop("pending_org", None)
                    r["org_state"] = "failed"
                self._persist_rounds()
            finally:
                self._org_queue.task_done()
                for r in batch:
                    self._org_inflight.discard(r["seq"])

    def flush_organization(self, timeout: float = 10.0) -> bool:
        """等待异步整理队列清空（chat 退出限时等待；run 用同步模式无需调用）。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._org_queue.unfinished_tasks == 0:
                return True
            time.sleep(0.1)
        return self._org_queue.unfinished_tasks == 0

    def _block_map_lines(
        self, rounds: list[dict]
    ) -> tuple[dict[int, list[dict]], list[str], list[dict]]:
        """分块地图（v3 按文件分块 + 标签行，零 LLM），供整理/分裂指令用。

        现场重算——不信轮上持久化的 blocks（181052 等旧会话存的是
        kind=work 旧结构）；FileLedger 按批内轮序推演，用每轮开始时的
        版本数判定 创建 vs 重构（与 scripts/file_block_structure.py 的
        build 同流程，标签行即分辨率指令）。

        连续纯聊天轮（单保底块且无工具）合并为一组（2026-09-10 用户
        拍板：组内只输出一个结果）：地图显示 R1-2（2 轮·纯聊天合并），
        合成块 ID R1-2-B1；merged_groups 携带组信息，供 _stage_org_state
        落地时广播到组内每轮。
        """
        ledger = lifecycle_module.FileLedger()
        round_blocks: dict[int, list[dict]] = {}
        merged_groups: list[dict] = []
        map_lines: list[str] = []
        i = 0
        n = len(rounds)
        while i < n:
            r = rounds[i]
            blocks = blocks_module.segment_round_by_file(r)
            round_blocks[r["seq"]] = blocks
            versions_before = {
                p: len(e["versions"]) for p, e in ledger.entries().items()
            }
            is_chat = (
                len(blocks) == 1 and blocks[0]["kind"] == "fallback"
                and not blocks_module.round_has_tool_calls(r)
            )
            if not is_chat:
                if blocks:
                    orig = (r.get("user_input") or {}).get("original") or ""
                    sub = [
                        f"R{r['seq']}（{len(blocks)} 块）· 👤 {_clip_quote(orig)}"
                    ]
                    round_tools = blocks_module.round_has_tool_calls(r)
                    for b in blocks:
                        if b["kind"] == "file":
                            state = blocks_module.state_label(
                                blocks_module.block_end_state(
                                    ledger.state_of(b["file"]), b
                                )
                            )
                        else:
                            state = ""
                        label = blocks_module.label_line(
                            b, versions_before, state, round_tools
                        )
                        extra = ""
                        if b["kind"] == "user":
                            # 用户块：附补充输入原话锚点（轮头之后的用户事件）
                            bevs = set(b.get("events") or [])
                            texts = [
                                e["message"].get("content") or ""
                                for e in r["events"]
                                if e.get("type") == "user" and e.get("id") in bevs
                            ]
                            if texts:
                                extra = f" · 👤 {_clip_quote('；'.join(texts))}"
                        sub.append(
                            f"  {b['id']} {label}{extra}"
                            f"（{b['start_event']}~{b['end_event']}）"
                        )
                    map_lines.append("\n".join(sub))
                else:
                    map_lines.append(
                        f"R{r['seq']}：无分块结构（改用 refined_index 事件摘要）"
                    )
                ledger.update(r, blocks=blocks)
                i += 1
                continue
            # 连续纯聊天轮 → 合并组（组内只输出一个结果）
            group = [r]
            j = i + 1
            while j < n:
                rj = rounds[j]
                bj = blocks_module.segment_round_by_file(rj)
                if (
                    len(bj) == 1 and bj[0]["kind"] == "fallback"
                    and not blocks_module.round_has_tool_calls(rj)
                ):
                    group.append(rj)
                    j += 1
                else:
                    break
            first_seq, last_seq = group[0]["seq"], group[-1]["seq"]
            anchor = (
                f"R{first_seq}" if first_seq == last_seq
                else f"R{first_seq}-{last_seq}"
            )
            bid = f"{anchor}-B1"
            first_ev = group[0]["events"][0].get("id") or ""
            last_ev = group[-1]["events"][-1].get("id") or ""
            merged_groups.append({
                "bid": bid,
                "anchor": anchor,
                "seqs": [g["seq"] for g in group],
                "first_seq": first_seq,
            })
            sub = [f"{anchor}（{len(group)} 轮·纯聊天合并）"]
            for g in group:
                go = (g.get("user_input") or {}).get("original") or ""
                sub.append(f"  👤 {_clip_quote(go)}")
            sub.append(f"  {bid} 【保底块】：（{first_ev}~{last_ev}）")
            map_lines.append("\n".join(sub))
            for g in group:
                round_blocks[g["seq"]] = [{
                    "id": bid,
                    "kind": "fallback",
                    "start_event": first_ev,
                    "end_event": last_ev,
                }]
                ledger.update(
                    g, blocks=blocks_module.segment_round_by_file(g)
                )
            i = j
        return round_blocks, map_lines, merged_groups

    def _organize_rounds(
        self, rounds: list[dict], base_messages: Optional[list[dict]] = None
    ) -> tuple:
        """批量整理已闭合的 Round 们（维护管线的工作单元，V3 §4 机制）。

        2026-09-08 用户拍板：**整理 = 一次追加式对话**。输入 = 触发时刻
        的装配原文快照（base_messages 一字不动——纯追加才骑得住前缀缓存，
        任何内联改写都会把缓存从插入点打断）+ 尾部追加"分块地图 + 整理
        指令"。全部工具禁用、单次生成：read_full 退役（原文本来就在眼前，
        截断索引再造一遍反而丢了细节还多付一遍生成）。
        输出 = 各轮 Normalized 意图 + 关键约束 + 逐块信息量自适应描述 + 合并
        State Patch，经常驻工具 submit_organization 提交（与工作对话同一
        tools 数组——序列化恒定，前缀缓存常骑；字段语义以 schema 描述为
        唯一事实源），**全部写入 pending_org 暂存区**：本轮装配必须纹丝
        不动（连贯性 + 缓存前缀稳定），下一轮开启时由 _promote_org_results
        生效。产物对应不上批次轮（seq 缺失等畸形，GLM 实测会整字段省略）
        时整批 org_state=failed 回入水位，原始层永远不受影响——**不重试**
        （2026-09-10 用户拍板：重试只是再付一遍完整生成，不能确定解决
        失败）。返回 (是否成功, 交换记录或 None)——交换记录供分裂阶段
        纯追加（(messages, content, ordered)）。
        """
        round_blocks, map_lines, merged_groups = self._block_map_lines(rounds)

        seq_list = "、R".join(str(r["seq"]) for r in rounds)
        ms_lines = self._milestone_map_lines(rounds)
        instruction = (
            "[整理指令]\n"
            "以上是本会话的完整上下文。请把其中这些轮次整理成结构化档案："
            f"R{seq_list}。其余轮次不要输出。\n\n"
            + _ORG_META_INFO.format(seq_list=seq_list)
            + "\n"
            "[分块地图]（块由 Runtime 按工作对象确定性划分并打了标签；"
            "标签 = 写多细、写什么的指令，按标签执行，不需要判断哪些"
            "内容重要）\n"
            + "\n".join(map_lines)
            + "\n\n"
            + ("\n".join(ms_lines) + "\n\n" if ms_lines else "")
            + _ORG_TAG_INSTRUCTIONS
            + "\n完成后调用 submit_organization 工具提交结果（唯一出口，不要"
            "在正文中输出 JSON）。字段语义以工具定义为准；块 ID 逐字取自"
            "[分块地图]，每个块一条、一个不落、与地图同序。"
        )
        messages = list(base_messages or self._org_fallback_base(rounds))
        messages.append({"role": "user", "content": instruction})

        # org 工具收窄为只留 submit_organization（2026-09-10）：模型会把
        # 整理指令当普通工作对话乱调工具（实测正文说"先重建 HTML 前端"
        # 然后去调 write_file）——收窄工具集是硬约束，比提示词可靠。
        # 牺牲 org 路的前缀缓存（维护调用低频，可接受）。
        org_tools = [
            s for s in self._schemas
            if s.get("function", {}).get("name") == "submit_organization"
        ]
        content, ordered, _usage = self._stream_call(
            messages, tools=org_tools, purpose="organization",
        )
        state = self._extract_org_state(content, ordered)
        staged = self._stage_org_state(
            state, rounds, round_blocks, merged_groups
        )
        if staged == 0:
            # 无可用产物：保持 Runtime 视图（org_state=failed，回入水位
            # 等下次触发），原始层永远不受影响。2026-09-10 用户拍板：
            # 不重试——重试只是再付一遍完整生成，不能确定解决失败
            # （实测两次重试同因失败：输出预算/模型行为不因重试改变）。
            for r in rounds:
                r.pop("pending_org", None)
                r["org_state"] = "failed"
            self._persist_rounds()
            return False, None
        for r in rounds:
            r["org_state"] = "done"
        # 代次打标：最近 3 批视图保留，更早的按文件状态折叠（2026-09-10）
        self._org_generation += 1
        for r in rounds:
            r["org_generation"] = self._org_generation
        self._persist_rounds()
        # 交换记录（2026-09-10）：split 阶段纯追加这段对话——org 刚跑完
        # KV 全热，分裂白得整理产物（12K+），只付自己的指令 ~1.5K
        exchange = (messages, content or "", ordered or [])
        return True, exchange

    def _stage_org_state(
        self, state: Optional[dict], rounds: list[dict], round_blocks: dict,
        merged_groups: Optional[list[dict]] = None,
    ) -> int:
        """把整理产物写进各轮的 pending_org 暂存区，返回匹配到轮的数量。

        轮匹配三级：item["seq"] 整数 → 字符串数字强转 → 一一对应声明下
        按位置兜底（GLM 实测会整字段省略 seq，2026-09-08 复测发现）。
        全都对应不上由调用方判失败，不静默吞掉。
        """
        if not isinstance(state, dict):
            return 0
        rounds_by_seq = {r["seq"]: r for r in rounds}
        items = [it for it in (state.get("rounds") or []) if isinstance(it, dict)]
        staged = 0
        for idx, item in enumerate(items):
            r = None
            try:
                r = rounds_by_seq.get(int(item.get("seq")))
            except (TypeError, ValueError):
                r = None
            if r is None and len(items) == len(rounds):
                r = rounds[idx]
            if r is None:
                continue
            staged += 1
            # 暂存区：不直写正式字段——本轮对话期间的装配由这些字段
            # 组成，动它们就是"下一步替换"，会破坏连贯性和缓存前缀
            pending = r["pending_org"] = {}
            if item.get("normalized_user_input"):
                pending["normalized"] = str(item["normalized_user_input"])
            if item.get("key_constraints"):
                pending["key_constraints"] = str(item["key_constraints"])
            blocks = round_blocks.get(r["seq"]) or []
            blocks_by_id = {b["id"]: b for b in blocks}
            summaries = {}
            for bs in item.get("block_summaries") or []:
                if (
                    isinstance(bs, dict)
                    and str(bs.get("id") or "") in blocks_by_id
                    and bs.get("summary")
                ):
                    summaries[str(bs["id"])] = str(bs["summary"])
            if blocks_by_id:
                # 完整性兜底：LLM 漏标的块用确定性路由行补齐——视图里
                # 不允许出现没有描述的块（用户拍板：保证完整的细节描述）
                for bid, b in blocks_by_id.items():
                    if bid not in summaries:
                        parts = [f"{b['start_event']}~{b['end_event']}"]
                        if b["kind"] == "file":
                            parts.append(b["file"])
                        elif b.get("wrote_files"):  # 旧 v2 块兼容
                            parts.append("写: " + ", ".join(b["wrote_files"]))
                        if b.get("command_types"):
                            parts.append("命令[" + ", ".join(b["command_types"]) + "]")
                        summaries[bid] = "（LLM 未标注，仅路由）" + " · ".join(parts)
                pending["block_summaries"] = summaries
                if not r.get("blocks"):
                    pending["blocks"] = blocks  # 旧轮现场算出的块结构一并落盘
            valid_ids = {e["id"] for e in r["events"]}
            for line_item in item.get("refined_index") or []:
                if (
                    isinstance(line_item, dict)
                    and line_item.get("id") in valid_ids
                    and line_item.get("line")
                ):
                    pending.setdefault("refined_index", {})[line_item["id"]] = str(
                        line_item["line"]
                    )
        patch = state.get("state_patch")
        if isinstance(patch, dict) and rounds:
            if state.get("is_done") is not None and "is_done" not in patch:
                patch["is_done"] = bool(state["is_done"])
            # 批次级补丁挂在批内第一轮上：生效时只应用一次
            rounds[0].setdefault("pending_org", {})["state_patch"] = patch
        # 合并组广播（2026-09-10 用户拍板）：连续纯聊天轮合并为一个
        # 结果——组内任一轮拿到合成块描述则全组共享；组首轮标记
        # merged_anchor（视图显示 [R1-2] 锚点），非组首轮标记
        # merged_skip（视图跳过，由组首合并显示）
        for mg in merged_groups or []:
            text = ""
            for seq in mg["seqs"]:
                r = rounds_by_seq.get(seq)
                po = (r or {}).get("pending_org") or {}
                s = po.get("block_summaries", {}).get(mg["bid"])
                if s:
                    text = s
                    break
            if not text:
                continue  # 组内无人写（LLM 全跳）：保持各自兜底行
            for seq in mg["seqs"]:
                r = rounds_by_seq.get(seq)
                if r is None:
                    continue
                po = r.setdefault("pending_org", {})
                po["block_summaries"] = {mg["bid"]: text}
                if seq == mg["first_seq"]:
                    po["merged_anchor"] = mg["anchor"]
                else:
                    po["merged_skip"] = mg["anchor"]
        return staged

    @staticmethod
    def _org_fallback_base(rounds: list[dict]) -> list[dict]:
        """base_messages 缺省时的兜底装配：批次轮的原始协议消息（直调/测试用）。"""
        return [e["message"] for r in rounds for e in r["events"]]

    def _split_rounds(
        self, rounds: list[dict], exchange: Optional[tuple],
        base_messages: Optional[list[dict]] = None,
    ) -> bool:
        """分裂分析（串行维护管线的第二阶段，2026-09-10 用户定稿）。

        输入 = 整理对话的**纯追加延续**：org 的 messages + assistant(提交
        调用) + org 产物 + 分裂指令。org 刚跑完 KV 全热——分裂白得完整
        整理产物（12K+），只付自己的指令 ~1.5K（缓存友好的关键）。
        判据三步（见 _SPLIT_INSTRUCTIONS）：保底块归属（独立思想归主
        agent）→ 按功能语义聚合文件域层级（不按路径）→ 找最浅可分层
        判分裂。硬数据（活性文件清单 + 上限）由 Runtime 注入，LLM 只做
        语义判断。开思考（用户拍板：不开思考不调工具且质量低）。
        Level 0 只分析不分裂：产物暂存待查。
        """
        if exchange is not None:
            org_messages, org_content, org_ordered = exchange
            messages = list(org_messages)
            if org_ordered:
                # 工具调用形态（真实路径）：重建提交对话保证结构合法
                messages.append({
                    "role": "assistant",
                    "content": org_content or None,
                    "tool_calls": [
                        {
                            "id": tc.get("id") or f"org_{i}",
                            "type": "function",
                            "function": {
                                "name": tc.get("name") or "submit_organization",
                                "arguments": tc.get("arguments") or "{}",
                            },
                        }
                        for i, tc in enumerate(org_ordered)
                    ],
                })
                messages.append({
                    "role": "tool",
                    "tool_call_id": (org_ordered[0].get("id") if org_ordered
                                     else "org_0"),
                    "content": "已收到整理产物。",
                })
            else:
                # 正文 JSON 形态（fallback）：assistant 原文即可
                messages.append({
                    "role": "assistant", "content": org_content or "",
                })
        else:
            messages = list(
                base_messages or self._org_fallback_base(rounds)
            )

        # 硬数据（Runtime 生成，零 LLM）：活性文件清单 + 数量上限
        hard_lines, n_live = self._split_hard_data(rounds)
        round_blocks, map_lines, _merged = self._block_map_lines(rounds)
        all_ids = {
            b["id"] for blocks in round_blocks.values() for b in blocks
        }
        seq_list = "、R".join(str(r["seq"]) for r in rounds)
        instruction = (
            "[分裂分析指令]\n"
            "以上是本会话的完整上下文（含刚完成的整理产物与分块地图）。"
            f"请做**现状归属分析**（不是话题分类），对象为这些轮次：R{seq_list}。\n\n"
            "[硬数据]（Runtime 生成，零 LLM）\n"
            + "\n".join(hard_lines)
            + f"\n- 活性文件数（分裂单元数上限）：{n_live}\n\n"
            "[分块地图]（块按工作对象确定性划分，条目格式 = 块ID=事件范围）\n"
            + "\n".join(map_lines)
            + "\n\n"
            + _SPLIT_INSTRUCTIONS
        )
        messages.append({"role": "user", "content": instruction})

        # 与 org 同策略：只留 submit_domains（收窄工具集是硬约束）
        # 开思考（不传 thinking disabled）：语义判断需要推理，实测
        # 不开思考不调工具、质量低（用户拍板）。
        split_tools = [
            s for s in self._schemas
            if s.get("function", {}).get("name") == "submit_domains"
        ]
        content, ordered, _usage = self._stream_call(
            messages, tools=split_tools, purpose="split",
        )
        product = self._extract_domains(content, ordered)
        if product is None:
            # 与 org 同策略：不重试（2026-09-10 用户拍板）
            return False
        domains, unassigned, split = product
        # 主 agent 兜底（2026-09-10，thoughts 字段收敛后）：没被任何域
        # 认领的块 = 独立思想/零散块 → Runtime 自动归 unassigned（主
        # agent 剩余集合）。模型忘了填 unassigned 也不丢块——语义上
        # "没有域认领"与"归主 agent"等价，不需要模型再声明一次。
        covered = {
            b for d in domains if isinstance(d, dict)
            for b in (d.get("block_ids") or [])
        }
        kept = [
            b for b in ((unassigned or {}).get("block_ids") or [])
            if b in all_ids
        ]
        orphans = sorted(all_ids - covered - set(kept))
        if orphans:
            unassigned = {
                "block_ids": kept + orphans,
                "reason": (unassigned or {}).get("reason")
                or "未被任何文件域认领（独立思想/零散块），归主 agent",
            }
            if self.task is not None:
                self.task.record(
                    "maintenance",
                    f"split：{len(orphans)} 块未被域认领，已自动归主 agent "
                    f"{orphans[:8]}" + ("…" if len(orphans) > 8 else ""),
                )
        # 暂存到批首轮（与 state_patch 同通道），下一轮开启随 promote 生效
        pending = rounds[0].setdefault("pending_org", {})
        pending["domains"] = domains
        if unassigned:
            pending["unassigned"] = unassigned
        if split:
            pending["split_assessment"] = split
        self._persist_rounds()
        return True

    def _split_hard_data(self, rounds: list[dict]) -> tuple[list[str], int]:
        """分裂硬数据（零 LLM）：活性文件清单 + 数量。

        新会话文件状态已变——必须现场重算 ledger（不信持久化状态）。
        只列 live / read_only（磁盘上存在的），dead 不列（分裂对象是
        现状，死文件不占域、不占上限）。
        """
        ledger = lifecycle_module.FileLedger()
        for rr in self.rounds:
            ledger.update(rr, blocks=blocks_module.segment_round_by_file(rr))
        lines = ["- 活性文件清单（状态 live/read_only，块引用供归属）："]
        n = 0
        for path, e in sorted(ledger.entries().items()):
            if e["state"] == lifecycle_module.STATE_DEAD:
                continue
            n += 1
            refs = " ".join(e.get("block_refs") or [])
            lines.append(
                f"  - {path}（{e['state']}；写 {e['write_count']}/"
                f"读 {e['read_count']}；块: {refs or '无'}）"
            )
        if n == 0:
            lines.append("  （无——当前无任何活性文件，无可分域）")
        return lines, n

    @staticmethod
    def _extract_domains(content: str, ordered: list):
        """从分裂分析响应提取产物：优先 submit_domains 调用参数，回退
        正文 JSON。返回 (domains, unassigned, split_assessment) 或 None。"""
        state = None
        for tc in ordered or []:
            if tc.get("name") != "submit_domains":
                continue
            try:
                state = json.loads(tc.get("arguments") or "{}")
            except json.JSONDecodeError:
                continue
            if isinstance(state, dict):
                break
            state = None
        if state is None:
            state = Agent._parse_state_json(content)
        if not isinstance(state, dict) or not state.get("domains"):
            return None
        domains = Agent._dedupe_domains(state.get("domains") or [])
        return (
            domains,
            state.get("unassigned") or {},
            state.get("split_assessment") or {},
        )

    @staticmethod
    def _dedupe_domains(domains: list) -> list:
        """代码层兜底（2026-09-10）：同块跨域重复（模型偶发）按先到先留
        去重——子上下文拼装时同块出现在两个域会浪费且引起归属歧义。
        域名单保持原顺序；重复只从后续域移除。"""
        seen: set = set()
        for d in domains:
            if not isinstance(d, dict):
                continue
            ids = d.get("block_ids") or []
            kept = []
            for bid in ids:
                if bid in seen:
                    continue
                seen.add(bid)
                kept.append(bid)
            d["block_ids"] = kept
        return domains

    def _parallel_maintenance(
        self, batch: list[dict], base: list[dict]
    ) -> tuple:
        """水位维护管线：整理 → 分裂（串行两阶段，2026-09-10 用户定稿）。

        原并行双路（各自骑 base 快照）改为串行追加：分裂作为整理对话的
        纯追加延续——org 刚跑完 KV 全热，分裂白得完整整理产物（12K+），
        只付自己的指令 ~1.5K（实测 org cached=83,968/命中 95%，tools
        收窄不影响 messages 前缀缓存）。**org 失败则 split 跳过**（分裂
        依赖整理质量，失败批次不产出）。
        **硬上限 WOVRA_MAINT_TIMEOUT**：两阶段总预算（F 组实测大基座
        深度思考 23 分钟不完成，读超时不触发——token 在流；到点判
        failed 解锁管线）。守护线程化保证进程退出不被挂起调用拖住。
        启动/结束落账 history，挂起可观测。返回 (org_ok, split_ok)。
        """
        if self.task is not None:
            self.task.record(
                "maintenance",
                f"启动：批次 R{batch[0]['seq']}-R{batch[-1]['seq']}"
                f"（{len(batch)} 轮，输入快照 {len(base)} 条消息，硬上限 {self._org_maint_timeout:.0f}s）",
            )
        results = {"org": False, "split": False}
        box: dict = {}

        def run() -> None:
            try:
                results["org"], exchange = self._organize_rounds(batch, base)
                box["exchange"] = exchange
            except Exception as error:  # noqa: BLE001——失败不拖垮管线
                for r in batch:
                    r.pop("pending_org", None)
                    r["org_state"] = "failed"
                self._persist_rounds()
                if self.task is not None:
                    self.task.record(
                        "maintenance", f"org 阶段失败：{str(error)[:150]}"
                    )
                return
            if not results["org"]:
                return  # org 失败：split 跳过（依赖整理质量）
            try:
                results["split"] = self._split_rounds(
                    batch, box.get("exchange"), base
                )
            except Exception as error:  # noqa: BLE001
                if self.task is not None:
                    self.task.record(
                        "maintenance", f"split 阶段失败：{str(error)[:150]}"
                    )

        thread = threading.Thread(
            target=run, name="wovra-maintenance", daemon=True
        )
        thread.start()
        thread.join(self._org_maint_timeout)
        timed_out = thread.is_alive()
        if self.task is not None:
            self.task.record(
                "maintenance",
                f"结束：org={results['org']} split={results['split']}"
                + ("（超时返回，挂起阶段随守护线程终结或迟到完成）" if timed_out else ""),
            )
        return results["org"], results["split"]

    def _promote_org_results(self) -> None:
        """把暂存的整理产物落进正式视图（仅在**新 Round 开启时**调用）。

        本轮对话期间装配必须保持原样（连贯性 + 缓存前缀稳定），所以
        整理线程只把产物写进各轮的 pending_org 暂存区；直到下一轮
        开启，才替换精修索引/Normalized 意图、应用状态补丁并落盘。
        崩溃安全：pending_org 随 rounds 一起持久化，重启后第一次开
        新轮时补生效。
        """
        changed = False
        for r in self.rounds:
            pending = r.pop("pending_org", None)
            if not pending:
                continue
            changed = True
            if pending.get("normalized"):
                r["user_input"]["normalized"] = pending["normalized"]
            if pending.get("key_constraints"):
                r["user_input"]["key_constraints"] = pending["key_constraints"]
            if pending.get("blocks") and not r.get("blocks"):
                r["blocks"] = pending["blocks"]  # 旧轮补块结构
            if pending.get("block_summaries"):
                r["block_summaries"] = pending["block_summaries"]
            if pending.get("refined_index"):
                r.setdefault("refined_index", {}).update(pending["refined_index"])
            # 合并组标记（连续纯聊天轮合并显示）：组首 anchor，非组首 skip
            if pending.get("merged_anchor"):
                r["merged_anchor"] = pending["merged_anchor"]
            if pending.get("merged_skip"):
                r["merged_skip"] = pending["merged_skip"]
            # 分裂分析产物（Level 0：只分析不分裂，落档待查）
            if pending.get("domains"):
                r["domains"] = pending["domains"]
            if pending.get("unassigned"):
                r["unassigned"] = pending["unassigned"]
            if pending.get("split_assessment"):
                r["split_assessment"] = pending["split_assessment"]
            patch = pending.get("state_patch")
            if patch and self.task is not None:
                self.task.apply_state_patch(patch)
        if changed:
            self._persist_rounds()

    def _read_full_event(self, event_id: str) -> str:
        """按事件 ID 返回完整原文，不做内容截断。

        旧会话数据可能带分离的 full 字段（旧版安全截断的产物），
        一并返回保证可读；新数据 message 即全文。
        """
        for r in self.rounds:
            for e in r["events"]:
                if e["id"] == event_id:
                    message = e["message"]
                    parts = [f"[{event_id}] {e['type']}"]
                    if message.get("tool_calls"):
                        parts.append(
                            "调用: "
                            + json.dumps(message["tool_calls"], ensure_ascii=False)
                        )
                    parts.append(message.get("content") or "")
                    if e.get("full"):
                        parts.append("[完整原文]\n" + e["full"])
                    return "\n".join(parts)
        return f"未找到事件: {event_id}"

    @staticmethod
    def _extract_org_state(content: str, ordered: list) -> Optional[dict]:
        """从整理响应提取产物：优先 submit_organization 的调用参数，
        回退消息正文 JSON（自由文本输出兼容，主路径是工具出口）。"""
        for tc in ordered or []:
            if tc.get("name") != "submit_organization":
                continue
            try:
                state = json.loads(tc.get("arguments") or "{}")
            except json.JSONDecodeError:
                continue
            if isinstance(state, dict):
                return state
        return Agent._parse_state_json(content)

    @staticmethod
    def _parse_state_json(text: str) -> Optional[dict]:
        """从整理输出里解析 JSON；容忍围栏与前后说明文字。"""
        raw = (text or "").strip()
        if not raw:
            return None
        if raw.startswith("```"):
            raw = raw.strip("`")
        start, end = raw.find("{"), raw.rfind("}")
        if start == -1 or end <= start:
            return None
        try:
            state = json.loads(raw[start:end + 1])
        except json.JSONDecodeError:
            return None
        return state if isinstance(state, dict) else None

    # ---- expand_history（设计文档第 12 节） --------------------------------------

    def expand_history(self, ids: list[str] | str, level: str = "full") -> str:
        """按需展开历史：Truncated → Summary（意图+索引）→ Full 三档读取。

        ids 可为轮（"R3"）、块（"R3-B2"，取回整块原文）或事件（"R3-E02"），
        容错逗号字符串与大小写；一次可传多个，无调用次数上限。展开只是
        临时把更高分辨率的信息读进当前上下文，不修改历史。
        """
        if isinstance(ids, str):
            ids = [s.strip() for s in ids.split(",") if s.strip()]
        level = (level or "full").strip().lower()
        if level not in ("truncated", "summary", "full"):
            return f"未知级别: {level}，可选 truncated / summary / full"
        results = []
        for rid in ids:
            if "-B" in rid:
                results.append(self._expand_block(rid))
            elif "-E" in rid:
                results.append(
                    self._read_full_event(rid) if level == "full" else self._event_summary(rid)
                )
            else:
                results.append(self._expand_round(rid, level))
        return "\n\n".join(results) or "未找到任何 ID"

    def _event_summary(self, event_id: str) -> str:
        for r in self.rounds:
            refined = r.get("refined_index") or {}
            for e in r["events"]:
                if e["id"] == event_id:
                    line = refined.get(event_id) or e["truncated"]
                    return f"[{event_id}] {line}"
        return f"未找到事件: {event_id}"

    def _expand_round(self, round_id: str, level: str) -> str:
        try:
            seq = int(round_id.lstrip("Rr"))
        except ValueError:
            return f"轮次 ID 无效: {round_id}"
        for r in self.rounds:
            if r["seq"] != seq:
                continue
            if level in ("summary", "truncated"):
                lines = [f"[R{seq}] 用户：{r['user_input']['original']}"]
                if r["user_input"].get("normalized"):
                    lines.append(f"意图：{r['user_input']['normalized']}")
                summaries = r.get("block_summaries") or {}
                if summaries:
                    # 视图优先（2026-09-10 折叠行的两级展开：行→视图→原文）
                    lines.append("块视图：")
                    for bid in sorted(
                        summaries, key=lambda x: int(x.rsplit("-B", 1)[-1])
                    ):
                        lines.append(f"▸ {bid}: {summaries[bid]}")
                else:
                    lines += self._round_index_lines(r)
                return "\n".join(lines)
            parts = [f"[R{seq}] 用户：{r['user_input']['original']}"]
            for e in r["events"]:
                if e["type"] == "user":
                    continue
                parts.append(
                    f"--- {e['id']} ({e['type']}) ---\n"
                    + (e.get("full") or e["message"].get("content") or "")
                )
            return "\n".join(parts)
        return f"未找到轮次: {round_id}"

    def _expand_block(self, block_id: str) -> str:
        """按块 ID（如 R9-B14）取回该块全部事件的原文（紧凑视图的回放通道）。

        紧凑视图 2026-09-08 起以块描述为主索引、事件行退场，块 ID 是
        视图里唯一保留的定位锚——expand_history 必须认得它。
        """
        head = block_id.lstrip("Rr").split("-B")[0]
        try:
            seq = int(head)
        except ValueError:
            return f"块 ID 无效: {block_id}"
        for r in self.rounds:
            if r["seq"] != seq:
                continue
            block = next(
                (b for b in r.get("blocks") or [] if b["id"] == block_id), None
            )
            if block is None:
                return f"未找到块: {block_id}（该轮无分块结构或块号不存在）"
            parts = [
                f"[{block_id}] {block['start_event']}~{block['end_event']}"
                f"（写: {', '.join(block['wrote_files']) or '无'}）"
            ]
            for i in range(block["start"], block["end"] + 1):
                e = r["events"][i]
                message = e["message"]
                body = message.get("content") or ""
                if message.get("tool_calls"):
                    calls = json.dumps(message["tool_calls"], ensure_ascii=False)
                    body = f"调用: {calls}" + (f"\n{body}" if body else "")
                parts.append(f"--- {e['id']} ({e['type']}) ---\n{body}")
            return "\n".join(parts)
        return f"未找到块: {block_id}"

    # ---- baseline 阈值压缩（设计文档第 6 节） ------------------------------------

    def _baseline_context_estimate(self) -> int:
        """下一次请求的上下文体量估算（窗口占用口径）。

        摘要 + 未压缩轮次的全部事件（正文与工具调用参数）。
        水位曾误用累计计费口径——每步重复计整段 prompt，成倍虚增
        （实测真实上下文 3 万 tok、账本 58 万，提前触发压缩）。
        """
        parts = []
        if self.task is not None and self.task.baseline_summary:
            parts.append(self.task.baseline_summary)
        for r in self.rounds:
            if r.get("compacted"):
                continue
            for e in r["events"]:
                m = e["message"]
                parts.append(str(m.get("content") or ""))
                for tc in m.get("tool_calls") or []:
                    fn = tc.get("function") or {}
                    parts.append(str(fn.get("arguments") or ""))
        return tokens.estimate("\n".join(parts))

    def _baseline_accounting(self) -> None:
        """baseline：真实上下文体量达 80% × 窗口时触发阈值压缩。

        水位 = _baseline_context_estimate（下一次请求的体量），与
        managed 的窗口保底同口径；触发后压缩较早轮次为摘要，水位
        下次检查时自然反映压缩后的体量。baseline_prompt_used 只做
        计费口径的成本记录，不参与触发。
        """
        self._baseline_prompt_used += self.last_stats["prompt_tokens"]
        if self.task is not None:
            self.task.baseline_prompt_used = self._baseline_prompt_used
        threshold = self.context_limit * _COMPRESS_THRESHOLD
        if self._baseline_context_estimate() < threshold:
            return
        older = [r for r in self.rounds if not r.get("compacted")][:-2]
        if len(older) < 1:
            return  # 保留最近 2 轮原文；没有可压缩的历史就等下一轮
        self._emit_status("历史接近窗口上限，正在压缩较早的对话…")
        transcript = "\n\n".join(
            f"[R{r['seq']}] " + truncate.render_round_events(r) for r in older
        )
        summary, _ordered, _usage = self._stream_call(
            [{
                "role": "user",
                "content": (
                    "以下是长会话较早阶段的对话记录（截断索引形式）。"
                    "请压缩成一段高密度摘要，保留：任务相关结论、重要决策、"
                    "已尝试方案与结果、未解决的问题。省略寒暄与重复输出。"
                    "直接输出摘要正文。\n\n" + transcript
                ),
            }],
            purpose="compaction",
            extra_body={"thinking": {"type": "disabled"}},
        )
        if self.task is not None:
            prior = self.task.baseline_summary
            self.task.baseline_summary = (
                (prior + "\n\n" if prior else "") + summary.strip()
            )
        for r in older:
            r["compacted"] = True


def _sanitize_json_strings(obj):
    """递归清洗结构里的未配对代理项（见 task.sanitize_surrogates）。"""
    if isinstance(obj, str):
        return sanitize_surrogates(obj)
    if isinstance(obj, list):
        return [_sanitize_json_strings(v) for v in obj]
    if isinstance(obj, dict):
        return {k: _sanitize_json_strings(v) for k, v in obj.items()}
    return obj


def _action_word(name: str) -> str:
    """工具名 → 进度提示的动作词（未知工具退回"调用 xxx"）。"""
    return _ACTION_WORDS.get(name, f"调用 {name}")


def _runtime_reminder(text: str) -> dict:
    """运行时专属注入通道（zcode-borrowings.md 1.1）。

    机制信息（任务状态、文件地图、运行时意志）用 <runtime-reminder>
    信封包裹后以 user-role 注入——OpenAI 协议没有独立的 reminder role，
    用"信封 + 系统提示词声明"实现身份可辨：模型分得清这是运行时
    在说话，不是用户在发言。
    """
    return {"role": "user", "content": f"<runtime-reminder>\n{text}\n</runtime-reminder>"}


def _schema_of(fn: Callable) -> dict:
    """根据函数签名自动生成 OpenAI tools 协议要求的 JSON Schema。"""
    properties = {}
    for name, param in inspect.signature(fn).parameters.items():
        annotation = param.annotation
        # Optional[X]（X | None）取 X 的类型，避免退化为 string
        args_ = getattr(annotation, "__args__", None)
        if args_ and type(None) in args_:
            non_none = [a for a in args_ if a is not type(None)]
            if len(non_none) == 1:
                annotation = non_none[0]
        json_type = _JSON_TYPES.get(annotation, "string")
        properties[name] = {"type": json_type}

    doc = inspect.getdoc(fn)
    # 描述取首段（空行分隔、折叠空白）：关键使用约束（如 run_command
    # 的超时与常驻服务警告）往往一行装不下，首段才能完整送达模型
    description = (
        " ".join(doc.split("\n\n")[0].split()) if doc else fn.__name__
    )

    return {
        "type": "function",
        "function": {
            "name": fn.__name__,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": [
                    name
                    for name, p in inspect.signature(fn).parameters.items()
                    if p.default is inspect.Parameter.empty
                ],
            },
        },
    }
