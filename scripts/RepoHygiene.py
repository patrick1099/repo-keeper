#!/usr/bin/env python3
"""Make ``git status`` in an embedded firmware repo show code changes only.

**Local by default.** ``.gitignore`` and ``.gitattributes`` are tracked files
pulled down from the shared branch; rewriting them turns one person's tidying
into a commit everyone else has to review, resolve and live with. Every fix
here therefore has a per-clone form that leaves the repo's contents untouched,
and that is what runs unless ``--shared`` is passed:

    .gitignore      ->  .git/info/exclude
    .gitattributes  ->  .git/info/attributes
    skip-worktree   ->  already local (it lives in .git/index)

The trade is real and stated rather than hidden: nothing local survives a fresh
clone, and a repo-level ``.gitignore`` rule OUTRANKS ``.git/info/exclude``, so a
negation cannot be undone from the local side (verified: with ``*.exe`` in
.gitignore, ``!keep.exe`` in info/exclude loses). Where that bites, the report
says so instead of writing a rule that quietly does nothing.


A Keil/IAR repo generates four kinds of noise, and they need three different
fixes -- which is the whole reason this is a tool and not a .gitignore snippet
someone pastes around:

  * **untracked build output** (``Objects/``, ``*.map``, ``.clangd``, ``~$x.docx``)
    -> a ``.gitignore`` rule. Works because git only consults .gitignore for
    paths it does not already track.

  * **tracked files that only ever change locally** (``*.uvoptx`` holds which
    target you clicked in the IDE; ``RTE_Components.h`` is rewritten every time
    Keil opens the project) -> ``git update-index --skip-worktree``. A
    .gitignore rule does *nothing* to a tracked file, and ``git rm --cached``
    would delete the file out of your colleagues' checkouts on their next pull.
    skip-worktree is the only one of the three that means "stop reporting my
    local edits" without changing what the repo contains.

  * **files reported modified whose content is byte-identical** -- the
    ``core.autocrlf=true`` line-ending dance. No ignore rule and no freeze can
    touch this one; it needs ``git add --renormalize`` plus a ``.gitattributes``
    entry so it does not come back.

  * **things only the owner can judge** (a ``Release/Useful/*.bin`` that may be
    a deliberately committed release artifact) -> reported, never acted on.

Default action is a scan that writes nothing. Every writer takes the same
``dry_run`` flag so a preview and a real run travel one code path.
"""

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from toolname import PROJECT_CONFIG_NAME, REANCHOR_EXE, TOOL_NAME  # noqa: E402


# The report is mostly Chinese. Left to the Windows locale default, piping it
# anywhere re-encodes it to cp936 and the reader sees mojibake.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, ValueError):
        pass


# ---------------------------------------------------------------------------
# The rule tables
#
# Every rule carries the reason it exists. A .gitignore whose lines cannot be
# explained is how the 150-line hand-maintained ones in these repos happened:
# nobody dares delete a line they cannot account for, so the file only grows.
# ---------------------------------------------------------------------------

# Sections of (why, [pattern, ...]) groups.
#
# The reason is attached to a GROUP, not to a pattern, because .gitignore has
# no inline comments: a `#` anywhere but column one is a literal character in
# the pattern. Writing `Objects/  # 输出目录` does not create a documented rule,
# it creates a rule that matches nothing -- silently, since git never reports a
# pattern that matched no files.
IGNORE_SECTIONS = [
    ("Keil / ARM 编译产物", [
        ("Keil 的输出目录:目标文件与列表文件", ["Objects/", "Listings/"]),
        ("目标文件与可执行映像", ["*.o", "*.obj", "*.axf", "*.elf", "*.hex"]),
        ("链接、依赖与构建报告(每次编译重写)",
         ["*.map", "*.lst", "*.build_log.htm", "*.dep", "*.lnp", "*.__i"]),
    ]),
    ("IAR 编译产物", [
        ("IAR 的输出目录", ["Obj/", "List/"]),
        ("浏览信息与生成的命令行文件", ["*.pbi", "*.pbi.*", "*.xcl"]),
        ("IAR 工作区窗口状态与调试会话", ["*.wsdt", "*.dni", "*.cspy.bat"]),
    ]),
    ("Keil / IAR 界面状态(每个人不一样,没有共享价值)", [
        ("Keil 窗口布局,文件名带用户名",
         ["*.uvguix.*", "*.uvgui.*", "*.uvguix.*.bak"]),
        ("Keil Pack 释放出来的原始副本", ["*.base@*"]),
    ]),
    ("clangd / 代码索引工具产物(带绝对路径,换台机器就失效)", [
        ("clangd-config 生成的配置,路径与本机绑定",
         [".clangd", "compile_commands.json", "compile_flags.txt",
          "k2c_iar_predef.h"]),
        ("索引与标签缓存", [".cache/", ".code-jump-tags/"]),
    ]),
    ("编辑器 / 本地工作区", [
        ("IDE 工作区设置", [".vscode/", ".idea/", "*.pro.user", "*.creator.user"]),
        ("本地临时目录与 worktree 存放点", [".wt/", ".tmp/", ".edx/"]),
    ]),
    ("日志与分析报表", [
        ("构建日志", ["build*.log", "build_log*.txt", "*_alog.txt", "*_log.txt",
                      "hs_err_pid*.log"]),
        ("Keil 生成的 Flash/RAM 占用分析表",
         ["*_analysis.xlsx", "*_sort_by_flash.csv", "*_sort_by_ram.csv"]),
    ]),
    ("Office 与系统临时文件", [
        ("Word/Excel 打开文档时的锁文件与自动保存", ["~$*", "*.autosave"]),
        ("Windows / macOS 目录元数据", ["Thumbs.db", "desktop.ini", ".DS_Store"]),
    ]),
    ("Python / 脚本产物", [
        ("Python 字节码与测试缓存",
         ["__pycache__/", "*.pyc", ".pytest_cache/"]),
    ]),
    ("本工具自己的产物", [
        ("重写 .gitignore 时留下的旧文件备份", [".gitignore.bak"]),
        # The project config layer holds branch refs, whitelists and the
        # reasons behind deliberate divergence -- one person's private notes
        # about a shared repo. It must never be offered up for commit, and
        # covering it here is what makes that automatic rather than a step
        # someone has to remember.
        ("本工具的项目配置(私人规则,不进仓库)", [PROJECT_CONFIG_NAME]),
    ]),
]

