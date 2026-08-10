#!/usr/bin/env python3
"""Shared building blocks for the clangd-config backends.

Every backend (Keil .uvprojx, IAR .ewp, CMake) parses a different project
format but emits the same two artifacts, so the emission side lives here:

  * toolchain-config access (Keil path, IAR path, probe caches)
  * path formatting (relative-to-anchor vs absolute, forward slashes)
  * ``.clangd`` document rendering
  * ``compile_commands.json`` entry construction and writing
  * the config-*discovery* placement check (clangd only searches ancestor
    directories, never siblings) plus the pointer-``.clangd`` fix for it

Nothing in here knows about a specific project file format.
"""

import hashlib
import json
import os
import re
import sys
import shutil
import subprocess
import time
from pathlib import Path


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from toolname import GLOBAL_DIR_NAME, REANCHOR_EXE, TOOL_NAME  # noqa: E402

#: Probed toolchain locations. JSON rather than the global TOML because this
#: file is *written* by the tool (probe results get cached back into it) and
#: the stdlib can read TOML but not write it.
CONFIG_FILE = Path.home() / GLOBAL_DIR_NAME / 'toolchains.json'

#: Where keil2clangd, this tool's predecessor, kept the same data. Read-only
#: fallback so a rename does not make the user re-enter their Keil/IAR paths;
#: anything saved from here on goes to CONFIG_FILE.
LEGACY_CONFIG_FILE = Path.home() / '.keil2clangd.json'

REANCHOR_EXE_NAME = REANCHOR_EXE

# Source files frozen into the re-anchor exe: ReAnchor imports Keil2Clangd,
# which imports k2c_common and k2c_macroscan. Editing any of them leaves the
# prebuilt exe behind.
REANCHOR_EXE_SOURCES = ('ReAnchor.py', 'Keil2Clangd.py', 'k2c_common.py',
                        'k2c_macroscan.py')


# ---------------------------------------------------------------------------
# toolchains.json
# ---------------------------------------------------------------------------

def stdin_is_interactive():
    """True only when there is a real console to prompt on.

    Agents and CI inherit a pipe. Prompting there either blocks until the run
    is killed or silently consumes whatever the pipe holds; neither is a way to
    ask a question.
    """
    try:
        return sys.stdin is not None and sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def load_config():
    """Return the toolchain config dict, or {} if absent/unreadable.

    Falls back to the predecessor's file so a rename does not cost the user
    their probed Keil/IAR paths. Reads only -- saves always go to CONFIG_FILE.
    """
    for path in (CONFIG_FILE, LEGACY_CONFIG_FILE):
        if not path.is_file():
            continue
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, OSError):
            continue
    return {}


def save_config(config):
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(config, f, indent=2, ensure_ascii=False)


def config_get(key, default=None):
    return load_config().get(key, default)


def config_set(key, value):
    config = load_config()
    config[key] = value
    save_config(config)


# ---------------------------------------------------------------------------
# Path formatting
# ---------------------------------------------------------------------------

def format_path(abs_path, base_dir, use_absolute=False):
    """Format a path relative to ``base_dir`` (or absolute), forward slashes.

    Falls back to absolute when the two live on different Windows drives,
    where no relative path exists.
    """
    abs_path = Path(abs_path).resolve()
    if use_absolute:
        return str(abs_path).replace('\\', '/')
    try:
        return os.path.relpath(str(abs_path), str(base_dir)).replace('\\', '/')
    except ValueError:
        return str(abs_path).replace('\\', '/')


# ---------------------------------------------------------------------------
# .clangd rendering
# ---------------------------------------------------------------------------

CLANG_TIDY_ADD = [
    "bugprone-*",
    "readability-*",
    "performance-*",
]

CLANG_TIDY_REMOVE = [
    "readability-magic-numbers",
    "readability-identifier-length",
    "bugprone-easily-swappable-parameters",
    "performance-no-int-to-ptr",
]

DIAGNOSTICS_SUPPRESS = [
    "-Wunused-parameter",
    "-Wmissing-prototypes",
    "-Wstrict-prototypes",
]

