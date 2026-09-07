"""python -m wovra：后台进程入口。

后台任务治理（run_background）与脚本化运行都以独立进程形式使用
`python -m wovra ...`——本模块让 `-m` 形式可用。
"""

from .cli import main

if __name__ == "__main__":
    main()
