"""python -m wovra：组织运行时的后台入口。

子任务派发（dispatch_subtask）以独立进程运行 `python -m wovra run
<子任务id>`——进程隔离让父子会话各自持有全局状态，人的终端永远不被
阻塞。本模块只是让 `-m` 形式可用。
"""

from .cli import main

if __name__ == "__main__":
    main()
