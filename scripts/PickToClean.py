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
import cli_common as cc  # noqa: E402

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
    """跑 git 且必须成功；成功返回 stdout（去尾换行），失败抛 ExternalToolError。"""
    r = _run(wt, *args)
    if r.returncode != 0:
        raise cc.ExternalToolError(
            "E_EXTERNAL_TOOL",
            "git 失败: " + " ".join(args) + "\n" + (r.stderr or ""),
            details={"tool": "git", "args": list(args),
                     "exit_code": r.returncode,
                     "stderr_tail": (r.stderr or "").strip()[-2000:]})
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


def _conflict_guidance_text(wt, src_hash):
    files = " ".join(_conflict_files(wt)) or "<冲突文件>"
    return (
        f"cherry-pick {src_hash} 冲突，现场已保留。\n"
        f"  1) 在干净分支 worktree 手工解冲突（禁止 -X theirs/ours）\n"
        f"  2) git -C {wt} add -A -- {files}\n"
        f"  3) git -C {wt} cherry-pick --continue\n"
        f"  4) git -C {wt} commit --amend --reset-author --no-edit   # 作者归一\n"
        f"  5) 重跑本脚本处理剩余 commit\n"
    )


def _cherry_pick_in_progress(wt):
    r = _run(wt, "rev-parse", "--verify", "-q", "CHERRY_PICK_HEAD")
    return r.returncode == 0


def _check_identity(wt, cfg):
    """确认这个 worktree 会用干净分支该用的身份签名。

    没在配置里声明身份就没什么可核对的,跳过 —— 不能因为「没配」拦住所有人。
    不匹配抛 E_VALIDATION（前置条件不满足），由 cc.main 统一映射 rc2。
    """
    want = cfg.get("identity.clean")
    if not isinstance(want, dict):
        return
    name = _run(wt, "config", "user.name").stdout.strip()
    email = _run(wt, "config", "user.email").stdout.strip()
    if name == want.get("name") and email == want.get("email"):
        return
    raise cc.CliError(
        "E_VALIDATION",
        "干净分支 worktree 的提交身份与配置不符,拒绝搬运:\n"
        f"  worktree 现在会签: {name or '(未设置)'} <{email or '(未设置)'}>\n"
        f"  配置要求的是:      {want.get('name')} <{want.get('email')}>\n"
        "改配置,或者把 worktree 配对:\n"
        f'  git -C {wt} config --worktree user.name "{want.get("name")}"\n'
        f'  git -C {wt} config --worktree user.email "{want.get("email")}"\n'
        "(--worktree 需要仓库层 extensions.worktreeConfig=true)",
        details={"state": "identity_mismatch", "worktree": wt,
                 "actual": {"name": name, "email": email},
                 "expected": {"name": want.get("name"),
                              "email": want.get("email")}},
        exit_code=cc.EXIT_ARG)


def _preflight(wt, commits, cfg):
    """全部前置检查，任何一条不过就在动手前抛 E_VALIDATION；通过返回 0。"""
    # a. 无进行中的 cherry-pick。
    #    必须排在「工作区干净」前面:一场没收拾的冲突同时让两条都不满足,而先报
    #    「先清理工作区」是把人往错路上指 —— 清理解决不了它,要的是 --abort/--quit。
    if _cherry_pick_in_progress(wt):
        raise cc.CliError(
            "E_VALIDATION",
            "检测到进行中的 cherry-pick（CHERRY_PICK_HEAD 存在），"
            "请先 git cherry-pick --quit/--abort 后再运行。",
            details={"state": "cherry_pick_in_progress", "worktree": wt},
            exit_code=cc.EXIT_ARG)
    # b. 工作区必须干净
    status = _out(wt, "status", "--porcelain")
    if status.strip():
        raise cc.CliError(
            "E_VALIDATION",
            "先清理干净分支 worktree（git status 非空）:\n" + status,
            details={"state": "dirty_worktree", "worktree": wt,
                     "status": status.strip()},
            exit_code=cc.EXIT_ARG)
    # c. 提交身份必须是干净分支该用的那个
    _check_identity(wt, cfg)
    # d. 每个 commit 都必须能解析（doc 守卫要跑 git show，须先确保 hash 有效）
    for h in commits:
        r = _run(wt, "rev-parse", "--verify", "-q", h + "^{commit}")
        if r.returncode != 0:
            raise cc.CliError(
                "E_VALIDATION", f"无法解析 commit: {h}",
                details={"state": "unresolvable_commit", "src": h,
                         "worktree": wt},
                exit_code=cc.EXIT_ARG)
    # e. 文档守卫：所有 commit 一起预检，任一命中就在动手前退出（列出越界文件）
    offending = []
    for h in commits:
        names = _out(wt, "show", "--name-only", "--pretty=format:", h)
        for p in names.splitlines():
            p = p.strip()
            if p and _is_doc(p) and p not in cb.ALLOW:
                offending.append(f"{h}: {p}")
    if offending:
        raise cc.CliError(
            "E_VALIDATION",
            "拒绝：以下 commit 含文档改动（工作分支→干净分支不搬文档）:\n    "
            + "\n    ".join(offending),
            details={"state": "doc_guard", "offending": offending,
                     "worktree": wt},
            exit_code=cc.EXIT_ARG)
    return 0