REMOVE_FLAGS = [
    "-W*",
    "-pedantic",
]


def render_diagnostics(suppress=None, tidy_add=None, tidy_remove=None):
    """Render the ``Diagnostics:`` block as a list of lines."""
    suppress = DIAGNOSTICS_SUPPRESS if suppress is None else suppress
    tidy_add = CLANG_TIDY_ADD if tidy_add is None else tidy_add
    tidy_remove = CLANG_TIDY_REMOVE if tidy_remove is None else tidy_remove

    lines = ["Diagnostics:", "  Suppress:"]
    lines += ["    - {0}".format(s) for s in suppress]
    lines += ["  ClangTidy:", "    Add:"]
    lines += ["      - {0}".format(a) for a in tidy_add]
    lines += ["    Remove:"]
    lines += ["      - {0}".format(r) for r in tidy_remove]
    return lines


class ClangdDoc:
    """Accumulate CompileFlags groups and render a ``.clangd`` document.

    Flags are added in labelled groups so the generated file stays readable
    and a human can tell which backend stage produced which flag.
    """

    def __init__(self):
        self._groups = []          # list of (comment_or_None, [flag, ...])
        self._seen = set()
        self._remove = list(REMOVE_FLAGS)
        self._remove_comment = "Drop warning flags that are noisy for embedded code"
        self._diagnostics = None   # None -> defaults

    def add_group(self, comment, flags, allow_duplicates=False):
        """Append a labelled flag group, dropping flags already emitted.

        ``allow_duplicates`` is for groups where a repeated token is meaningful
        rather than redundant -- two ``-imacros`` headers need two ``-imacros``
        tokens, and deduplicating them would silently drop the second header.
        """
        if allow_duplicates:
            fresh = list(flags)
            self._seen.update(fresh)
        else:
            fresh = []
            for flag in flags:
                if flag in self._seen:
                    continue
                self._seen.add(flag)
                fresh.append(flag)
        if fresh:
            self._groups.append((comment, fresh))
        return self

    def has_flag(self, flag):
        return flag in self._seen

    def set_remove(self, flags, comment=None):
        self._remove = list(flags)
        if comment is not None:
            self._remove_comment = comment
        return self

    def set_diagnostics(self, suppress=None, tidy_add=None, tidy_remove=None):
        self._diagnostics = (suppress, tidy_add, tidy_remove)
        return self

    def render(self):
        lines = ["CompileFlags:", "  Add:"]
        for comment, flags in self._groups:
            if comment:
                lines.append("    # {0}".format(comment))
            lines += ["    - {0}".format(f) for f in flags]

        lines.append("  Remove:")
        if self._remove_comment:
            lines.append("    # {0}".format(self._remove_comment))
        lines += ["    - {0}".format(f) for f in self._remove]

        lines.append("")
        if self._diagnostics is None:
            lines += render_diagnostics()
        else:
            lines += render_diagnostics(*self._diagnostics)
        return '\n'.join(lines) + '\n'

    def write(self, output_dir, dry_run=False):
        out = Path(output_dir) / '.clangd'
        if dry_run:
            print("Would generate: {0}".format(out))
            return out
        out.write_text(self.render(), encoding='utf-8')
        print("Generated: {0}".format(out))
        return out


# ---------------------------------------------------------------------------
# compile_commands.json
# ---------------------------------------------------------------------------

def _shell_quote(arg):
    """Quote an argument for the ``command`` string only.

    ``arguments`` is the authoritative, already-split form; ``command`` is a
    single string that a reader may re-split on whitespace, so any argument
    holding a space (toolchain paths under "Program Files", say) has to be
    quoted or it silently becomes two arguments.
    """
    if arg and not any(c in arg for c in ' \t"'):
        return arg
    return '"{0}"'.format(arg.replace('"', '\\"'))


