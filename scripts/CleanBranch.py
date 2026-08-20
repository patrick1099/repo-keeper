#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CleanBranch.py — 干净分支 <-> 工作分支 对账助手(探测 + 验证)

两条长期分支有**角色分工**:干净分支只放代码(对外/对客户),工作分支是超集
(代码 + 文档 + 工具)。它们的历史各自被 filter-branch 改写过,所以
**不能 merge**,只能 cherry-pick;也不能用 patch-id 求差,只能按内容比。

把确定性步骤压成一次调用,给出结论 + 待执行命令。
不做 cherry-pick / 文档迁移本身——那些要人现场判断(冲突、迁移方向、白名单例外)。

用法:
    py -3 CleanBranch.py detect     # 探测漂移、待 pick 的 commit、越界文档
    py -3 CleanBranch.py verify     # 三项不变量验证 (PASS/FAIL)
    py -3 CleanBranch.py --explain  # 每个配置值来自哪一层

分支 ref、代码路径、白名单等全部来自两层配置(见 local_config.py),脚本本身
不含任何仓库特有的常量。

退出码: 0 = 干净/通过, 1 = 有待办/失败, 2 = 脚本错误/配置缺失
"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import local_config  # noqa: E402
import cli_common as cc  # noqa: E402

# Windows 控制台默认 cp936，强制 UTF-8 输出避免中文乱码 / emoji 编码错误
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Configuration.
#
# These stay module-level names rather than attributes on a config object so
# the reconciliation logic below reads exactly as it did when they were
# literals -- this file was parameterised, not rewritten, and every branch of
# it was paid for with a bug in a real repo. They are populated by
# ``configure()``; tests assign them directly.
# ---------------------------------------------------------------------------
CLEAN_REF = None        # 干净分支:纯代码
WORK_REF = None         # 工作分支:超集
MAIN_REF = None         # 主分支:只读核对,绝不触碰

CODE_PATHS = []         # 固件代码路径(drift 以此为准)
DOC_GLOBS = []          # 越界文档探测模式
ALLOW = ()              # 合法的源码内文档,永不迁移/删除

# 两分支**刻意**长期不一致的文件 {路径: 为什么}。这些差异是设计意图、不是待办的
# drift,故不计入 verify 的 FAIL——否则红色天天亮、失去信息量。
#
# 注意这是**文件级**白名单:一旦某文件入列,它**将来的任何**差异也一并不报 FAIL。
# 因此 verify/detect 仍会把它们的 diff --stat 打出来(标为「预期漂移」),
# 便于人眼发现行数变化。新增条目前先确认该差异要永久存在,而不是「还没搬」。
EXPECTED_DRIFT = {}

# 干净分支上**永不搬到工作分支**的提交 {完整 hash: 理由}。EXPECTED_DRIFT 只让
# verify 不报 FAIL,挡不住 detect 把这些提交列成「待 cherry-pick」——照单执行会把
# 工作分支刻意保留的内容抹掉。此处显式拉黑。
NEVER_PICK = {}

SCAN = 60               # 匹配同步点时回看多少条

_REQUIRED = ("branches.clean", "branches.work", "paths.code")


def configure(cfg=None):
    """Pull the module-level settings out of the two config layers.

    Raises ``local_config.ConfigError`` when a key this tool cannot invent is
    missing. Guessing a branch ref would point the whole run at the wrong
    branch and still look like it worked.
    """
    global CLEAN_REF, WORK_REF, MAIN_REF, CODE_PATHS, DOC_GLOBS, ALLOW
    global EXPECTED_DRIFT, NEVER_PICK, SCAN

    cfg = cfg if cfg is not None else local_config.load()
    CLEAN_REF, WORK_REF, CODE_PATHS = cfg.require(*_REQUIRED)

    MAIN_REF = cfg.get("branches.main")
    DOC_GLOBS = cfg.get("paths.doc_globs", [])
    ALLOW = cfg.get("allow.docs_on_clean", [])
    EXPECTED_DRIFT = cfg.get("expected_drift", {})
    NEVER_PICK = cfg.get("never_pick", {})
    SCAN = cfg.get("scan.depth", SCAN)
    return cfg


# ---------------------------------------------------------------------------
# worktree 解析
# ---------------------------------------------------------------------------

