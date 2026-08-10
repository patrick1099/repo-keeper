#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PickToClean.py — 把工作分支的 commit 搬到干净分支，途中自动剥离私人注释。

用法:
    py -3 PickToClean.py <commit> [<commit> ...]   # 旧->新 顺序

从本仓库任一 worktree 内运行。分支 ref / 代码路径 / 白名单来自两层配置。

=== 核心机制（本方案立身之本，不要改成手动调剥离脚本）===
`git cherry-pick -n` 把工作分支的**原始 blob**（带私人注释）落到 index + 工作区，
**不过 filter**。随后 `git add` 会**重新从工作区读文件并应用 clean filter**——而干净
分支 worktree 的 filter 就是剥离脚本。于是「剥离」复用现有 filter 配置这一**唯一真相源**：
本脚本自己**绝不**调剥离脚本、不复制 marker 正则。

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
#   分辨 cherry-pick 的冲突退出、`diff --cached --quiet` 的「有差异」退出、
#   `rev-parse --verify` 的「坏 ref」退出——这些都是预期的非零，不能当脚本错误。
#   故本地实现一个**不退出**的运行器；worktree 解析 / DOC_GLOBS / ALLOW 仍复用 CleanBranch。
# ---------------------------------------------------------------------------
def _run(wt, *args):
    """在 wt 跑 git，返回 CompletedProcess（不因非零退出而中止）。
    始终关闭 quotepath 以拿到可读的 UTF-8 路径。绝不解码文件内容——只处理路径/hash 文本。"""
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


def _changed_paths(wt):
    """cherry-pick -n 之后，相对 HEAD 被改动的路径（含删除）。"""
    out = _out(wt, "diff", "--cached", "--name-only", "--diff-filter=ACMRD")
    return [p for p in out.splitlines() if p]


def _conflict_files(wt):
    out = _out(wt, "diff", "--name-only", "--diff-filter=U")
    return [p for p in out.splitlines() if p]


def _print_conflict_guidance(wt, src_hash):
    sys.stdout.flush()   # 否则 stderr 先冲出来，指引会印在前面几条 "picked ..." 之上
    files = " ".join(_conflict_files(wt)) or "<冲突文件>"
    msg = (
        f"cherry-pick {src_hash} 冲突，现场已保留。\n"
        f"  1) 在干净分支 worktree 手工解冲突（禁止 -X theirs/ours）\n"
        f"  2) git -C {wt} add -A -- {files}        # add 会自动剥离私人注释\n"
        f"  3) git -C {wt} commit -C {src_hash} --reset-author\n"
        f"  4) git -C {wt} cherry-pick --quit\n"
        f"  5) 重跑本脚本处理剩余 commit\n"
    )
    sys.stderr.write(msg)


def _cherry_pick_in_progress(wt):
    """是否有进行中的 cherry-pick（人工残留）。CHERRY_PICK_HEAD 只由**非 -n** 的
    cherry-pick 设置；本脚本用的 -n 无论成功/冲突都不设它，故这是纯粹的外部残留守卫。"""
    r = _run(wt, "rev-parse", "--verify", "-q", "CHERRY_PICK_HEAD")
    return r.returncode == 0