def make_compile_entries(compiler, base_args, source_files, base_dir,
                         use_absolute=False):
    """Build compile-command entries, one per source file."""
    dir_str = str(Path(base_dir).resolve()).replace('\\', '/')
    entries = []
    for source in source_files:
        file_str = format_path(source, base_dir, use_absolute)
        command = "{0} -c {1} {2}".format(
            _shell_quote(compiler), _shell_quote(file_str),
            " ".join(_shell_quote(a) for a in base_args))
        entries.append({
            "command": command,
            "arguments": [compiler, "-c", file_str] + list(base_args),
            "directory": dir_str,
            "file": file_str,
        })
    return entries


def write_compile_commands(entries, output_dir, dry_run=False):
    out = Path(output_dir) / 'compile_commands.json'
    if dry_run:
        print("Would generate: {0}  ({1} entries)".format(out, len(entries)))
        return out
    with open(str(out), 'w', encoding='utf-8') as f:
        json.dump(entries, f, indent=4, ensure_ascii=False)
    print("Generated: {0}  ({1} entries)".format(out, len(entries)))
    return out


# ---------------------------------------------------------------------------
# Config-discovery placement
# ---------------------------------------------------------------------------

def _is_ancestor(ancestor, descendant):
    ancestor = Path(ancestor).resolve()
    descendant = Path(descendant).resolve()
    try:
        descendant.relative_to(ancestor)
        return True
    except ValueError:
        return False


def common_ancestor(paths):
    """Deepest directory that contains every given path, or None."""
    dirs = []
    for p in paths:
        p = Path(p).resolve()
        dirs.append(p if p.is_dir() else p.parent)
    if not dirs:
        return None
    try:
        return Path(os.path.commonpath([str(d) for d in dirs]))
    except ValueError:
        # different drives
        return None


class PlacementReport:
    """Result of checking whether clangd can *discover* the generated config.

    clangd searches a source file's own directory and its ancestors only --
    never siblings. When the output dir is a sibling of the sources, same-file
    navigation still works but cross-file jump-to-definition silently fails.
    """

    def __init__(self, output_dir, unreachable, anchor):
        self.output_dir = Path(output_dir).resolve()
        self.unreachable = unreachable      # source files clangd cannot reach
        self.anchor = anchor                # where a pointer .clangd should go

    @property
    def ok(self):
        return not self.unreachable

    def describe(self):
        if self.ok:
            return "placement: OK -- output dir is an ancestor of all sources."
        lines = [
            "placement: PROBLEM -- {0} source file(s) are NOT under the output dir."
            .format(len(self.unreachable)),
            "  output dir: {0}".format(self.output_dir),
            "  example:    {0}".format(self.unreachable[0]),
            "  clangd only searches a file's own dir and its ANCESTORS, so it will",
            "  never find this database. Same-file jumps keep working; cross-file",
            "  jump-to-definition and find-references silently fail.",
        ]
        if self.anchor:
            lines.append("  fix: drop a pointer .clangd in {0}".format(self.anchor))
        return '\n'.join(lines)


def check_placement(output_dir, source_files):
    """Check that ``output_dir`` is on the ancestor path of every source."""
    output_dir = Path(output_dir).resolve()
    unreachable = [Path(s).resolve() for s in source_files
                   if not _is_ancestor(output_dir, s)]
    anchor = None
    if unreachable:
        # The pointer .clangd must sit above the sources; it does NOT need to
        # contain the output dir, so folding that in would only push the anchor
        # needlessly shallow (often all the way to the drive root).
        anchor = common_ancestor(source_files)
    return PlacementReport(output_dir, unreachable, anchor)


def render_pointer_clangd(database_dir, anchor_dir, suppress=None,
                          tidy_add=None, tidy_remove=None):
    """Render a small ``.clangd`` that points clangd at a database elsewhere.

    The generated file carries the Diagnostics blocks too: the real ``.clangd``
    sits in a directory clangd will never read for these sources, so its
    diagnostics settings would otherwise be lost.
    """
    rel = format_path(database_dir, anchor_dir, use_absolute=False)
    lines = [
        "# Generated by {0}.".format(TOOL_NAME),
        "# The real compile_commands.json lives in a sibling directory, which",
        "# clangd never searches. This pointer makes it discoverable.",
        "CompileFlags:",
        "  CompilationDatabase: {0}".format(rel),
        "",
    ]
    lines += render_diagnostics(suppress, tidy_add, tidy_remove)
    return '\n'.join(lines) + '\n'