def _would_cherry_pick_be_empty(wt, src_hash):
    """只读判定：真实 cherry-pick 这条 commit 会不会是空提交。

    dry-run 与真实路径共用同一判据 —— 内容已全部在干净分支 HEAD 上时,
    cherry-pick 产出的树与 HEAD 相同,即空提交。全程只跑 git diff,不动工作区。
    """
    r = _run(wt, "rev-parse", "--verify", "-q", src_hash + "^")
    if r.returncode != 0:          # 根提交，无父，无从判定
        return False
    names = _out(wt, "diff", "--name-only", src_hash + "^", src_hash)
    paths = [p for p in names.splitlines() if p]
    if not paths:                  # 没改任何文件 -> 一定是空提交
        return True
    q = _run(wt, "diff", "--quiet", "HEAD", src_hash, "--", *paths)
    return q.returncode == 0


def _pick_one(wt, src_hash, dry_run=False):
    """搬一条 commit。返回:
        ("done", new_short_hash) — 已落到干净分支
        ("skip", None)          — 内容与干净分支已一致，pick 出来是空提交
        ("conflict", None)      — 冲突，现场已保留（调用方 exit 1）
        ("would_pick", None)    — dry-run：会 pick（未落盘）
    """
    if dry_run:
        if _would_cherry_pick_be_empty(wt, src_hash):
            return ("skip", None)
        return ("would_pick", None)
    # --keep-redundant-commits 不给:内容已在对面时宁可跳过,也不要在对外分支的
    # 记录里留一条什么都没改的提交。
    r = _run(wt, "cherry-pick", src_hash)
    if r.returncode != 0:
        combined = (r.stdout or "") + (r.stderr or "")
        if "empty" in combined or "nothing to commit" in combined:
            _run(wt, "cherry-pick", "--skip")
            return ("skip", None)
        return ("conflict", None)

    # 作者归一到本 worktree 的身份（已在 _preflight 核对过与 identity.clean 一致）。
    # 单独 amend 而不是 pick 时带上:cherry-pick 没有 --reset-author。
    _out(wt, "commit", "--amend", "--reset-author", "--no-edit")
    return ("done", _out(wt, "rev-parse", "--short", "HEAD"))


def build_parser():
    parser = cc.CliFriendlyParser(
        prog="PickToClean",
        description="LLMs/agents: run 'PickToClean --ai-help' for usage guidance. "
                    "把工作分支的 commit 搬到干净分支，先过两道闸（文档守卫 + 身份核对）。")
    parser.add_argument("commits", nargs="*", metavar="COMMIT",
                        help="要搬运的 commit hash（旧->新 顺序）")
    parser.add_argument("--dry-run", action="store_true",
                        help="预演：判定每个 commit 会 pick 还是 skip，不落盘")
    parser.add_argument("--json", action="store_true",
                        help="以 JSON 信封输出(与 --format json 等价)")
    parser.add_argument("--format", choices=("json",), default="json",
                        help="输出格式:仅支持 json(与 --json 等价)")
    parser.add_argument("--ai-help", action="store_true",
                        help="输出 AI 优化的使用说明并退出")
    return parser


