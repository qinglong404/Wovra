"""Block 结构化（上下文分化运行时 · 机制一）：纯规则、零 LLM。

docs/context-differentiation-runtime.md §2 机制一。轮内以**写/改文件**
为截止划分 Block；shell 命令打分类标签（v1 六类）；环境配置天然单独
组块；一轮没有写/改 → 整轮一块。全部是确定性规则——Runtime 先把明显
的结构切出来，语义归后面的低频批量标注（机制二）。

segment_round 是事件流的纯函数：不读运行时状态、不调模型，对历史
task.json 同样成立（scripts/render_blocks.py 即此离线用法）。

Block v1 结构（最小集 + 渲染辅助字段）：
    {
      "id": "R17-B1",
      "kind": "work" | "environment",
      "start": 0, "end": 4,             # 事件下标区间（含端点）
      "start_event": "R17-E01",
      "end_event": "R17-E05",
      "touched_files": ["icp.py"],      # 块内涉及的文件路径（去重保序）
      "wrote_files": ["icp.py"],        # 其中被写/改的（块截止的依据）
      "command_types": ["test"],        # 块内 shell 命令标签（去重保序）
    }
事件原文不复制——渲染/整理时按下标回查，块本身零 token 成本。"""

# 包结构（2026-09-11 重构，纯搬迁）：
#     common.py   共享常量与微工具（工具集合/命令分类/调用解析）
#     segment.py  分块：segment_round(v1) + segment_round_by_file(v3 主线)
#     labels.py   生命周期标签 → 标签行
#     digest.py   block_digest / render_round
# 全部原名（含私有）经本文件再导出，外部与测试的引用路径不变。

from .common import (  # noqa: F401
    FILE_OP_TOOLS,
    READ_TOOLS,
    WRITE_TOOLS,
    tag_command,
)
from .common import _TAG_RULES, _call_info, _head, _remember  # noqa: F401
from .segment import (  # noqa: F401
    segment_round,
    segment_round_by_file,
)
from .segment import _ESCAPE_MARKERS, _NOT_FOUND_MARKERS, _VERIFY_TAGS, _block_kind, _fblock, _finalize, _finalize_fblock, _new_block, _op_failure  # noqa: F401
from .labels import (  # noqa: F401
    block_end_state,
    label_line,
    lifecycle_tags,
    round_has_tool_calls,
    state_label,
)
from .digest import (  # noqa: F401
    block_digest,
    render_round,
)