def write_pointer_clangd(database_dir, anchor_dir, dry_run=False, **kwargs):
    """Write the pointer ``.clangd``; ``dry_run`` reports without touching disk.

    The dry-run gate lives *inside* the write function on purpose: a preview
    must travel the same code path as the real write, so the two can never
    drift apart.
    """
    out = Path(anchor_dir) / '.clangd'
    if dry_run:
        print("Would generate: {0}  (pointer to {1})".format(out, database_dir))
        return out
    out.write_text(render_pointer_clangd(database_dir, anchor_dir, **kwargs),
                   encoding='utf-8')
    print("Generated: {0}  (pointer to {1})".format(out, database_dir))
    return out


# ---------------------------------------------------------------------------
# ReAnchor exe deployment
# ---------------------------------------------------------------------------

def reanchor_exe_source():
    """Path to the prebuilt re-anchor exe shipped with the plugin, or None."""
    cand = Path(__file__).resolve().parent / 'dist' / REANCHOR_EXE_NAME
    return cand if cand.is_file() else None


def reanchor_exe_stale_sources(exe=None):
    """Names of exe sources modified after the prebuilt exe was frozen.

    The exe is a build artifact in a gitignored ``dist/``, so nothing forces it
    to keep up with the scripts beside it. An exe built before a fix carries
    that fix's *absence* into every project the generator touches, and reports
    success while doing it -- the failure surfaces months later as a stale
    error message no one can find in the source. Comparing mtimes is what turns
    that silent drift into something the run says out loud.
    """
    exe = Path(exe) if exe is not None else reanchor_exe_source()
    if exe is None or not exe.is_file():
        return []
    try:
        built = exe.stat().st_mtime
    except OSError:
        return []
    here = Path(__file__).resolve().parent
    stale = []
    for name in REANCHOR_EXE_SOURCES:
        try:
            if (here / name).stat().st_mtime > built:
                stale.append(name)
        except OSError:
            continue
    return stale


def _same_bytes(a, b):
    """Content equality by hash. Equal sizes are not equal builds."""
    try:
        if Path(a).stat().st_size != Path(b).stat().st_size:
            return False
        return _sha256(a) == _sha256(b)
    except OSError:
        return False


