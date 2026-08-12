#!/usr/bin/env python3
"""One command that sets a repo up, so you only have to remember one name.

The four skills in this plugin are stages of one job, and doing them in the
wrong order wastes a round: generating clangd config before the ignore rules
means the generated ``.clangd`` shows up as an untracked change, and touching
either before you have left the shared branch means doing it in the wrong
checkout. Chaining them is mechanical, so it lives in code here rather than as
a paragraph in a SKILL.md asking an agent to remember three more skill names --
an instruction can be skipped and nothing says so.

``init`` performs only actions that are **per-clone and reversible**:

    generate the two config templates   (new files, outside git's view)
    ignore rules -> .git/info/exclude   (never .gitignore; --shared is not passed)
    freeze IDE state -> skip-worktree   (undo: RepoHygiene.py --unfreeze)
    clangd config                       (generated files, already ignored)

Everything that needs a human stays a report: where to put a worktree, whether
a committed ``.bin`` is a release artifact or residue, and the whole
clean-branch job (conflicts and migration direction are judgement, not steps).

Exit codes: 0 = 就绪, 1 = 有需要你决定的事, 2 = 出错。
"""
import argparse
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import local_config  # noqa: E402
import Proj2Clangd  # noqa: E402
import RepoHygiene  # noqa: E402
from toolname import (GLOBAL_DIR_NAME, PROJECT_CONFIG_NAME,  # noqa: E402
                      REANCHOR_EXE, TOOL_NAME)

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

DEFAULT_PROTECTED = ["main", "master"]


# ---------------------------------------------------------------------------
# templates
# ---------------------------------------------------------------------------

GLOBAL_TEMPLATE = '''\
# {tool} 全局配置 —— 跨项目复用的那一半。
#
# 这个文件在你的家目录里,不在任何仓库中,所以不需要 ignore 规则,也不会被同事看到。
# 跟具体项目有关的东西(分支 ref、代码路径、白名单)写进项目层:
#     <仓库根>/{project_file}
# 项目层覆盖这里。想知道某个值最后来自哪一层,跑任意脚本加 --explain。
#
# 合并规则:表逐键深合并、数组整体替换、标量项目优先。

[branches]
# 受保护分支:不在上面直接干活,init 会建议你开 worktree
protected = ["main", "master"]
# 主分支:同步时只读核对,绝不触碰
main = "main"

[paths]
# 越界文档的探测模式 —— 跟项目无关,所以放全局
doc_globs = ["*.md", "*.csv", "*.txt", "*.docx", "docs/"]

[scan]
# 找同步点时回看多少条提交
depth = 60

# 身份跟着**分支纯度**走,不跟着仓库走:纯代码/对外分支签一个身份,
# 超集/工具分支签另一个。配了之后 PickToClean.py 会在搬运前核对 worktree
# 的 git config 对不对得上,对不上就停下 —— 免得对外提交悄悄签错人。
# [identity]
# clean = {{ name = "...", email = "..." }}
# work  = {{ name = "...", email = "..." }}
'''

PROJECT_TEMPLATE = '''\
# {tool} 项目配置 —— 只写**这个仓库特有**的东西。
#
# 其余继承 ~/{global_dir}/defaults.toml;想知道某个值来自哪一层,加 --explain。
# 本文件不进仓库:ignore 规则写在 .git/info/exclude 里(不是 .gitignore ——
# 那是从工作分支拉下来的共享文件,改它会打扰同事)。
#
# 下面这三项是 clean-branch 必填的,工具不猜:猜错的分支 ref 会让整轮跑在错的
# 分支上,而且看起来一切正常。只用 repo-hygiene / clangd-config 的话可以先空着。

# [branches]
# clean = "customer/xxx"        # 干净分支:纯代码,对外/对客户
# work  = "task/xxx"            # 工作分支:代码 + 文档 + 工具(超集)

# [paths]
# code = ["App", "Boot", "drv", "bsp"]   # drift 以这些路径为准

# [allow]
# 合法的源码内文档,永不迁移/删除
# docs_on_clean = ["App/Code/xxx/说明.txt"]

# [expected_drift]
# 两分支**刻意**永久不一致的文件。键=路径,值=为什么(理由必填 —— 这份名单是
# 文件级的,一旦入列,该文件将来的任何差异也不再报警,理由是下一个人唯一能据以
# 判断「这条还成立吗」的东西)。
# "App/Code/drv/x.c" = "某次决策:work 保留重构,clean 已整体回退"

# [never_pick]
# 永不从 clean 搬到 work 的提交。expected_drift 只让 verify 闭嘴,挡不住 detect
# 把它列成「待 cherry-pick」——照单执行会抹掉刻意保留的内容。两张表都要填。
# "<40 位 hash>" = "为什么这条永远不搬"
'''