def _resolve_worktrees():
    """从 `git worktree list` 运行时解析干净/工作分支的 worktree 路径。
    机器无关——不硬编码绝对路径(换机就失效)。需从本仓库任一 worktree 内运行。"""
    r = subprocess.run(
        ["git", "-C", os.getcwd(), "worktree", "list", "--porcelain"],
        capture_output=True, encoding="utf-8")
    if r.returncode != 0:
        raise cc.ExternalToolError(
            "E_EXTERNAL_TOOL",
            "需在本仓库的某个 worktree 内运行本脚本。\n" + (r.stderr or ""),
            details={"tool": "git", "exit_code": r.returncode,
                     "stderr_tail": (r.stderr or "").strip()[-2000:]})
    wt_by_branch, cur = {}, None
    for line in r.stdout.splitlines():
        if line.startswith("worktree "):
            cur = line[len("worktree "):]
        elif line.startswith("branch ") and cur:
            br = line[len("branch "):]
            if br.startswith("refs/heads/"):
                br = br[len("refs/heads/"):]
            wt_by_branch[br] = cur
    cw, ww = wt_by_branch.get(CLEAN_REF), wt_by_branch.get(WORK_REF)
    missing = [ref for ref, wt in ((CLEAN_REF, cw), (WORK_REF, ww)) if not wt]
    if missing:
        raise cc.CliError(
            "E_VALIDATION",
            "未在 worktree 列表找到分支: " + ", ".join(missing)
            + "\n已知分支: " + ", ".join(wt_by_branch),
            details={"state": "missing_worktree", "missing": missing,
                     "known_branches": sorted(wt_by_branch)},
            exit_code=cc.EXIT_ARG)
    return cw, ww


_WT_CACHE = None


def worktrees():
    """惰性解析并缓存 (clean_wt, work_wt)。模块 import 不再有副作用(便于测试)。"""
    global _WT_CACHE
    if _WT_CACHE is None:
        _WT_CACHE = _resolve_worktrees()
    return _WT_CACHE


def clean_wt():
    return worktrees()[0]


def work_wt():
    return worktrees()[1]


def git(wt, *args):
    """在指定 worktree 跑 git，关闭 quotepath 以拿到可读中文路径。"""
    cmd = ["git", "-C", wt, "-c", "core.quotepath=false", *args]
    r = subprocess.run(cmd, capture_output=True, encoding="utf-8")
    if r.returncode != 0:
        raise cc.ExternalToolError(
            "E_EXTERNAL_TOOL",
            f"git failed: {' '.join(args)}\n{r.stderr}",
            details={"tool": "git", "args": list(args),
                     "exit_code": r.returncode,
                     "stderr_tail": (r.stderr or "").strip()[-2000:]})
    return r.stdout.rstrip("\n")


def log_pairs(ref, n=None):
    """返回 [(hash, subject), ...]，新->旧。"""
    out = git(clean_wt(), "log", f"-{n or SCAN}", "--format=%h\x1f%s", ref)
    pairs = []
    for line in out.splitlines():
        if "\x1f" in line:
            h, s = line.split("\x1f", 1)
            pairs.append((h, s))
    return pairs


def find_sync_point():
    """
    histories 被独立 filter-branch 改写过 → 不能用 hash 求差。
    按 subject 匹配找同步点：从干净分支 新->旧，第一条 subject 在工作分支出现、
    且代码路径 tree 相等的 commit，即两边对齐处。
    返回 (clean_hash, work_hash) 或 (None, None)。
    """
    work = log_pairs(WORK_REF)
    work_by_subj = {}
    for h, s in work:
        work_by_subj.setdefault(s, h)  # 取该 subject 最新的工作分支 commit
    for ch, cs in log_pairs(CLEAN_REF):
        wh = work_by_subj.get(cs)
        if not wh:
            continue
        diff = git(clean_wt(), "diff", "--stat", ch, wh, "--", *CODE_PATHS)
        if diff == "":
            return ch, wh
    return None, None


def _blob(ref, path):
    """取 <ref>:<path> 的字节；不存在返回 None。两分支共享对象库，从 clean_wt 读即可。"""
    r = subprocess.run(["git", "-C", clean_wt(), "cat-file", "-p", f"{ref}:{path}"],
                       capture_output=True)
    return r.stdout if r.returncode == 0 else None


def path_aligned(path):
    """两分支在 path 上内容是否已一致（逐字节；单边增删一律判为不一致）。"""
    wb, cb = _blob(WORK_REF, path), _blob(CLEAN_REF, path)
    if wb is None or cb is None:      # 单边增删
        return False
    return wb == cb