def _sha256(path):
    h = hashlib.sha256()
    with open(str(path), 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def find_project_root(start, sources=None):
    """Best guess at the directory a human would call "the project root".

    Walks up from ``start`` looking for a ``.git`` entry -- a file in a
    worktree, a directory in a normal clone. Falls back to the common ancestor
    of the output dir and the sources, which is the tightest directory that
    still sits above everything the config describes.
    """
    start = Path(start).resolve()
    for d in [start] + list(start.parents):
        if (d / '.git').exists():
            return d
    if sources:
        anchor = common_ancestor(list(sources) + [start])
        if anchor:
            return anchor
    return start


def _git_ignores(path):
    """True when git would ignore ``path``. None when git cannot say."""
    path = Path(path)
    try:
        proc = subprocess.run(
            ['git', 'check-ignore', '-q', str(path)],
            cwd=str(path.parent), stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    # 0 = ignored, 1 = not ignored, 128 = not a repo / git unusable
    if proc.returncode == 0:
        return True
    if proc.returncode == 1:
        return False
    return None


def deploy_reanchor_exe(project_root, dry_run=False):
    """Copy the re-anchor exe to ``project_root`` so it is easy to find.

    The exe re-anchors by scanning *downwards* from its own directory, so the
    project root is the one place it works for every config in the tree -- and
    the one place a human will actually spot it. Returns the destination path,
    or None when nothing was deployed.
    """
    src = reanchor_exe_source()
    if src is None:
        print("re-anchor exe: not built -- skipped "
              "(build it with scripts/build_exe.bat)")
        return None

    # Warn before the "already current" shortcut: a stale exe that is already
    # in place is exactly the case that must not stay quiet.
    stale = reanchor_exe_stale_sources(src)
    if stale:
        built = time.strftime('%Y-%m-%d', time.localtime(src.stat().st_mtime))
        print("re-anchor exe: WARNING the prebuilt exe is OUT OF DATE "
              "(built {0}).".format(built))
        print("  Newer than it: {0}".format(', '.join(stale)))
        print("  It will run with the old behaviour, including bugs already "
              "fixed in the scripts.")
        print("  Rebuild before relying on it: scripts/build_exe.bat")

    dest = Path(project_root).resolve() / REANCHOR_EXE_NAME
    if dest.is_file() and _same_bytes(src, dest):
        print("re-anchor exe: already current at {0}".format(dest))
        return dest

    verb = "Would copy" if dry_run else "Copied"
    if not dry_run:
        try:
            shutil.copy2(str(src), str(dest))
        except OSError as e:
            print("re-anchor exe: WARNING could not copy to {0} ({1})".format(dest, e))
            return None
    print("re-anchor exe: {0} -> {1}".format(verb, dest))

    if _git_ignores(dest):
        print("  NOTE: git ignores this file (a '*.exe' rule, most likely).")
        print("  The exe is only useful to teammates if it travels with the repo:")
        print("    git add -f {0}".format(REANCHOR_EXE_NAME))
        print("    or add '!{0}' to .gitignore".format(REANCHOR_EXE_NAME))
        print("  Leave it untracked if you would rather keep the binary local.")
    return dest


# ---------------------------------------------------------------------------
# Post-generation self-check
#
# This used to be three sections of skill prose telling the caller to verify
# the output by hand. Hand verification leaves no trace when it is skipped --
# and it was skipped. Reading the files back off disk and saying what is wrong
# costs a few milliseconds and cannot be forgotten.
# ---------------------------------------------------------------------------

_ADD_FLAG_RE = re.compile(r'^\s{4}-\s+(-\S.*?)\s*$')


def parse_clangd_add_flags(clangd_path):
    """Flags under ``CompileFlags.Add`` of a generated .clangd, in order.

    Deliberately not a YAML parser: this only ever reads documents this package
    rendered, and adding a dependency to re-read our own output would be a poor
    trade. A pointer .clangd has no Add block and yields [].
    """
    flags = []
    in_add = False
    for line in Path(clangd_path).read_text(encoding='utf-8').splitlines():
        stripped = line.strip()
        if stripped.startswith('Add:'):
            in_add = True
            continue
        if in_add:
            if stripped.startswith(('Remove:', 'Diagnostics:', 'CompilationDatabase:')):
                break
            m = _ADD_FLAG_RE.match(line)
            if m:
                flags.append(m.group(1))
    return flags


def _defines(flags):
    return {f[2:] for f in flags if f.startswith('-D')}


def _includes(flags):
    return [f[2:] for f in flags if f.startswith('-I')]


class VerifyReport:
    """What the generated config looks like when read back off disk.

    ``errors`` are states that can never be legitimate -- the two generated
    files disagreeing with each other, or an anchor clangd cannot use. They set
    a non-zero exit. ``warnings`` are things that are usually wrong but have
    honest causes (a toolchain not installed on this machine, a source listed
    in a .dep that was since deleted), so they only print.
    """

    def __init__(self):
        self.errors = []
        self.warnings = []
        self.notes = []

    @property
    def ok(self):
        return not self.errors

    def describe(self, strict=False):
        lines = []
        if self.ok and not self.warnings:
            lines.append("verify: OK -- {0}".format(
                '; '.join(self.notes) if self.notes else 'no problems found.'))
            return '\n'.join(lines)

        lines.append("verify: {0} error(s), {1} warning(s)"
                     .format(len(self.errors), len(self.warnings)))
        for note in self.notes:
            lines.append("  {0}".format(note))
        for msg in self.errors:
            lines.append("  ERROR   {0}".format(msg))
        for msg in self.warnings:
            lines.append("  {0} {1}".format("ERROR  " if strict else "WARNING",
                                            msg))
        return '\n'.join(lines)


def verify_output(output_dir):
    """Read the generated .clangd + compile_commands.json back and check them.

    Checks, in the order the skill used to ask a human for:
      - every ``-I`` directory exists;
      - every ``file`` entry exists;
      - ``directory`` is an existing absolute path (clangd hard requirement);
      - the ``-D`` set in .clangd equals the ``-D`` set in the database.
    """
    report = VerifyReport()
    output_dir = Path(output_dir).resolve()
    clangd_path = output_dir / '.clangd'
    cc_path = output_dir / 'compile_commands.json'

    if not clangd_path.is_file() and not cc_path.is_file():
        report.errors.append(
            "neither .clangd nor compile_commands.json exists in {0}"
            .format(output_dir))
        return report

    clangd_flags = []
    if clangd_path.is_file():
        clangd_flags = parse_clangd_add_flags(clangd_path)
        missing_inc = [i for i in _includes(clangd_flags)
                       if not (output_dir / i).is_dir()]
        report.notes.append("{0} include path(s), {1} missing".format(
            len(_includes(clangd_flags)), len(missing_inc)))
        for inc in missing_inc:
            report.warnings.append("include path does not exist: {0}".format(inc))

    if not cc_path.is_file():
        return report

    try:
        entries = json.loads(cc_path.read_text(encoding='utf-8'))
    except ValueError as exc:
        report.errors.append("compile_commands.json is not valid JSON: {0}"
                             .format(exc))
        return report

    missing_src = []
    bad_dirs = set()
    db_defines = set()
    for entry in entries:
        directory = entry.get('directory', '')
        if not directory or not Path(directory).is_absolute():
            # clangd resolves every relative -I against this anchor and refuses
            # to work from a relative one, so this is fatal rather than untidy.
            bad_dirs.add(directory or '(empty)')
        elif not Path(directory).is_dir():
            bad_dirs.add(directory)
        src = Path(entry.get('file', ''))
        if not src.is_absolute():
            src = Path(directory or output_dir) / src
        if not src.is_file():
            missing_src.append(entry.get('file', ''))
        db_defines |= _defines(entry.get('arguments', []))

    report.notes.append("{0} source file(s), {1} missing"
                        .format(len(entries), len(missing_src)))
    for path in missing_src[:5]:
        report.warnings.append("source file does not exist: {0}".format(path))
    if len(missing_src) > 5:
        report.warnings.append("... and {0} more missing source file(s)"
                               .format(len(missing_src) - 5))
    for directory in sorted(bad_dirs):
        report.errors.append(
            "compile_commands.json 'directory' must be an existing absolute "
            "path, got: {0}".format(directory))

    if clangd_flags:
        clangd_defines = _defines(clangd_flags)
        only_clangd = clangd_defines - db_defines
        only_db = db_defines - clangd_defines
        if only_clangd or only_db:
            report.errors.append(
                ".clangd and compile_commands.json disagree on -D macros "
                "(clangd only: {0}; database only: {1})".format(
                    ', '.join(sorted(only_clangd)) or 'none',
                    ', '.join(sorted(only_db)) or 'none'))
        else:
            report.notes.append("{0} macro(s), consistent across both files"
                                .format(len(clangd_defines)))
    return report


def run_verify(output_dir, no_verify=False, strict=False):
    """Verify a freshly generated config and return the process exit code.

    Runs by default. An opt-in check is one a hurried caller simply omits,
    which is the failure this whole section exists to stop.
    """
    if no_verify:
        return 0
    report = verify_output(output_dir)
    print(report.describe(strict=strict))
    if report.errors:
        return 3
    if strict and report.warnings:
        return 3
    return 0