# ---------------------------------------------------------------------------
# git helpers
# ---------------------------------------------------------------------------

def git(args, cwd, check=False):
    r = subprocess.run(["git", "-C", str(cwd), *args],
                       capture_output=True, encoding="utf-8")
    if check and r.returncode != 0:
        raise RuntimeError(r.stderr.strip())
    return r.stdout.strip() if r.returncode == 0 else None


def repo_root(path):
    return git(["rev-parse", "--show-toplevel"], path)


def current_branch(path):
    """Branch name, or None when detached."""
    name = git(["symbolic-ref", "--quiet", "--short", "HEAD"], path)
    return name or None


def main_worktree_root(path):
    common = git(["rev-parse", "--path-format=absolute", "--git-common-dir"], path)
    return str(Path(common).parent) if common else None


def worktree_list(path):
    out = git(["worktree", "list", "--porcelain"], path) or ""
    entries, cur = [], None
    for line in out.splitlines():
        if line.startswith("worktree "):
            cur = line[len("worktree "):]
        elif line.startswith("branch ") and cur:
            br = line[len("branch "):]
            entries.append((br.removeprefix("refs/heads/"), cur))
    return entries


# ---------------------------------------------------------------------------
# steps
# ---------------------------------------------------------------------------

class Report:
    def __init__(self):
        self.done = []
        self.todo = []

    def ok(self, text):
        self.done.append(text)
        print("  [ok]   " + text)

    def ask(self, text):
        self.todo.append(text)
        print("  [你来] " + text)

    def note(self, text):
        print("         " + text)


def write_if_absent(path, text, report, label, dry_run):
    path = Path(path)
    if path.is_file():
        report.ok("{0} 已存在,不动它: {1}".format(label, path))
        return False
    if dry_run:
        report.ok("[dry-run] 会生成 {0}: {1}".format(label, path))
        return True
    path.parent.mkdir(parents=True, exist_ok=True)
    # write_bytes with an explicit encoding: the default text path uses the
    # locale encoding (cp936 on this machine) and TOML must be UTF-8, so a
    # template with Chinese comments would come back unparseable.
    path.write_bytes(text.encode("utf-8"))
    report.ok("生成了 {0}: {1}".format(label, path))
    return True


def step_branch(root, cfg, report, worktree_path):
    """在受保护分支上不干活 —— 这是整个插件的前提。"""
    branch = current_branch(root)
    protected = cfg.get("branches.protected", DEFAULT_PROTECTED)
    if branch is None:
        report.ask("当前是 detached HEAD,先切到一条分支再来。")
        return 1
    if branch not in protected:
        report.ok("当前分支 {0}(不在受保护列表里),就在这儿干活。".format(branch))
        return 0

    if worktree_path:
        # Already being resolved this run -- calling it a pending decision
        # would leave the summary asking for something we are about to do.
        report.ok("当前在受保护分支 {0},按 --worktree 另开检出。".format(branch))
        return 0

    # Where a worktree goes is a real choice (inside the repo? beside it? on
    # another drive?) and guessing it litters someone's disk.
    report.ask("当前在受保护分支 {0} 上。".format(branch))
    report.note("在共享分支上直接改,是这套工具存在的原因之一 —— 先开一个 worktree。")
    report.note("放哪儿我不猜。想好位置后:")
    report.note('  py -3 Keeper.py init --worktree "<路径>" --branch "<新分支名>"')
    return 1


def step_make_worktree(root, report, worktree_path, branch_name, dry_run):
    existing = dict((b, p) for b, p in worktree_list(root))
    if branch_name and branch_name in existing:
        report.ok("分支 {0} 已有 worktree: {1}".format(
            branch_name, existing[branch_name]))
        return existing[branch_name], 0
    if Path(worktree_path).exists() and any(Path(worktree_path).iterdir()):
        report.ask("{0} 已存在且非空,不覆盖。换个路径。".format(worktree_path))
        return None, 1
    if dry_run:
        report.ok("[dry-run] 会建 worktree {0} (分支 {1})".format(
            worktree_path, branch_name))
        return worktree_path, 0
    args = ["worktree", "add"]
    if branch_name:
        args += ["-b", branch_name]
    args.append(str(worktree_path))
    if git(args, root) is None:
        report.ask("建 worktree 失败,自己跑一遍看报什么: git worktree add ...")
        return None, 1
    report.ok("建好 worktree: {0}".format(worktree_path))
    return worktree_path, 0