# Patterns that must survive the sweep. The re-anchor exe is only useful to a
# teammate if it travels with the repo, and every one of these repos already
# carries an `*.exe` rule that swallows it.
#
# These are emitted at the very END of the file, below the preserved manual
# block, because git resolves a path by LAST matching pattern. A negation above
# a hand-written `*.exe` is silently undone by it.
NEGATIONS = [
    ("!" + REANCHOR_EXE, "这个 exe 要跟着仓库走,否则换机器没人能修复 clangd 配置"),
]

# Tracked files whose every change is local state. These get skip-worktree, not
# an ignore rule (inert on a tracked file) and not `git rm --cached` (deletes it
# out of everyone else's checkout).
#
# Matched against every tracked file, not just today's dirty ones: the point of
# freezing `*.uvoptx` is that it never bothers you again, not that it stops
# bothering you until the next time you switch target.
FREEZE_RULES = [
    ("*.uvoptx",    "Keil 用户选项:当前选中哪个 target、断点、窗口位置"),
    ("*.uvopt",     "同上,Keil4"),
    ("RTE_Components.h", "Keil 每次打开工程都会重写,内容只跟工程名/target 名走"),
    ("*.pbd",       "IAR 浏览信息数据库,每次编译重写"),
    ("*.pbd.*",     "IAR 浏览信息数据库附属文件"),
    # Already committed by someone, carrying that machine's absolute paths.
    # The ignore rule is inert on them, so freezing is the only fix that keeps
    # your working copy correct without touching what the repo contains.
    (".clangd",     "别人提交过的 clangd 配置,里面是他那台机器的绝对路径"),
    ("compile_commands.json", "同上"),
]

# Tracked paths the tool refuses to classify on its own.
REVIEW_RULES = [
    ("*.bin",  "可能是你故意提交的发布固件,也可能是编译残留 —— 只有你知道"),
    ("*.hex",  "同上"),
    ("*.a",    "预编译库:可能是供应商给的,不是你编出来的"),
    ("*.lib",  "同上"),
]

# Existing .gitignore lines that are actively harmful. Each is something these
# repos really contain.
DANGEROUS_LINES = {
    "*.py":          "会静默忽略你以后写的每一个 Python 脚本",
    "*.0":           "含义模糊,极易误伤(任何以 .0 结尾的文件)",
    "CMakeLists.txt": "构建脚本是源码,不该被忽略",
    "*.c":           "源文件",
    "*.h":           "头文件",
    "Makefile":      "构建脚本是源码",
    "*.s":           "汇编源文件",
    "*.S":           "汇编源文件",
}

# Extensions git keeps trying to convert line endings on. Marking them -text
# stops the phantom-modified loop from coming back after a renormalize.
GITATTRIBUTES_BINARYISH = [
    ("*.uvprojx", "Keil 工程文件:内容常是 LF,而 core.autocrlf 想写成 CRLF"),
    ("*.uvoptx",  "同上"),
    ("*.ewp",     "IAR 工程文件"),
    ("*.eww",     "IAR 工作区文件"),
]

GENERATED_BANNER = ("# 以下规则由 {0} 的 repo-hygiene 生成 —— 重新生成会整体覆盖。"
                    .format(TOOL_NAME))

#: Recognises a generated banner written by **any** version of this tool.
#:
#: The exact string has already changed once -- keil2clangd became repo-keeper --
#: and matching only the current spelling made the rename half-land: a block
#: written under the old name no longer looked generated, so it was preserved as
#: user content and a second block was appended below it. The file then held two
#: generated blocks, roughly a hundred duplicated lines, under a banner that
#: promises the opposite. Nothing failed and nothing said so.
#:
#: Matching the shape instead of the literal is what makes the next rename a
#: non-event. The tool name is the only part allowed to vary; the rest of the
#: sentence is the signature.
GENERATED_BANNER_RE = re.compile(
    r'^# 以下规则由 \S+ 的 repo-hygiene 生成 .*$', re.MULTILINE)
MANUAL_MARKER = "# ==== 手工增补:这条线以下工具永不改动 ===="
NEGATION_MARKER = "# ==== 例外 —— 必须留在文件最后:git 按最后一条匹配的规则决定 ===="


# ---------------------------------------------------------------------------
# git plumbing
# ---------------------------------------------------------------------------