def _check_identity(wt, cfg):
    """确认这个 worktree 会用干净分支该用的身份签名。

    `commit --reset-author` 拿的是这个 worktree 的 git config,不是本脚本给的值 ——
    这是对的,git config 才是唯一真相源。但一份新检出可能根本没配过,于是提交会
    悄悄签上个人身份落进对外分支,而一切看起来都成功了。所以这里不覆盖、只核对:
    对不上就在动手前停下。没在配置里声明身份就没什么可核对的,跳过。
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
    # a. 工作区必须干净
    status = _out(wt, "status", "--porcelain")
    if status.strip():
        sys.stderr.write("先清理干净分支 worktree（git status 非空）:\n" + status + "\n")
        return 2
    # b. 无进行中的 cherry-pick
    if _cherry_pick_in_progress(wt):
        sys.stderr.write("检测到进行中的 cherry-pick（CHERRY_PICK_HEAD 存在），"
                         "请先 git cherry-pick --quit/--abort 后再运行。\n")
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


def _is_comment_only(wt, src_hash):
    """src_hash 相对其父提交是否**只改了私人注释**（剥离后无任何变化）。

    必须在 cherry-pick **之前**判断。实测教训：若前一条 commit 已被剥离落到干净分支，
    那边的文件里就没有那行私人注释了；这条纯注释 commit 的 diff 正以那行为上下文，
    三方合并变成「我方删了此行 / 对方在此行后加行」→ **冲突**，
    永远走不到 cherry-pick 之后的「剥离后为空」检查。
    """
    parent = _run(wt, "rev-parse", "--verify", "-q", src_hash + "^")
    if parent.returncode != 0:      # 根提交，没有父 → 不算纯注释
        return False
    r = _run(wt, "diff", "--name-status", src_hash + "^", src_hash)
    if r.returncode != 0:
        return False
    lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
    if not lines:
        return False                # 空 commit，交给后面的常规流程
    for ln in lines:
        parts = ln.split("\t")
        status, path = parts[0], parts[-1]
        if status != "M":           # 增/删/改名 一律不算纯注释
            return False
        if not path.endswith((".c", ".h")):
            return False
        old = cb._blob(src_hash + "^", path)
        new = cb._blob(src_hash, path)
        if old is None or new is None:
            return False
        if cb.strip_bytes(old) != cb.strip_bytes(new):
            return False
    return True


def _pick_one(wt, src_hash):
    """搬一条 commit。返回:
        ("done", new_short_hash) — 已产出剥离后的干净分支 commit
        ("skip", None)          — 剥离后为空，跳过
        ("conflict", None)      — 冲突，现场已保留（调用方 exit 1）
    """
    # a0. 纯注释 commit：在 cherry-pick 之前就跳过，否则会因上下文不符而冲突（见 _is_comment_only）
    if _is_comment_only(wt, src_hash):
        return ("skip", None)

    # a. cherry-pick -n：原始 blob 进 index+工作区，不过 filter
    r = _run(wt, "cherry-pick", "-n", "--allow-empty", src_hash)
    if r.returncode != 0:
        # -n 冲突不会设 CHERRY_PICK_HEAD，但会留下未合并的 index 条目 + 工作区冲突标记
        _print_conflict_guidance(wt, src_hash)
        return ("conflict", None)

    # b. 本次改动的路径
    changed = _changed_paths(wt)

    # c. 关键：把仍存在于工作区的文件**重新过 filter 落库**（.c/.h 被剥离）。
    #    - 删除的路径已被 cherry-pick 记为删除，无需（也不能）再 `git add`：
    #      对已消失的文件 `git add -- <path>` 会报 "pathspec did not match any files"。
    #    - changed 为空（内容与干净分支完全一致的空 pick）时不加，直接走下面的 skip。
    existing = [p for p in changed
                if os.path.exists(os.path.join(wt, p.replace("/", os.sep)))]
    if existing:
        _out(wt, "add", "--", *existing)

    # d. 剥离后相对 HEAD 无实质改动 → 纯注释 commit，跳过（不产空 commit）
    diff = _run(wt, "diff", "--cached", "--quiet", "HEAD")
    if diff.returncode == 0:
        _run(wt, "cherry-pick", "--quit")     # 无进行中的 pick 时是安全 no-op
        _out(wt, "reset", "--hard", "HEAD")   # 清掉工作区里未剥离的残留文本
        return ("skip", None)

    # e. 复用原 message；--reset-author 把 author 改成当前身份
    #    （身份已在 _preflight 里核对过与 identity.clean 一致）
    _out(wt, "commit", "-C", src_hash, "--reset-author")
    new_hash = _out(wt, "rev-parse", "--short", "HEAD")

    # f. 让干净分支工作区文本 == 其（已剥离的）blob，磁盘上不留私人注释。
    #    只处理仍存在的 .c/.h（删除的文件无须、也不能 checkout）。
    #    坑：不能直接 `git checkout -- <path>`——cherry-pick -n 落到磁盘的是**未剥离**文本，
    #    但 clean filter 会把它归一化成与已提交 blob 相等，于是 git 认为「无差异」而不覆盖。
    #    故先删磁盘文件，再 checkout（文件缺失时 git 必定用 blob 经 smudge 重写 = 剥离后文本）。
    #    实测：`git checkout -- p` 与 `git checkout-index -f -- p` 在 `git add` 之后都是 no-op
    #    （index 的 stat 缓存与磁盘文件相符，git 认为无需重写）。只有先删再 checkout 有效。
    to_refresh = [p for p in existing if p.endswith((".c", ".h"))]
    if to_refresh:
        for p in to_refresh:
            try:
                os.remove(os.path.join(wt, p.replace("/", os.sep)))
            except OSError:
                pass
        # 此处**已经删掉了磁盘文件**：若 checkout 失败绝不能只丢一句 "git 失败" 就退，
        # 否则 worktree 会静默缺文件。给出明确的恢复命令。
        r = _run(wt, "checkout", "--", *to_refresh)
        if r.returncode != 0:
            sys.stderr.write(
                "commit 已成功（" + new_hash + "），但刷新工作区失败：\n"
                + (r.stderr or "") + "\n"
                + "以下文件已从磁盘删除、尚未从 blob 恢复：\n    "
                + "\n    ".join(to_refresh) + "\n"
                + f"请手工恢复：git -C {wt} checkout -- .\n")
            sys.exit(2)

    return ("done", new_hash)


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
            print(f"skip {src_hash} (纯注释，剥离后为空)")
        else:
            picked.append((src_hash, new_hash))
            print(f"picked {new_hash}  <- {src_hash}")

    if picked:
        print("\n下一步跑 CleanBranch.py verify")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