def step_configs(root, report, dry_run):
    gp = local_config.global_config_path()
    write_if_absent(gp, GLOBAL_TEMPLATE.format(
        tool=TOOL_NAME, project_file=PROJECT_CONFIG_NAME),
        report, "全局配置", dry_run)

    # The project layer belongs at the top of the MAIN checkout, so one file
    # serves every linked worktree instead of drifting into per-worktree copies.
    target = Path(main_worktree_root(root) or root) / PROJECT_CONFIG_NAME
    write_if_absent(target, PROJECT_TEMPLATE.format(
        tool=TOOL_NAME, global_dir=GLOBAL_DIR_NAME),
        report, "项目配置", dry_run)


def step_hygiene(root, report, dry_run, apply_writes):
    print("")
    argv = ["-p", str(root)]
    if apply_writes:
        argv.append("--apply")       # 注意:不带 --shared,共享文件一个字节不动
    if dry_run:
        argv.append("--dry-run")
    rc = RepoHygiene.main(argv)
    if rc:
        report.ask("repo-hygiene 没跑完(退出码 {0}),看上面的输出。".format(rc))
        return 1
    report.ok("repo-hygiene 完成(全本地:.git/info/ 与 .git/index,仓库文件零改动)")
    report.note("报告里的 [4] 只有你能判断 —— 逐条看一眼,别让我替你决定。")
    return 0


#: Backends where several project files in one repo mean several independent
#: databases, each belonging beside its own project file. CMake is absent on
#: purpose: its many nested CMakeLists.txt describe one build tree, not many.
_MULTI_PROJECT_KINDS = {"keil", "iar"}


def _clangd_projects(found):
    """The project files init should configure, one entry per database.

    A repo with an App and a Boot .uvprojx needs two databases in two
    directories -- they cannot share one, and a single .clangd above both would
    apply App's macros to Boot's sources. Handing the backend the repo root
    instead only ever produced one of them, and the run before this fix put that
    one in whatever directory the caller happened to stand in.
    """
    projects = []
    for kind, label, matches in found:
        if kind in _MULTI_PROJECT_KINDS:
            projects.extend((kind, label, m) for m in matches)
        else:
            projects.append((kind, label, None))
    return projects


def _report_reanchor_exe(root, report, projects):
    """Say whether the re-anchor exe made it into the repo root.

    Only the Keil backend deploys it (the exe understands Keil layouts and
    nothing else), and it used to do so with a single line of stdout that said
    "skipped" when the exe had never been built -- which is the state of every
    fresh clone of the plugin. init then reported a clean run over a repo that
    had no way to repair its own clangd config after being moved. If it is not
    there, that belongs in the summary, not in the scrollback.
    """
    if not any(kind == "keil" for kind, _, _ in projects):
        return
    exe = Path(root) / REANCHOR_EXE
    if exe.is_file():
        report.ok("换路径自修的 {0} 已在仓库根: {1}".format(REANCHOR_EXE, exe))
        return
    report.ask("{0} 没能放到仓库根 —— 换机器/换路径后没人能修 clangd 配置。"
               "看上面这一步的输出是哪一步卡住了。".format(REANCHOR_EXE))


def step_clangd(root, report, dry_run):
    print("")
    found = Proj2Clangd.detect(root)
    if not found:
        report.ok("没有 Keil/IAR/CMake 工程,跳过 clangd 配置。")
        return 0

    projects = _clangd_projects(found)
    if dry_run:
        report.ok("[dry-run] 会为 {0} 个工程生成 clangd 配置: {1}".format(
            len(projects),
            ", ".join(str(p) if p else k for k, _, p in projects)))
        return 0

    if len(projects) > 1:
        report.note("发现 {0} 个工程,逐个配置(每个工程一份数据库,"
                    "放在它自己的目录里)。".format(len(projects)))

    stuck = []
    for kind, label, project in projects:
        argv = ["--kind", kind, "-p", str(root)]
        if project is not None:
            # Both vendor backends take --project, and -o now defaults to the
            # project's own directory, so this is all it takes to keep two
            # databases from landing on top of each other.
            argv += ["--project", str(project)]
        print("")
        rc = Proj2Clangd.main(argv)
        if rc:
            stuck.append((kind, project, rc))

    _report_reanchor_exe(root, report, projects)

    if not stuck:
        report.ok("clangd 配置已生成并自检通过({0} 个工程)".format(len(projects)))
        return 0

    # Ambiguity is a refusal by design (which target? which config?), not a
    # crash -- surfacing the command beats swallowing it.
    report.ask("{0}/{1} 个工程的 clangd 配置没生成 —— 多半是要你指定 "
               "target/configuration。照上面的候选列表选一个再跑:".format(
                   len(stuck), len(projects)))
    for kind, project, rc in stuck:
        flag = "-c" if kind == "iar" else "-t"
        target = "<configuration 名>" if kind == "iar" else "<target 名>"
        if project is None:
            report.note('  py -3 Proj2Clangd.py --kind {0} -p "{1}"  (退出码 {2})'
                        .format(kind, root, rc))
        else:
            report.note('  py -3 Proj2Clangd.py --kind {0} --project "{1}" '
                        '{2} {3}  (退出码 {4})'
                        .format(kind, project, flag, target, rc))
    return 1


