#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PickToClean.py — 把工作分支的 commit 搬到干净分支，先过两道闸。

用法:
    py -3 PickToClean.py <commit> [<commit> ...]   # 旧->新 顺序

从本仓库任一 worktree 内运行。分支 ref / 代码路径 / 白名单来自两层配置。

搬运本身就是 `git cherry-pick` + `--reset-author`；这个脚本的价值全在动手**之前**
那两道闸，它们各自对应一种「成功退出但结果是错的」：

  * **文档守卫** —— 干净分支的不变量是「每条提交只含代码」。一条混了文档的提交
    照样能 pick 成功,于是文档静静地进了对外分支的记录,而 git 什么都不会说。
    所有 commit 一起预检,任一命中就在动手前整体拒绝。
  * **身份核对** —— `--reset-author` 拿的是这个 worktree 的 git config,这是对的,
    git config 才是唯一真相源。但一份新检出可能根本没配过,于是提交签上个人身份
    落进对外分支,而一切看起来都成功了。核对而不是覆盖:对不上就停,并给出命令。

冲突不自动解:现场原样保留,给出接手步骤。

退出码:
    0 = 全部完成
    1 = cherry-pick 冲突（现场保留给人工处理）
    2 = 前置条件不满足 / 配置缺失 / 脚本错误
