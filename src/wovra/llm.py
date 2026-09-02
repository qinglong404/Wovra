"""LLM 客户端：Wovra 中所有与模型交互的代码都统一经过这里。

统一收口的原因：换模型、换服务商、调整默认参数时只需要改这一个文件，
而不是散落在各个组件里。这也是 README 中 "Task Manager 之下复用
现有能力" 思路的一部分——模型调用是最基础的底层能力。
"""

import os
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv
from openai import OpenAI

# .env 约定放在项目根目录（本文件位于 src/wovra/，向上三级即根目录）。
# 包被安装到别处时该路径不存在，load_dotenv 会静默跳过，不影响运行；
# 真正的部署环境应当用真实的环境变量注入配置。
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
load_dotenv(dotenv_path=_PROJECT_ROOT / ".env")


class LLM:
    """对 OpenAI 协议客户端的薄封装。

    只做三件事：读取配置、持有客户端、转发调用。
    刻意不做流式封装、不做重试——阶段 1 保持最小，够用就好。
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
    ) -> None:
        # 参数优先，其次环境变量（.env 已在上面加载进环境），最后兜底默认值。
        # 显式传入 > .env > 默认 的顺序让测试和临时换模型都很方便。
        self.model = model or os.environ.get("Wovra_MODEL", "gpt-4o-mini")
        api_key = api_key or os.environ.get("Wovra_API_KEY", "")
        base_url = base_url or os.environ.get("Wovra_BASE_URL")

        if not api_key:
            raise RuntimeError(
                "未配置 API 密钥。请在项目根目录 .env 中填写 Wovra_API_KEY，"
                "或通过 LLM(api_key=...) 传入。"
            )

        # base_url 允许为 None：此时 SDK 使用 OpenAI 官方地址。
        self._client = OpenAI(api_key=api_key, base_url=base_url)

    def chat(self, messages: list[dict], tools: Optional[list[dict]] = None, **kwargs: Any):
        """发送一次对话补全请求，返回原始 response 对象。

        返回原始对象而不是只返回文本：工具调用场景需要访问
        response 里的 tool_calls、finish_reason 等细节，封装掉反而碍事。
        tools=None 时 SDK 会自动省略该参数，不影响普通对话。
        """
        return self._client.chat.completions.create(
            model=self.model,
            messages=messages,
            tools=tools,
            **kwargs,
        )