def step_clean_branch(root, report):
    """只报告 —— 分支对账要人现场判断,不该被一条 init 顺手带过。"""
    print("")
    # Re-read: step_configs may have just created the project layer, and the
    # cfg loaded at the top of init predates it.
    cfg = local_config.load(start=root)
    have = all(cfg.has(k) for k in
               ("branches.clean", "branches.work", "paths.code"))
    if not have:
        report.ok("clean-branch 未配置(项目配置里的 branches/paths 还是注释状态)")
        report.note("只做本机开发的话不用配。要做「只让代码进干净分支」那套再填。")
        return
    report.ask("clean-branch 已配置 —— 分支对账要你在场,init 不替你跑:")
    report.note("  py -3 CleanBranch.py detect")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def cmd_init(args):
    start = Path(args.path).resolve()
    root = repo_root(start)
    if not root:
        sys.stderr.write("{0} 不在 git 仓库里。\n".format(start))
        return 2

    print("=" * 70)
    print("{0} init: {1}".format(TOOL_NAME, root))
    print("=" * 70)

    try:
        cfg = local_config.load(start=root)
    except local_config.ConfigError as exc:
        sys.stderr.write(str(exc) + "\n")
        return 2

    pending = 0
    report = Report()
    rc = step_branch(root, cfg, report, args.worktree)
    if rc and not args.worktree:
        # Staying on a protected branch is a stop, not a warning: every later
        # step would write into the checkout we just said not to work in.
        _summary(report)
        return 1
    if args.worktree:
        new_root, rc = step_make_worktree(root, report, args.worktree,
                                          args.branch, args.dry_run)
        if rc:
            _summary(report)
            return 1
        root = new_root
        print("")
        print("以下步骤在新 worktree 里进行: {0}".format(root))

    step_configs(root, report, args.dry_run)
    pending += step_hygiene(root, report, args.dry_run, not args.no_apply)
    pending += step_clangd(root, report, args.dry_run)
    step_clean_branch(root, report)
    _summary(report)
    return 1 if (pending or report.todo) else 0


def _summary(report):
    print("")
    print("=" * 70)
    print("做完 {0} 件,需要你决定 {1} 件".format(len(report.done), len(report.todo)))
    for item in report.todo:
        print("  - " + item)
    print("=" * 70)


def cmd_explain(args):
    try:
        print(local_config.load(start=args.path).explain())
    except local_config.ConfigError as exc:
        sys.stderr.write(str(exc) + "\n")
        return 2
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="Keeper.py",
        description="{0} 的总入口:一条命令把仓库配置好,"
                    "需要人判断的一律报告不做。".format(TOOL_NAME))
    sub = parser.add_subparsers(dest="cmd")

    p = sub.add_parser("init", help="配置这个仓库(生成配置 + 治理 git 噪音 + clangd)")
    p.add_argument("-p", "--path", default=".", help="仓库目录(默认当前目录)")
    p.add_argument("--worktree", default=None,
                   help="在受保护分支上时,把 worktree 建到这个路径")
    p.add_argument("--branch", default=None,
                   help="随 --worktree 一起:新分支名(不给就检出已有分支)")
    p.add_argument("--no-apply", action="store_true",
                   help="只扫描不写(默认会写本机那几处:.git/info/、.git/index)")
    p.add_argument("--dry-run", action="store_true",
                   help="走真实路径但不落盘,报告将会发生什么")
    p.set_defaults(func=cmd_init)

    p2 = sub.add_parser("explain", help="每个配置值来自全局层还是项目层")
    p2.add_argument("-p", "--path", default=".")
    p2.set_defaults(func=cmd_explain)

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