"""
import os
import subprocess
import sys
from fnmatch import fnmatch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import CleanBranch as cb  # noqa: E402  复用 clean_wt()/DOC_GLOBS/ALLOW（唯一真相源）
import local_config  # noqa: E402

# Windows 控制台默认 cp936，强制 UTF-8 输出避免中文乱码
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# git 运行器
#   不复用 CleanBranch.git()：它遇到任何非零退出就 sys.exit(2)，而本脚本必须能
#   分辨 cherry-pick 的冲突退出、`rev-parse --verify` 的「坏 ref」退出——这些都是
#   预期的非零，不能当脚本错误。
# ---------------------------------------------------------------------------
def _run(wt, *args):
    """在 wt 跑 git，返回 CompletedProcess（不因非零退出而中止）。
    始终关闭 quotepath 以拿到可读的 UTF-8 路径。"""
    return subprocess.run(
        ["git", "-C", wt, "-c", "core.quotepath=false", *args],
        capture_output=True, encoding="utf-8")


def _out(wt, *args):
    """跑 git 且必须成功；成功返回 stdout（去尾换行），失败打印并 exit 2。"""
    r = _run(wt, *args)
    if r.returncode != 0:
        sys.stderr.write("git 失败: " + " ".join(args) + "\n" + (r.stderr or "") + "\n")
        sys.exit(2)
    return r.stdout.rstrip("\n")


def _is_doc(path):
    """path 是否命中 DOC_GLOBS。注意 `docs/` 是前缀（目录），其余是 basename 级 glob。"""
    for g in cb.DOC_GLOBS:
        if g.endswith("/"):
            if path == g.rstrip("/") or path.startswith(g):
                return True
        elif fnmatch(path, g):
            return True
    return False


def _conflict_files(wt):
    out = _out(wt, "diff", "--name-only", "--diff-filter=U")
    return [p for p in out.splitlines() if p]


def _print_conflict_guidance(wt, src_hash):
    sys.stdout.flush()   # 否则 stderr 先冲出来，指引会印在前面几条 "picked ..." 之上
    files = " ".join(_conflict_files(wt)) or "<冲突文件>"
    msg = (
        f"cherry-pick {src_hash} 冲突，现场已保留。\n"
        f"  1) 在干净分支 worktree 手工解冲突（禁止 -X theirs/ours）\n"
        f"  2) git -C {wt} add -A -- {files}\n"
        f"  3) git -C {wt} cherry-pick --continue\n"
        f"  4) git -C {wt} commit --amend --reset-author --no-edit   # 作者归一\n"
        f"  5) 重跑本脚本处理剩余 commit\n"
    )
    sys.stderr.write(msg)


def _cherry_pick_in_progress(wt):
    r = _run(wt, "rev-parse", "--verify", "-q", "CHERRY_PICK_HEAD")
    return r.returncode == 0


def _check_identity(wt, cfg):
    """确认这个 worktree 会用干净分支该用的身份签名。

    没在配置里声明身份就没什么可核对的,跳过 —— 不能因为「没配」拦住所有人。
    """
    want = cfg.get("identity.clean")
    if not isinstance(want, dict):
        return 0
    name = _run(wt, "config", "user.name").stdout.strip()
    email = _run(wt, "config", "user.email").stdout.strip()
    if name == want.get("name") and email == want.get("email"):
        return 0
    sys.stderr.write(
        "干净分支 worktree 的提交身份与配置不符,拒绝搬运:\n"
        f"  worktree 现在会签: {name or '(未设置)'} <{email or '(未设置)'}>\n"
        f"  配置要求的是:      {want.get('name')} <{want.get('email')}>\n"
        "改配置,或者把 worktree 配对:\n"
        f'  git -C {wt} config --worktree user.name "{want.get("name")}"\n'
        f'  git -C {wt} config --worktree user.email "{want.get("email")}"\n'
        "(--worktree 需要仓库层 extensions.worktreeConfig=true)\n")
    return 2


def _preflight(wt, commits, cfg):
    """全部前置检查，任何一条不过就在动手前返回错误码 (2)；通过返回 0。"""
    # a. 无进行中的 cherry-pick。
    #    必须排在「工作区干净」前面:一场没收拾的冲突同时让两条都不满足,而先报
    #    「先清理工作区」是把人往错路上指 —— 清理解决不了它,要的是 --abort/--quit。
    if _cherry_pick_in_progress(wt):
        sys.stderr.write("检测到进行中的 cherry-pick（CHERRY_PICK_HEAD 存在），"
                         "请先 git cherry-pick --quit/--abort 后再运行。\n")
        return 2
    # b. 工作区必须干净
    status = _out(wt, "status", "--porcelain")
    if status.strip():
        sys.stderr.write("先清理干净分支 worktree（git status 非空）:\n" + status + "\n")
        return 2
    # c. 提交身份必须是干净分支该用的那个
    rc = _check_identity(wt, cfg)
    if rc:
        return rc
    # d. 每个 commit 都必须能解析（doc 守卫要跑 git show，须先确保 hash 有效）
    for h in commits:
        r = _run(wt, "rev-parse", "--verify", "-q", h + "^{commit}")
        if r.returncode != 0:
            sys.stderr.write(f"无法解析 commit: {h}\n")
            return 2
    # e. 文档守卫：所有 commit 一起预检，任一命中就在动手前退出（列出越界文件）
    offending = []
    for h in commits:
        names = _out(wt, "show", "--name-only", "--pretty=format:", h)
        for p in names.splitlines():
            p = p.strip()
            if p and _is_doc(p) and p not in cb.ALLOW:
                offending.append(f"{h}: {p}")
    if offending:
        sys.stderr.write("拒绝：以下 commit 含文档改动（工作分支→干净分支不搬文档）:\n    "
                         + "\n    ".join(offending) + "\n")
        return 2
    return 0


def _pick_one(wt, src_hash):
    """搬一条 commit。返回:
        ("done", new_short_hash) — 已落到干净分支
        ("skip", None)          — 内容与干净分支已一致，pick 出来是空提交
        ("conflict", None)      — 冲突，现场已保留（调用方 exit 1）
    """
    # --keep-redundant-commits 不给:内容已在对面时宁可跳过,也不要在对外分支的
    # 记录里留一条什么都没改的提交。
    r = _run(wt, "cherry-pick", src_hash)
    if r.returncode != 0:
        combined = (r.stdout or "") + (r.stderr or "")
        if "empty" in combined or "nothing to commit" in combined:
            _run(wt, "cherry-pick", "--skip")
            return ("skip", None)
        _print_conflict_guidance(wt, src_hash)
        return ("conflict", None)

    # 作者归一到本 worktree 的身份（已在 _preflight 核对过与 identity.clean 一致）。
    # 单独 amend 而不是 pick 时带上:cherry-pick 没有 --reset-author。
    _out(wt, "commit", "--amend", "--reset-author", "--no-edit")
    return ("done", _out(wt, "rev-parse", "--short", "HEAD"))


def main(argv, cfg=None):
    if not argv:
        sys.stderr.write(__doc__)
        return 2

    if cfg is None:
        try:
            cfg = cb.configure()
        except local_config.ConfigError as exc:
            sys.stderr.write(str(exc) + "\n")
            return 2

    wt = cb.clean_wt()   # 惰性解析（测试通过 CleanBranch._WT_CACHE 注入 fixture）

    rc = _preflight(wt, argv, cfg)
    if rc != 0:
        return rc

    picked = []
    for src_hash in argv:      # 旧->新
        kind, new_hash = _pick_one(wt, src_hash)
        if kind == "conflict":
            return 1
        elif kind == "skip":
            print(f"skip {src_hash} (内容已在干净分支，pick 出来是空提交)")
        else:
            picked.append((src_hash, new_hash))
            print(f"picked {new_hash}  <- {src_hash}")

    if picked:
        print("\n下一步跑 CleanBranch.py verify")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