def _real_drift_paths():
    """两分支间存在差异的代码路径。

    以前这里还要逐个 path_aligned() 复查一遍,因为「只差私人注释」不算 drift。
    那套已退役,比较就是纯字节比较 —— 而 diff 报出来的路径按定义就是字节不同,
    再查一遍恒为真,只是白跑 2N 个 git 进程。
    """
    names = git(clean_wt(), "diff", "--name-only", WORK_REF, CLEAN_REF,
                "--", *CODE_PATHS)
    return [n for n in names.splitlines() if n]


def _stat(paths):
    if not paths:
        return ""
    return git(clean_wt(), "diff", "--stat", WORK_REF, CLEAN_REF, "--", *paths)


def real_drift():
    """返回 (real_paths, stat_text)——所有真实差异，不区分是否刻意。"""
    real = _real_drift_paths()
    return real, _stat(real)


def commit_content_on_work(h):
    """该干净分支 commit 改动的代码路径，在两分支上是否已全部内容一致。

    为真 → 把它 cherry-pick 到工作分支只会得到一个空提交（内容早已在那边，
    只是当初以别的 subject / 别的提交落地）。detect 必须剔除它，否则照着
    打印的命令跑会造出重复提交。

    比 subject 匹配更可靠：subject 可以改写，内容不会骗人。
    """
    r = subprocess.run(["git", "-C", clean_wt(), "rev-parse", "--verify", "-q",
                        h + "^"], capture_output=True, encoding="utf-8")
    if r.returncode != 0:          # 根提交，无父，无从判断
        return False
    names = git(clean_wt(), "diff", "--name-only", h + "^", h, "--", *CODE_PATHS)
    paths = [n for n in names.splitlines() if n]
    if not paths:                  # 没碰代码路径 → 不是「已同步」，交人判断
        return False
    return all(path_aligned(p) for p in paths)


def _never_pick(h):
    """h 是缩写 hash；命中 NEVER_PICK 则返回理由，否则 None。"""
    for full, reason in NEVER_PICK.items():
        if full.startswith(h):
            return reason
    return None


def _drift_reason(path):
    """EXPECTED_DRIFT 的理由。集合形式（测试里常见）没有理由可给。"""
    if isinstance(EXPECTED_DRIFT, dict):
        return EXPECTED_DRIFT.get(path)
    return None


def classify_drift():
    """按 EXPECTED_DRIFT 切分真实差异。

    返回 (expected, unexpected, expected_stat, unexpected_stat)。
    只有 unexpected 非空才该让 verify 失败；expected 仍打印出来供人眼核对。
    """
    real = _real_drift_paths()
    expected = [p for p in real if p in EXPECTED_DRIFT]
    unexpected = [p for p in real if p not in EXPECTED_DRIFT]
    return expected, unexpected, _stat(expected), _stat(unexpected)


def stray_docs():
    """干净分支上的越界文档：tracked(去白名单) + untracked 散落。"""
    if not DOC_GLOBS:
        return [], []
    tracked = git(clean_wt(), "ls-files", "--", *DOC_GLOBS).splitlines()
    tracked = [f for f in tracked if f and f not in ALLOW]
    untracked = []
    for line in git(clean_wt(), "status", "--porcelain").splitlines():
        if line.startswith("??"):
            untracked.append(line[3:])
    return tracked, untracked


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------