def command(argv, context, cfg=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.commits:
        if context.json_mode:
            return cc.fail("E_VALIDATION", "缺少要搬运的 commit",
                           exit_code=cc.EXIT_ARG)
        context.sinks.err.write(__doc__)
        return cc.fail("E_VALIDATION", "缺少要搬运的 commit",
                       exit_code=cc.EXIT_ARG)

    if cfg is None:
        try:
            cfg = cb.configure()
        except local_config.ConfigError as exc:
            return cc.fail("E_VALIDATION", str(exc), exit_code=cc.EXIT_ARG)

    wt = cb.clean_wt()   # 惰性解析（测试通过 CleanBranch._WT_CACHE 注入 fixture）

    _preflight(wt, args.commits, cfg)

    picked, skipped = [], []
    for src_hash in args.commits:      # 旧->新
        kind, new_hash = _pick_one(wt, src_hash, dry_run=args.dry_run)
        if kind == "conflict":
            guidance = _conflict_guidance_text(wt, src_hash)
            if context.json_mode:
                return cc.fail(
                    "E_EXTERNAL_TOOL", "cherry-pick 冲突，现场已保留",
                    details={"tool": "git", "state": "conflict",
                             "worktree": wt, "src": src_hash,
                             "conflict_files": _conflict_files(wt),
                             "guidance": guidance},
                    exit_code=cc.EXIT_FAIL)
            context.sinks.out.flush()
            context.sinks.err.write(guidance)
            return cc.fail("E_EXTERNAL_TOOL", "cherry-pick 冲突，现场已保留",
                           exit_code=cc.EXIT_FAIL)
        if kind == "skip":
            skipped.append({"src": src_hash})
            print(f"skip {src_hash} (内容已在干净分支，pick 出来是空提交)"
                  + (" (dry-run,未落盘)" if args.dry_run else ""))
        elif kind == "would_pick":
            picked.append({"src": src_hash, "hash": None})
            print(f"would pick {src_hash} (dry-run,未落盘)")
        else:
            picked.append({"src": src_hash, "hash": new_hash})
            print(f"picked {new_hash}  <- {src_hash}")

    if picked and not args.dry_run:
        print("\n下一步跑 CleanBranch.py verify")

    data = {"action": "pick", "worktree": wt,
            "picked": picked, "skipped": skipped}
    if args.dry_run:
        data["dry_run"] = True
    return cc.ok(data)


AI_HELP = """---
name: PickToClean
description: >
  Cherry-pick commits from the work branch onto the clean branch, gated by two
  guards before anything is touched: a doc guard (no docs may travel to the
  clean branch) and an identity check (the worktree must sign with the
  configured clean-branch identity). Conflicts are left in place for manual
  resolution. Use when the user asks to move commits to the clean branch,
  apply a commit to the clean side, or preview what a pick would do.
ai_help_version: 0.1.0
---

# PickToClean AI Help Guide

## Quick Reference

- **Pick commits:** `PickToClean.py <commit> [<commit> ...]`
- **Preview without writing:** `PickToClean.py <commit> ... --dry-run`
- **Machine output:** `PickToClean.py <commit> ... --json`

## When to Use

Use this tool when the user asks to:
- carry commits from the work branch onto the clean branch
- apply a commit to the clean side (it cherry-picks + resets the author)
- preview which commits would land and which would be skipped

Do NOT use for:
- reporting drift (use CleanBranch detect/verify)
- committing docs to the clean branch (the doc guard refuses)

## Command Reference

- `COMMIT...`: hashes to move, oldest -> newest
- `--dry-run`: decide pick vs skip for each commit without mutating anything
- `--json`: machine envelope output (equivalent to `--format json`)

## Input / Output

- `--json` success: `{ok:true, data:{action, worktree, picked, skipped, dry_run?}, error:null, meta:{log}}`
- `--json` failure: envelope on stderr, stdout empty; `error.code` from the table below
- human mode: per-commit progress on stdout, errors / conflict guidance on stderr

## Side Effects & Safety

- Real mode mutates the clean-branch worktree: `git cherry-pick` then
  `git commit --amend --reset-author --no-edit`.
- `--dry-run` never mutates: it decides via read-only `git diff` whether each
  commit would produce a non-empty pick, so nothing lands (data carries
  `dry_run: true`).
- Guards run before any mutation: in-progress cherry-pick / dirty worktree /
  identity mismatch / unresolvable hash / doc-guard violations all refuse up front.
- On conflict the working tree is left in place for manual resolution, and
  remaining commits are not processed.

## Exit Codes

| code | meaning |
|---|---|
| 0 | success |
| 1 | runtime failure (conflict / git failure) |
| 2 | parameter / usage / preflight error (E_VALIDATION) |

## Errors & Recovery

| code | meaning | recovery |
|---|---|---|
| `E_VALIDATION` | missing commits / preflight guard / config | fix per the message |
| `E_EXTERNAL_TOOL` | git failed or cherry-pick conflict | resolve conflict in the clean worktree, then re-run |
| `E_INTERNAL` | unexpected bug | report it |
"""


def main(argv=None, cfg=None, sinks=None):
    return cc.main(argv, sinks,
                   command=lambda a, ctx: command(a, ctx, cfg),
                   parser_factory=build_parser, ai_help=AI_HELP,
                   prog="PickToClean")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