def git(args, cwd, check=True):
    """Run git and return stdout as text. Paths come back UTF-8-ish."""
    proc = subprocess.run(['git'] + list(args), cwd=str(cwd),
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if check and proc.returncode != 0:
        raise RuntimeError("git {0} failed ({1}): {2}".format(
            ' '.join(args), proc.returncode,
            proc.stderr.decode('utf-8', 'replace').strip()))
    return proc.stdout.decode('utf-8', 'surrogateescape')


def git_z(args, cwd):
    """Run a NUL-separated git listing and return the entries.

    ``-z`` is not a nicety here: without it git backslash-quotes any path that
    is not pure ASCII, and these repos are full of ``Doc/技术说明书/`` and
    ``上位机/``. Quoted paths silently fail every later comparison.
    """
    out = git(args, cwd)
    return [e for e in out.split('\0') if e]


def find_repo_root(start):
    out = git(['rev-parse', '--show-toplevel'], start, check=False).strip()
    return Path(out) if out else None


class RepoState:
    """Everything the plan needs to know about one repository."""

    def __init__(self, root):
        self.root = Path(root).resolve()
        self.tracked = set(git_z(['ls-files', '-z'], self.root))
        self.untracked = git_z(
            ['ls-files', '-z', '--others', '--exclude-standard'], self.root)
        self.deleted = set(git_z(
            ['diff', '--name-only', '-z', '--diff-filter=D'], self.root))

        # Two views of "modified" that disagree exactly on the phantoms:
        # diff-files trusts the index stat cache, diff compares content.
        stat_modified = set()
        for line in git(['diff-files', '--name-only', '-z'], self.root).split('\0'):
            if line:
                stat_modified.add(line)
        content_modified = set(git_z(['diff', '--name-only', '-z'], self.root))
        self.modified = content_modified
        self.phantom = sorted(stat_modified - content_modified - self.deleted)

        self.frozen = set()
        for entry in git_z(['ls-files', '-v', '-z'], self.root):
            # "<tag> <path>" -- lowercase tags and S mean a bit is already set
            if len(entry) > 2 and entry[1] == ' ':
                tag, path = entry[0], entry[2:]
                if tag in ('S', 'h', 's'):
                    self.frozen.add(path)

        # --git-common-dir, not --git-dir: in a linked worktree the two differ,
        # and info/exclude is read from the COMMON one. Writing to the worktree
        # copy would have no effect at all.
        common = git(['rev-parse', '--path-format=absolute',
                      '--git-common-dir'], self.root, check=False).strip()
        self.info_dir = Path(common) / 'info' if common \
            else self.root / '.git' / 'info'

    def gitignore_path(self):
        return self.root / '.gitignore'

    def ignore_path(self, shared):
        return self.gitignore_path() if shared else self.info_dir / 'exclude'

    def attributes_path(self, shared):
        return (self.root / '.gitattributes') if shared \
            else self.info_dir / 'attributes'

    def read_gitignore(self):
        return _read(self.gitignore_path())


def _read(path):
    if not Path(path).is_file():
        return ''
    return Path(path).read_text(encoding='utf-8', errors='surrogateescape')


# ---------------------------------------------------------------------------
# A gitignore matcher, just big enough to classify our own rules and the
# literal paths in the existing files.
# ---------------------------------------------------------------------------

def _translate(pattern):
    """Compile one gitignore pattern into a regex over a repo-relative path."""
    anchored = pattern.startswith('/')
    body = pattern[1:] if anchored else pattern
    dir_only = body.endswith('/')
    if dir_only:
        body = body[:-1]
    # A pattern containing a slash is anchored at the repo root; one without a
    # slash matches at any depth. That is git's rule, and getting it backwards
    # makes `Objects/` match nothing.
    if '/' in body:
        anchored = True

    out = []
    i = 0
    while i < len(body):
        c = body[i]
        if c == '*':
            if body[i:i + 3] == '**/':
                out.append('(?:.*/)?')
                i += 3
                continue
            if body[i:i + 2] == '**':
                out.append('.*')
                i += 2
                continue
            out.append('[^/]*')
        elif c == '?':
            out.append('[^/]')
        elif c == '[':
            j = body.find(']', i)
            if j == -1:
                out.append(re.escape(c))
            else:
                out.append(body[i:j + 1])
                i = j + 1
                continue
        else:
            out.append(re.escape(c))
        i += 1

    core = ''.join(out)
    prefix = '' if anchored else '(?:.*/)?'
    # The trailing group is what makes a directory rule cover its contents; on
    # a file pattern it is inert, so both cases share one expression.
    return re.compile('^' + prefix + core + '(?:/.*)?$')


class Matcher:
    """Ordered pattern list with git's last-match-wins negation."""

    def __init__(self, patterns):
        self.rules = []
        for pat in patterns:
            neg = pat.startswith('!')
            body = pat[1:] if neg else pat
            self.rules.append((neg, _translate(body), pat))

    def match(self, relpath):
        """Return the winning pattern string, or None when not ignored."""
        winner = None
        for neg, rx, pat in self.rules:
            if rx.match(relpath):
                winner = None if neg else pat
        return winner


def all_ignore_patterns():
    pats = []
    for _title, groups in IGNORE_SECTIONS:
        for _why, patterns in groups:
            pats += patterns
    pats += [p for p, _why in NEGATIONS]
    return pats


def matches_any(rules, relpath):
    """First (pattern, why) from a rule table matching ``relpath``, or None."""
    name = relpath.rsplit('/', 1)[-1]
    for pat, why in rules:
        if _translate(pat).match(relpath) or _translate(pat).match(name):
            return pat, why
    return None


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------

class Plan:
    def __init__(self, state):
        self.state = state
        self.newly_ignored = []     # untracked paths the new rules silence
        self.still_noisy = []       # untracked paths no rule covers
        self.freeze = []            # (path, why) tracked -> skip-worktree
        self.freeze_dirty = []      # of those, the ones dirty right now
        self.already_frozen = []
        self.renormalize = []       # phantom-modified paths
        self.review = []            # (path, why) tool refuses to decide
        self.absorbed = []          # old lines a generated rule now covers
        self.carried = []           # old lines kept verbatim
        self.dangerous = []         # (line, why) kept but flagged
        self.dead = []              # old literal lines pointing at nothing
        self.shadowed = []          # (pattern, tracked path) inert rules

    @property
    def touches_anything(self):
        return bool(self.newly_ignored or self.freeze or self.renormalize
                    or self.absorbed or self.dangerous)


def build_plan(state):
    plan = Plan(state)
    matcher = Matcher(all_ignore_patterns())

    for path in state.untracked:
        # git reports untracked directories with a trailing slash; test the
        # directory itself, since a rule on it covers everything inside.
        probe = path.rstrip('/')
        if matcher.match(probe) or matcher.match(path):
            plan.newly_ignored.append(path)
        else:
            plan.still_noisy.append(path)

    # A rule cannot silence a file git already tracks. Saying so up front is
    # the difference between "the rule did nothing" and a bug hunt.
    for path in sorted(state.tracked):
        hit = matcher.match(path)
        if hit and (path in state.modified or path in state.deleted):
            plan.shadowed.append((hit, path))

    dirty = state.modified | state.deleted | set(state.phantom)
    for path in sorted(state.tracked):
        if path in state.frozen:
            plan.already_frozen.append(path)
            continue
        hit = matches_any(REVIEW_RULES, path)
        if hit and path in dirty:
            plan.review.append((path, hit[1]))
            continue
        hit = matches_any(FREEZE_RULES, path)
        if hit:
            plan.freeze.append((path, hit[1]))
            if path in dirty:
                plan.freeze_dirty.append(path)

    # Deleted build output is its own decision: freezing a deletion would hide
    # a pending removal rather than a local edit.
    for path in sorted(state.deleted):
        if matcher.match(path) and not matches_any(FREEZE_RULES, path):
            plan.review.append((
                path,
                "已被跟踪的构建产物,你本地删掉了 —— 要么 commit 这个删除,"
                "要么 git checkout 恢复;工具不替你选"))

    plan.renormalize = list(state.phantom)

    seen = set()
    deduped = []
    for path, why in plan.review:
        if path not in seen:
            seen.add(path)
            deduped.append((path, why))
    plan.review = deduped

    _classify_old_lines(plan, state, matcher)
    return plan


def split_existing(text):
    """Cut an existing .gitignore into (legacy_head, manual_block).

    The file this tool writes has three parts in a fixed order: a generated
    block, the manual block, then the negations. On a re-run the manual block
    must come back byte for byte, the negations must not be duplicated, and the
    generated block must not be mistaken for hand-written rules -- which is
    exactly what happened before this function existed: every generated line
    was re-classified as "unrecognised" and copied into the manual block, while
    the real manual block, living below the marker, was dropped.
    """
    trailer = ''
    if NEGATION_MARKER in text:
        text, trailer = text.split(NEGATION_MARKER, 1)
    if MANUAL_MARKER in text:
        head, manual = text.split(MANUAL_MARKER, 1)
    else:
        head, manual = text, ''
    # Our own generated block regenerates from the rule tables; re-reading it
    # as user input would duplicate it. Any generation's banner counts -- see
    # GENERATED_BANNER_RE for what matching only the current one cost.
    if GENERATED_BANNER_RE.search(head):
        head = ''

    # Anything the user appended at end-of-file lands below the negation block.
    # Rescuing it into the manual block is not politeness -- dropping a line
    # someone added by the most natural gesture there is would be silent data
    # loss, and a .gitignore never tells you a rule went missing.
    known = {pat for pat, _why in NEGATIONS}
    rescued = [l.rstrip() for l in trailer.splitlines()
               if l.strip() and l.split('#')[0].strip() not in known
               and not l.strip().startswith('#')]

    kept = [l.rstrip() for l in manual.splitlines() if l.strip()] + rescued
    return head, kept


def _classify_old_lines(plan, state, matcher):
    """Split the existing .gitignore into absorbed / carried / flagged."""
    text = state.read_gitignore()
    if not text:
        return
    body, preserved = split_existing(text)
    plan.carried.extend(preserved)
    for raw in body.splitlines():
        line = raw.split('#')[0].strip()
        if not line:
            continue
        if line in DANGEROUS_LINES:
            plan.dangerous.append((line, DANGEROUS_LINES[line]))
            continue
        if line.startswith('!'):
            plan.carried.append(line)
            continue
        probe = line.lstrip('/').rstrip('/')
        if matcher.match(probe):
            plan.absorbed.append(line)
            continue
        # A literal path that no longer exists is dead weight, but it is kept:
        # a deleted directory can come back, and a wrong drop is silent.
        if not any(ch in line for ch in '*?[') and \
                not (state.root / probe).exists():
            plan.dead.append(line)
        if line not in plan.carried:
            plan.carried.append(line)


# ---------------------------------------------------------------------------
# Rendering and writing
# ---------------------------------------------------------------------------

def render_rule_block(plan, shared):
    """The generated rule block, identical in both targets."""
    lines = [
        "# " + "=" * 74,
        GENERATED_BANNER,
        "# 每条规则都写了为什么存在 —— 说不出理由的行,就是这个文件失控的起点。",
        "# " + "=" * 74,
        "",
    ]
    for title, groups in IGNORE_SECTIONS:
        lines.append("# ---- {0} ----".format(title))
        for why, patterns in groups:
            # The reason goes on its own line: .gitignore has no inline
            # comments, so a trailing `# ...` becomes part of the pattern.
            lines.append("# {0}".format(why))
            lines += patterns
        lines.append("")

    if plan.freeze or plan.already_frozen:
        lines += [
            "# ---- 说明:下面这些文件故意不写 ignore 规则 ----",
            "# *.uvoptx / RTE_Components.h 已经被仓库跟踪了,而 ignore 规则对已跟踪",
            "# 文件完全无效。它们改用 git update-index --skip-worktree 冻结:文件继",
            "# 续留在仓库里(同事那边毫无变化),只是本机的改动不再被 git 报告。",
            "",
        ]

    if shared:
        lines.append(MANUAL_MARKER)
        lines += plan.carried if plan.carried else [""]

        # Negations go last, below the manual block: git resolves a path by the
        # LAST pattern that matches it, so a `!foo.exe` sitting above a
        # hand-written `*.exe` is silently cancelled by it.
        lines += ["", NEGATION_MARKER]
        for pat, why in NEGATIONS:
            lines.append("# {0}".format(why))
            lines.append(pat)
    else:
        # No negations in the local file: a repo .gitignore outranks
        # .git/info/exclude, so `!x` here loses to a tracked `*.exe` every
        # time. Emitting it would look like a fix and be one line of nothing.
        lines += [
            "# ---- 这里不写 ! 例外规则 ----",
            "# 仓库自己的 .gitignore 优先级高于 .git/info/exclude,本地的 ! 打不过它。",
            "# 需要放行某个被仓库规则挡住的文件,只能 git add -f,或改共享 .gitignore。",
        ]
    return '\n'.join(lines).rstrip('\n') + '\n'


def render_gitignore(plan):
    """Full .gitignore text (shared target)."""
    return render_rule_block(plan, shared=True)


def _strip_generated(text):
    """Everything in ``text`` above our generated block.

    Cuts at the *first* banner from any tool generation, so a file that already
    collected duplicate blocks from the rename comes back with one.
    """
    match = GENERATED_BANNER_RE.search(text)
    if match is None:
        return text.rstrip('\n')
    head = text[:match.start()]
    # The banner normally sits under a framing '# ===...' rule; drop that too,
    # or every regeneration leaves one more orphaned line behind.
    frame = '# ' + '=' * 74
    lines = head.split('\n')
    while lines and (not lines[-1].strip() or lines[-1].rstrip() == frame):
        lines.pop()
    return '\n'.join(lines).rstrip('\n')


def write_ignore(plan, shared=False, dry_run=False):
    """Write the ignore rules, locally by default.

    The dry-run gate is inside the writer on purpose: a preview that travels a
    different code path from the real write is a preview of nothing.
    """
    state = plan.state
    target = state.ignore_path(shared)
    label = '.gitignore' if shared else '.git/info/exclude'
    old_text = _read(target)

    if shared:
        new_text = render_gitignore(plan)
    else:
        # Everything already in info/exclude is the user's -- git's own stub
        # comments included -- so it is kept verbatim above our block.
        kept = _strip_generated(old_text)
        new_text = (kept + '\n\n' if kept else '') \
            + render_rule_block(plan, shared=False)

    if old_text == new_text:
        print("  {0}: 已经是最新,不动".format(label))
        return False
    if dry_run:
        extra = ",旧文件备份为 .gitignore.bak" if shared else ",不进仓库,同事无感"
        print("  {0}: 将写入 ({1} 行 -> {2} 行){3}".format(
            label, len(old_text.splitlines()), len(new_text.splitlines()), extra))
        return True

    target.parent.mkdir(parents=True, exist_ok=True)
    if shared and old_text:
        (state.root / '.gitignore.bak').write_text(
            old_text, encoding='utf-8', errors='surrogateescape')
    target.write_text(new_text, encoding='utf-8')
    if shared:
        print("  .gitignore: 已重写(共享文件,会进提交),旧文件保存在 .gitignore.bak")
    else:
        print("  {0}: 已写入 —— 纯本地,不进仓库,同事看不到".format(label))

    missed = verify_with_git(plan)
    if missed:
        print("  {0}: !! 自检失败 —— 写完之后 git 仍然不忽略这 {1} 个文件:"
              .format(label, len(missed)))
        for path in missed[:10]:
            print("      {0}".format(path))
        print("      规则写进去了但没生效,通常是模式写法不对(ignore 文件不支持行尾注释)。")
    else:
        print("  {0}: 自检通过 —— git 确认 {1} 个文件已被忽略"
              .format(label, len(plan.newly_ignored)))
    return True


def _raw_matches_index(state, path):
    """True when the file's bytes on disk are exactly what the index stores.

    ``--no-filters`` is load-bearing: plain ``git hash-object`` DOES apply the
    path's filters when the file sits in a repo (verified -- on a CRLF worktree
    file with an LF blob the two hashes come out equal), so without it this
    function answers "does git think it is clean", which is the question that
    was already asked, instead of "are the bytes the same".
    """
    try:
        raw = git(['hash-object', '--no-filters', '--', path], state.root,
                  check=False).strip()
        entry = git(['ls-files', '-s', '--', path], state.root,
                    check=False).split()
    except RuntimeError:
        return False
    return bool(raw) and len(entry) >= 2 and entry[1] == raw


def verify_with_git(plan):
    """Ask git itself whether the rules we just wrote actually bite.

    Our own matcher decides the plan; git decides reality, and the two can
    disagree. They did: an early version wrote every rule as
    ``Objects/  # 输出目录``, which our matcher happily parsed and git read as a
    pattern containing spaces and a hash. All 74 rules matched nothing, and
    nothing said so -- git never reports a pattern that hit no files.

    So the check is not "did we write the file" but "does git now ignore the
    files we promised to silence".
    """
    if not plan.newly_ignored:
        return []
    root = plan.state.root
    # -z on both sides: without it git C-quotes any non-ASCII path on output
    # ("Doc/~$\350\257\264...") and the comparison below never matches, which
    # would report a working rule as broken on every 中文 filename.
    payload = '\0'.join(plan.newly_ignored).encode('utf-8', 'surrogateescape')
    proc = subprocess.run(
        ['git', 'check-ignore', '-z', '--stdin'],
        cwd=str(root), input=payload,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    covered = {l for l in proc.stdout.decode('utf-8', 'surrogateescape').split('\0')
               if l}
    return [p for p in plan.newly_ignored
            if p not in covered and p.rstrip('/') not in covered]


def write_gitattributes(plan, shared=False, dry_run=False):
    """Add ``-text`` for the project files git keeps re-converting.

    Gated on the repo *containing* such files, not on a phantom being visible
    right now. Gating on the symptom means the prevention only ever lands in
    the one moment the bug is showing -- and a re-run right after a successful
    renormalize would silently skip it, leaving the loop free to come back.
    """
    state = plan.state
    candidates = [p for p in sorted(state.tracked)
                  if matches_any(GITATTRIBUTES_BINARYISH, p)]
    if not candidates:
        return False

    # -text is only safe where the worktree bytes ALREADY equal the stored
    # blob. Where they differ -- worktree CRLF against a blob normalised to LF,
    # the usual case for a file committed under core.autocrlf -- switching off
    # normalisation does not reveal a phantom, it MANUFACTURES a real diff, and
    # the renormalize that follows would commit the CRLF bytes. That is a junk
    # commit touching every project file in the repo.
    unsafe = [p for p in candidates if not _raw_matches_index(state, p)]
    subjects = [p for p in candidates if p not in set(unsafe)]
    if unsafe:
        print("  {0}: 跳过 —— 这 {1} 个文件盘上的字节与仓库里存的不一致(多半是"
              "工作区 CRLF、仓库 LF)。".format(
                  '.gitattributes' if shared else '.git/info/attributes',
                  len(unsafe)))
        for path in unsafe[:5]:
            print("      {0}".format(path))
        print("      对它们加 -text 不是暴露幻影,是凭空造出一个真实差异,"
              "接着 renormalize 会把 CRLF 提交进去。")
    if not subjects:
        return False
    target = state.attributes_path(shared)
    label = '.gitattributes' if shared else '.git/info/attributes'
    # Append only. This file is where a repo's local clean/smudge filters get
    # installed, and clobbering one would break it with no visible symptom
    # until the wrong bytes are already committed.
    old = _read(target)
    needed = [(p, why) for p, why in GITATTRIBUTES_BINARYISH
              if p not in old]
    if not needed:
        return False
    # Same trap as .gitignore, one file over: an inline `# ...` is parsed as an
    # attribute name, git prints "# is not a valid attribute name" and the
    # -text never takes effect. Reasons go on their own lines.
    block = ["", "# 工程文件按原样存取,不做 CRLF/LF 转换 —— 否则 git 会反复把它们",
             "# 报成「已修改」而内容一个字节都没变。"]
    for pattern, why in needed:
        block.append("# {0}".format(why))
        block.append("{0} -text".format(pattern))
    if dry_run:
        print("  {0}: 将追加 {1} 条 -text 规则{2}".format(
            label, len(needed), "" if shared else "(纯本地)"))
        return True
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text((old.rstrip('\n') + '\n' if old else '')
                      + '\n'.join(block).lstrip('\n') + '\n',
                      encoding='utf-8', errors='surrogateescape')
    print("  {0}: 已追加 {1} 条 -text 规则{2}".format(
        label, len(needed), "(共享文件,会进提交)" if shared else " —— 纯本地"))

    # git puts the "not a valid attribute name" complaint on stderr, so the
    # normal helper -- stdout only -- would call a dead rule healthy.
    proc = subprocess.run(['git', 'check-attr', 'text', '--'] + subjects[:20],
                          cwd=str(state.root), stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT)
    out = proc.stdout.decode('utf-8', 'surrogateescape')
    if 'not a valid attribute name' in out or ': text: unspecified' in out:
        print("  {0}: !! 自检失败 —— git 没有认下这些规则:".format(label))
        for line in out.splitlines()[:6]:
            print("      {0}".format(line))
    else:
        print("  {0}: 自检通过 —— git 确认工程文件已不做换行转换".format(label))
    return True


def apply_freeze(plan, dry_run=False):
    """skip-worktree the tracked files that only ever change locally."""
    if not plan.freeze:
        return False
    paths = [p for p, _why in plan.freeze]
    if dry_run:
        print("  冻结: 将对 {0} 个文件设置 skip-worktree".format(len(paths)))
        return True
    # One call per batch, but keep the command line short on Windows.
    for i in range(0, len(paths), 50):
        git(['update-index', '--skip-worktree'] + paths[i:i + 50],
            plan.state.root)
    print("  冻结: {0} 个文件已 skip-worktree".format(len(paths)))
    return True


def apply_unfreeze(state, paths, dry_run=False):
    if not paths:
        return False
    if dry_run:
        print("  解冻: 将对 {0} 个文件清除 skip-worktree".format(len(paths)))
        return True
    for i in range(0, len(paths), 50):
        git(['update-index', '--no-skip-worktree'] + paths[i:i + 50], state.root)
    print("  解冻: {0} 个文件已恢复跟踪".format(len(paths)))
    return True


def apply_renormalize(plan, dry_run=False):
    """Clear the phantom-modified flag. Stages nothing when content matches.

    The phantom list is re-derived here rather than taken from the plan: the
    caller writes the ``-text`` attribute first, and that write itself can turn
    a previously clean file into a phantom. A list built before it is stale.
    """
    if dry_run:
        if not plan.renormalize:
            return False
        print("  换行归一: 将对 {0} 个「内容没变却显示已修改」的文件 "
              "git add --renormalize".format(len(plan.renormalize)))
        return True
    root = plan.state.root
    targets = RepoState(root).phantom
    if not targets:
        return False
    before = set(git_z(['diff', '--cached', '--name-only', '-z'], root))
    git(['add', '--renormalize'] + targets, root)
    after = set(git_z(['diff', '--cached', '--name-only', '-z'], root))

    # A genuinely phantom file stages nothing. Anything that did stage was a
    # real content change we misread -- so undo it rather than leave a silent
    # byte-level edit sitting in the index waiting to be committed.
    leaked = sorted(after - before)
    if leaked:
        git(['reset', '-q', '--'] + leaked, root, check=False)
        print("  换行归一: !! {0} 个文件其实内容真的不同,已撤出暂存区,未做改动:"
              .format(len(leaked)))
        for path in leaked[:5]:
            print("      {0}".format(path))
        return False
    print("  换行归一: {0} 个文件已归一(未 stage 任何内容)".format(len(targets)))
    return True


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def _bullet(items, limit=12):
    out = ["    - {0}".format(i) for i in items[:limit]]
    if len(items) > limit:
        out.append("    - ... 另外 {0} 个".format(len(items) - limit))
    return out


def describe(plan, shared=False):
    state = plan.state
    lines = ["", "=" * 78, "仓库: {0}".format(state.root), "=" * 78]
    lines.append("模式: {0}".format(
        "共享 —— 会改 .gitignore / .gitattributes,这是要进提交、同事会看到的改动"
        if shared else
        "本地 —— 只写 .git/info/ 与 .git/index,一个字节都不进仓库,同事无感"))

    lines.append("")
    lines.append("[1] ignore 规则能解决的 —— 未跟踪的生成物")
    if plan.newly_ignored:
        lines.append("  {0} 个文件/目录将被新规则挡住:".format(len(plan.newly_ignored)))
        lines += _bullet(plan.newly_ignored)
    else:
        lines.append("  无")
    if plan.still_noisy:
        lines.append("  规则覆盖不到、仍会出现在 git status 里的 {0} 个:"
                     .format(len(plan.still_noisy)))
        lines += _bullet(plan.still_noisy)
        lines.append("    (这些多半是真该提交的东西 —— 如果不是,告诉我加规则)")

    lines.append("")
    lines.append("[2] ignore 规则解决不了的 —— 已跟踪、但只有本机在改")
    if plan.freeze:
        lines.append("  {0} 个文件建议 skip-worktree 冻结:".format(len(plan.freeze)))
        for path, why in plan.freeze[:12]:
            lines.append("    - {0}".format(path))
            lines.append("        {0}".format(why))
        if len(plan.freeze) > 12:
            lines.append("    - ... 另外 {0} 个".format(len(plan.freeze) - 12))
        lines.append("  文件继续留在仓库里,同事 pull 不受任何影响;只是本机改动不再上报。")
        if plan.freeze_dirty:
            lines.append("  其中 {0} 个此刻就有未提交改动 —— 冻结后这些改动会从 "
                         "git status 消失(盘上内容不变):".format(len(plan.freeze_dirty)))
            lines += _bullet(plan.freeze_dirty, limit=6)
    else:
        lines.append("  无")
    if plan.already_frozen:
        lines.append("  已经冻结过的 {0} 个: {1}".format(
            len(plan.already_frozen), ', '.join(plan.already_frozen[:5])))

    lines.append("")
    lines.append("[3] 两者都解决不了的 —— 内容没变却显示「已修改」")
    if plan.renormalize:
        lines.append("  {0} 个文件的工作区内容与仓库内容逐字节相同,是 core.autocrlf "
                     "的换行折腾:".format(len(plan.renormalize)))
        lines += _bullet(plan.renormalize)
        lines.append("  修法: git add --renormalize(不会 stage 任何内容) + {0} 标 "
                     "-text 防复发".format(
                         ".gitattributes" if shared else ".git/info/attributes"))
    else:
        lines.append("  无")

    lines.append("")
    lines.append("[4] 只有你能判断的 —— 工具拒绝替你决定")
    if plan.review:
        for path, why in plan.review[:15]:
            lines.append("    - {0}".format(path))
            lines.append("        {0}".format(why))
        if len(plan.review) > 15:
            lines.append("    - ... 另外 {0} 个".format(len(plan.review) - 15))
    else:
        lines.append("  无")

    lines.append("")
    lines.append("[5] 仓库自带 .gitignore 的体检" if shared
                 else "[5] 仓库自带 .gitignore 的体检(只读 —— 本地模式不碰它)")
    legacy, preserved = split_existing(state.read_gitignore())
    old_count = len([l for l in legacy.splitlines()
                     if l.strip() and not l.strip().startswith('#')])
    if preserved:
        lines.append("  已是本工具生成的格式;「手工增补」区 {0} 行原样保留"
                     .format(len(preserved)))
    lines.append("  有效规则行: {0}".format(old_count))
    verb = "被通配规则吸收(可以删掉的死路径)" if shared \
        else "本工具的通配规则已覆盖(改共享文件时可删的冗余行)"
    lines.append("  {0}: {1}".format(verb, len(plan.absorbed)))
    if plan.absorbed:
        lines += _bullet(plan.absorbed, limit=8)
    if plan.dead:
        lines.append("  指向已不存在路径的死行: {0}".format(len(plan.dead)))
        lines += _bullet(plan.dead, limit=8)
    if plan.dangerous:
        lines.append("  !! 危险规则: {0}{1}".format(
            len(plan.dangerous),
            " —— 已从生成文件中剔除,请确认" if shared
            else " —— 本地模式不动它们,但你该知道它们在那儿"))
        for line, why in plan.dangerous:
            lines.append("    - {0}    <- {1}".format(line, why))
    if not shared and (plan.absorbed or plan.dead or plan.dangerous):
        lines.append("  想真的整理这个共享文件,加 --shared —— 那会产生一个同事要 "
                     "review 的提交,先跟他们打招呼。")
    if plan.shadowed:
        lines.append("  规则被已跟踪文件架空(规则写了也不生效,得靠 [2] 冻结): {0}"
                     .format(len(plan.shadowed)))
        for pat, path in plan.shadowed[:8]:
            lines.append("    - {0}  被  {1}  架空".format(pat, path))

    return '\n'.join(lines)


SKIP_WORKTREE_CAVEATS = """
关于 skip-worktree,三件必须知道的事:
  1. 这个标记存在 .git/index 里,是**每个 clone 各自的**。它不会推给同事,也不会
     被别的 clone 或 worktree 继承 —— 换一份检出就要重跑一次。
  2. 如果同事真的改了某个被冻结的文件,你 pull/checkout 时 git 会报
     "Your local changes to the following files would be overwritten"。
     解法: RepoHygiene.py --unfreeze <文件> 之后再拉,拉完重新冻结。
  3. 想主动提交某个冻结文件的改动: --unfreeze 它 -> git add/commit -> --freeze。
"""

LOCAL_MODE_CAVEATS = """本地模式的代价(换来的是仓库和同事完全不受影响):
  * 写的是 .git/info/exclude、.git/info/attributes 和 .git/index —— 这三样都
    **不会被 clone、不会被 push**,重新 clone 一份就没了,要重跑。
  * 仓库自己的 .gitignore 优先级**高于** .git/info/exclude,所以被仓库规则挡住
    的文件,本地放不出来(`!xxx` 在这里打不过)。只能 git add -f,或走 --shared。
"""


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def iter_repos(root, each):
    root = Path(root).resolve()
    if not each:
        found = find_repo_root(root)
        return [found] if found else []
    repos = []
    for child in sorted(p for p in root.iterdir() if p.is_dir()):
        found = find_repo_root(child)
        if found and found == child.resolve():
            repos.append(found)
    return repos


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="让 git status 只显示代码改动:ignore 规则 + 冻结本机状态文件 + "
                    "修掉换行造成的幻影修改。默认只扫描不写,且只写本机(.git/info/、"
                    ".git/index),一个字节都不进仓库。")
    parser.add_argument('-p', '--path', default='.',
                        help="仓库目录(默认当前目录)")
    parser.add_argument('--each', action='store_true',
                        help="把 --path 当作父目录,逐个处理其下的每个仓库")
    parser.add_argument('--shared', action='store_true',
                        help="改共享文件 .gitignore / .gitattributes 而不是 "
                             ".git/info/ —— 这会产生同事要 review 的提交,默认关闭")
    parser.add_argument('--write-ignore', action='store_true',
                        help="写 ignore 规则(默认写 .git/info/exclude)")
    parser.add_argument('--write-gitignore', action='store_true',
                        help="等同 --write-ignore --shared(保留的旧写法)")
    parser.add_argument('--freeze', action='store_true',
                        help="对 [2] 里的文件设置 skip-worktree")
    parser.add_argument('--renormalize', action='store_true',
                        help="修掉 [3] 里的幻影修改,并写 -text 属性防复发")
    parser.add_argument('--apply', action='store_true',
                        help="等于同时给上面三个开关")
    parser.add_argument('--unfreeze', nargs='*', metavar='PATH',
                        help="清除 skip-worktree;不给路径就解冻全部已冻结文件")
    parser.add_argument('--dry-run', action='store_true',
                        help="走真实写入路径但不落盘,报告将会发生什么")
    args = parser.parse_args(argv)

    repos = iter_repos(args.path, args.each)
    if not repos:
        print("没有找到 git 仓库: {0}".format(Path(args.path).resolve()))
        return 1

    shared = args.shared or args.write_gitignore
    do_ignore = args.write_ignore or args.write_gitignore or args.apply
    do_freeze = args.freeze or args.apply
    do_renorm = args.renormalize or args.apply

    for root in repos:
        state = RepoState(root)

        if args.unfreeze is not None:
            targets = args.unfreeze or sorted(state.frozen)
            if not targets:
                print("{0}: 没有被冻结的文件".format(root))
                continue
            print("仓库: {0}".format(root))
            apply_unfreeze(state, targets, dry_run=args.dry_run)
            continue

        plan = build_plan(state)
        print(describe(plan, shared=shared))

        if not (do_ignore or do_freeze or do_renorm):
            continue

        print("")
        print("执行{0}{1}:".format(
            " [共享模式 —— 会改进仓库的文件]" if shared else " [本地模式]",
            " (--dry-run,不落盘)" if args.dry_run else ""))
        if do_ignore:
            write_ignore(plan, shared=shared, dry_run=args.dry_run)
        if do_renorm:
            # Attributes first: setting -text changes how git normalises, so
            # the write itself can turn a clean file into a fresh phantom.
            # Renormalising before that would leave the new ones unfixed.
            write_gitattributes(plan, shared=shared, dry_run=args.dry_run)
            apply_renormalize(plan, dry_run=args.dry_run)
        if do_freeze:
            apply_freeze(plan, dry_run=args.dry_run)
            if plan.freeze:
                print(SKIP_WORKTREE_CAVEATS)
        if not shared:
            print(LOCAL_MODE_CAVEATS)

    if not (do_ignore or do_freeze or do_renorm) and args.unfreeze is None:
        print("")
        print("以上只是扫描,什么都没写。要执行(默认全走本机,不碰仓库文件):")
        print("  --write-ignore      只写 .git/info/exclude")
        print("  --freeze            只冻结 [2] 里的文件")
        print("  --renormalize       只修 [3] 的幻影修改")
        print("  --apply             三样都做      (加 --dry-run 先预演)")
        print("  --shared            改成写仓库里的 .gitignore / .gitattributes")
    return 0


if __name__ == '__main__':
    sys.exit(main())