def detect():
    print(f"=== DETECT: {CLEAN_REF} -> {WORK_REF} 对账 ===\n")
    expected, unexpected, exp_stat, unexp_stat = classify_drift()

    ch, wh = find_sync_point()
    ahead = []
    if ch:
        out = git(clean_wt(), "log", "--format=%h\x1f%s", f"{ch}..{CLEAN_REF}")
        ahead = [ln.split("\x1f", 1) for ln in out.splitlines() if "\x1f" in ln]

    # 同步点被"永久性刻意漂移"（如工作分支本地保留的 .clangd 改动使 tree 永不相等）
    # 卡住时，ch..CLEAN_REF 会含实际已同步过去的提交 → 必须剔除，否则照打印的命令跑
    # 会造重复/空提交。两道筛子：
    #   (1) subject 已在工作分支 —— 便宜，但 subject 可被改写；
    #   (2) 改动的代码路径两边内容已一致 —— 贵一点，但内容不会骗人。
    #       专治「同一内容以不同 subject 分别落地在两边」（此时 subject 筛子失效）。
    skipped_synced = 0
    blacklisted = []
    if ahead:
        blacklisted = [(h, s) for h, s in ahead if _never_pick(h)]
        ahead = [(h, s) for h, s in ahead if not _never_pick(h)]
    if ahead:
        work_subjects = {s for _, s in log_pairs(WORK_REF)}
        before = len(ahead)
        ahead = [(h, s) for h, s in ahead
                 if s not in work_subjects and not commit_content_on_work(h)]
        skipped_synced = before - len(ahead)

    if ch:
        print(f"[同步点] 干净分支 {ch}  ==  工作分支 {wh}")
    else:
        print("[同步点] 未找到（subject 无匹配，需人工核对）")

    if ahead:
        print(f"\n[待 cherry-pick 到工作分支] {len(ahead)} 条 (旧->新执行):")
        for h, s in reversed(ahead):  # 旧->新
            print(f"    {h}  {s}")
        hashes = " ".join(h for h, _ in reversed(ahead))
        print("\n  命令(从工作分支 worktree 跑, 逐条按需解冲突):")
        print(f'    git -C "{work_wt()}" cherry-pick {hashes}')
    else:
        note = (f"（{skipped_synced} 条内容已在工作分支，判为已同步）"
                if skipped_synced else "")
        print("\n[待 cherry-pick 到工作分支] 无" + note)

    if skipped_synced and ahead:
        print(f"  (另有 {skipped_synced} 条内容已在工作分支，已剔除)")

    if blacklisted:
        print("\n[永不搬运] never_pick 拉黑，刻意不同步（勿手动 cherry-pick）:")
        for h, s in blacklisted:
            print(f"    {h}  {s}")
            print(f"      理由: {_never_pick(h)}")

    if unexpected:
        print("\n[代码 drift] 待处理:\n" + unexp_stat)
    else:
        print("\n[代码 drift] 干净 ✅")
    if expected:
        print(f"  预期漂移 {len(expected)} 项（expected_drift 白名单）:\n" + exp_stat)
        _print_drift_reasons(expected)

    tracked, untracked = stray_docs()
    if tracked or untracked:
        print("\n[越界文档] 发现（需迁移到工作分支后从干净分支清除）:")
        for f in tracked:
            print(f"    tracked   {f}")
        for f in untracked:
            print(f"    untracked {f}")
    else:
        print("\n[越界文档] 无（仅白名单内文件）✅")

    return 1 if (ahead or unexpected or tracked or untracked) else 0


def _print_drift_reasons(paths):
    """把「为什么永久不一致」印出来。

    白名单是文件级的:一旦入列,该文件将来的任何差异也不再报 FAIL。理由是唯一
    能让下一个人判断「这条还成立吗」的东西,不打印等于没有。
    """
    reasons = [(p, _drift_reason(p)) for p in paths]
    if not any(r for _, r in reasons):
        return
    for path, reason in reasons:
        if reason:
            print(f"    {path}")
            print(f"      理由: {reason}")


def verify():
    print("=== VERIFY: 不变量检查 ===\n")
    ok = True

    expected, unexpected, exp_stat, unexp_stat = classify_drift()
    if not unexpected:
        print("1. 代码 drift ............ PASS ✅ (两分支固件一致)")
    else:
        ok = False
        print("1. 代码 drift ............ FAIL ❌ (已排除纯注释差异)\n" + unexp_stat)
    if expected:
        print(f"   └ 预期漂移 {len(expected)} 项（在 expected_drift 白名单，不计 FAIL；"
              "行数有变请核对）:\n" + exp_stat)
        _print_drift_reasons(expected)

    tracked, untracked = stray_docs()
    if not tracked and not untracked:
        print("2. 干净分支越界文档 ...... PASS ✅ (仅白名单)")
    else:
        ok = False
        print("2. 干净分支越界文档 ...... FAIL ❌ " + str(tracked + untracked))

    if MAIN_REF:
        main_line = git(clean_wt(), "log", "--oneline", "-1", MAIN_REF)
        print(f"3. {MAIN_REF} HEAD ............. {main_line}  (人工核对未变)")
    else:
        print("3. 主分支 ................ 跳过（未配置 branches.main）")

    print("\n=> " + ("ALL PASS ✅" if ok else "有 FAIL ❌"))
    return 0 if ok else 1


