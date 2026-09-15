"""共享 worklog 的"只暂存我这一节"——并行会话共存时的提交纪律。

背景（2026-09-14 实测）：`docs/worklog-20260911.md` 是**多会话共用**的文件，
base HEAD 止于 §101，而工作区里 §102（预备归属）、§103 直播块闪烁、§104
前端审计都是**别人未提交的在途改动**。直接 `git add` 会把这些一起裹进我的提交。

做法：用 `git hash-object -w` + `update-index --cacheinfo` 构造
「base HEAD + 我这一节（插在原位置）」的中间版本进索引；
工作区文件**完整保留**（含别人的节），编辑面最小。

用法：改 SECTION_START / SECTION_END / RENUMBER 后直接跑。
一次性但留档（口径：测试/取证脚本不删）。
"""

import subprocess

REPO = "/home/lkf/bc/python/Wovra"
WORKLOG = "docs/worklog-20260911.md"
SECTION_START = "## 103. 网络工具一步到位升级"   # 我这一节的标题（定位用）
NEXT_SECTION = "## §103 直播块闪烁"               # 下一节标题（本节到此为止）
RENUMBER = ("103", "105")                         # 与并行会话撞号 → 改号


def git(*args, input_text=None):
    return subprocess.run(("git", *args), cwd=REPO, capture_output=True,
                          text=True, input=input_text)


def main():
    base = git("show", f"HEAD:{WORKLOG}").stdout
    with open(f"{REPO}/{WORKLOG}", encoding="utf-8") as fh:
        full = fh.read()

    i = full.index(SECTION_START)
    j = full.index(NEXT_SECTION)
    section = full[i:j].rstrip()
    if section.endswith("---"):          # 尾部分隔线不属于本节
        section = section[:-3].rstrip()
    old_no, new_no = RENUMBER
    section = (section.replace(f"## {old_no}. ", f"## {new_no}. ")
               .replace(f"### {old_no}.", f"### {new_no}.")) + "\n"

    # 工作区：只改号，保持原位（别人的节原样不动）
    with open(f"{REPO}/{WORKLOG}", "w", encoding="utf-8") as fh:
        fh.write(full[:i] + section + full[j:])

    # 索引：base + 我这一节（插在 base 末尾，即 §101 之后——
    # §102 是别人的在途，不进索引，故我的节紧跟 base 尾部即可）
    staged = base.rstrip("\n") + "\n\n" + section
    blob = git("hash-object", "-w", "--stdin", input_text=staged).stdout.strip()
    upd = git("update-index", "--cacheinfo", f"100644,{blob},{WORKLOG}")
    print("blob:", blob, "| update-index rc:", upd.returncode, upd.stderr.strip())
    print("staged 行数:", staged.count("\n"), "| section 行数:", section.count("\n"))
    print("工作区行数:", full.count("\n"), "→", (full[:i] + section + full[j:]).count("\n"))


if __name__ == "__main__":
    main()
