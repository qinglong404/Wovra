"""Wovra：面向结构化、长时运行 AI 工作的运行时。"""

from pathlib import Path

from dotenv import load_dotenv

# .env 必须在任何子模块导入前加载：工具层的 PROJECT_ROOT（工作区）
# 等配置在导入期读取环境变量。llm.py 里的重复调用是无害幂等。
_PACKAGE_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(dotenv_path=_PACKAGE_ROOT / ".env")