def build_parser():
    parser = cc.CliFriendlyParser(
        prog="CleanBranch",
        description="干净分支 <-> 工作分支 对账:探测漂移与待搬运的提交,验证三项不变量。"
                    "只报告,不动手。")
    parser.add_argument("action", nargs="?", choices=["detect", "verify"],
                        help="detect=探测待办, verify=不变量 PASS/FAIL")
    local_config.add_explain_flag(parser)
    parser.add_argument("--json", action="store_true",
                        help="以 JSON 信封输出(与 --format json 等价)")
    parser.add_argument("--format", choices=("json",), default="json",
                        help="输出格式:仅支持 json(与 --json 等价)")
    parser.add_argument("--ai-help", action="store_true",
                        help="输出 AI 优化的使用说明并退出")
    return parser


def command(argv, context):
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.explain:
        try:
            cfg = configure()
        except local_config.ConfigError as exc:
            return cc.fail("E_VALIDATION", str(exc), exit_code=cc.EXIT_ARG)
        if context.json_mode:
            return cc.ok(local_config.explain_data(cfg))
        context.sinks.out.write(cfg.explain() + "\n")
        return cc.ok()

    if args.action is None:
        if context.json_mode:
            return cc.fail("E_VALIDATION",
                           "需要指定 action: detect 或 verify，或 --explain",
                           exit_code=cc.EXIT_ARG)
        parser.print_help()
        return cc.fail("E_VALIDATION", "", exit_code=cc.EXIT_ARG)

    try:
        cfg = configure()
    except local_config.ConfigError as exc:
        return cc.fail("E_VALIDATION", str(exc), exit_code=cc.EXIT_ARG)

    if args.action == "detect":
        rc = detect()
        return cc.ok({"action": "detect", "pending": rc != 0})
    if args.action == "verify":
        rc = verify()
        if rc == 0:
            return cc.ok({"action": "verify", "passed": True})
        return cc.fail("E_VERIFICATION_FAILED",
                       "CleanBranch verify 失败:干净分支不变量未通过",
                       details={"passed": False},
                       suggestion=cc.SUGGESTIONS.get("E_VERIFICATION_FAILED"),
                       exit_code=cc.EXIT_FAIL)


AI_HELP = """---
name: CleanBranch
description: >
  Reconcile the clean branch (code only, customer-facing) with the work branch
  (superset: code + docs + tooling) whose histories were rewritten independently
  and cannot be merged. Detect drift / commits to cherry-pick / stray docs, or
  verify three invariants. Report only, never mutates. Use when the user asks
  to reconcile clean vs work branches, check whether the clean branch is clean,
  or list commits to carry from one branch to the other.
ai_help_version: 0.1.0
---

# CleanBranch AI Help Guide

## Quick Reference

- **Detect pending work:** `CleanBranch.py detect --json`
- **Verify invariants:** `CleanBranch.py verify --json`
- **Inspect config provenance:** `CleanBranch.py --explain`

## When to Use

Use this tool when the user asks to:
- reconcile two long-lived branches (clean vs work) that cannot be merged
- list commits on the clean branch that are still pending on the work branch
- verify the clean branch keeps only code (no stray docs, no drift)

Do NOT use for:
- actually cherry-picking (use PickToClean); this tool only reports

## Command Reference

- `detect`: report drift, commits pending on the work branch, stray docs
- `verify`: check three invariants (code drift / stray docs / main HEAD)
- `--explain`: print which layer each config value came from, then exit
- `--json`: machine envelope output (equivalent to `--format json`)

## Input / Output

- `--json` success: `{ok:true, data:{action, pending?|passed?}, error:null, meta:{log}}`
- `--json` failure: envelope on stderr, stdout empty; `error.code` from the table below
- human mode: the existing report on stdout, errors on stderr

## Side Effects & Safety

- Report only; never writes or mutates anything.

## Exit Codes

| code | meaning |
|---|---|
| 0 | success (detect clean / verify pass) |
| 1 | runtime failure (see error.code) |
| 2 | parameter / usage error (E_VALIDATION) |

## Errors & Recovery

| code | meaning | recovery |
|---|---|---|
| `E_VALIDATION` | missing clean/work worktree or bad argument | check branches exist as worktrees / fix arguments |
| `E_EXTERNAL_TOOL` | git failed | check git is installed and callable |
| `E_VERIFICATION_FAILED` | one of the invariants FAILed | inspect error.details for the failed acceptance |
| `E_INTERNAL` | unexpected bug | report it |
"""


def main(argv=None, sinks=None):
    return cc.main(argv, sinks, command=command, parser_factory=build_parser,
                   ai_help=AI_HELP, prog="CleanBranch")


if __name__ == "__main__":
    sys.exit(main())
