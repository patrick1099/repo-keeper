#!/usr/bin/env python3
"""ReAnchor - surgically re-anchor .clangd / compile_commands.json after a project move.

Rewrites only machine/location-bound paths:
  * compile_commands.json "directory" -> the directory that file sits in
    (clangd requires an absolute anchor)
  * dead absolute toolchain -I / -imacros -> re-probed Keil location
Everything else (relative -I, -D macros, comments, AI-added lines) survives byte-for-byte.

The exe lives at the project root, so the search runs downwards: every config
location at or below the root is re-anchored, each against its own directory.
Before writing anything, the listed file set is checked against the tree -- a
database copied in from another project is refused rather than "fixed".
"""

import argparse
import copy
import json
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from Keil2Clangd import KeilPathResolver, _dedup  # noqa: E402

_WIN_ABS_RE = re.compile(r'^[A-Za-z]:[/\\]')
_ARM_MARKER = '/ARM/'


def _is_windows_abs(s):
    return bool(_WIN_ABS_RE.match(s))


def remap_dead_path(path_str, keil_root):
    """Map a dead toolchain path onto keil_root via its /ARM/... suffix.

    Returns the forward-slashed new path, or None when keil_root is unknown,
    the path has no /ARM/ segment, or the suffix does not exist under keil_root.
    """
    if not keil_root:
        return None
    norm = path_str.replace('\\', '/')
    idx = norm.upper().find(_ARM_MARKER)
    if idx < 0:
        return None
    cand = Path(keil_root) / norm[idx + 1:]
    if cand.exists():
        return str(cand).replace('\\', '/')
    return None


def fix_flag_value(path_str, keil_root):
    """Decide what to do with one -I/-imacros path value.

    Returns (new_path, status):
      (None, None)   -- relative or still alive: never touch
      (new, 'fixed') -- dead, remapped onto keil_root
      (None, 'dead') -- dead and not fixable: keep + warn
    """
    if not _is_windows_abs(path_str):
        return None, None
    if Path(path_str).exists():
        return None, None
    new = remap_dead_path(path_str, keil_root)
    if new:
        return new, 'fixed'
    return None, 'dead'


_CLANGD_I_RE = re.compile(r'^(\s*-\s+-I)(.*?)(\s*)$')
_CLANGD_BARE_RE = re.compile(r'^(\s*-\s+)([^-#\s].*?)(\s*)$')


def reanchor_clangd_text(text, keil_root):
    """Line-level surgery on .clangd text. Returns (new_text, changes, dead).

    Only -I values and the value line following '- -imacros' are candidates;
    every other line is passed through untouched. CRLF endings survive because
    the trailing-whitespace group captures the \r.
    """
    lines = text.split('\n')
    changes = []
    dead = []
    expect_imacros_value = False
    for i, line in enumerate(lines):
        m = _CLANGD_I_RE.match(line)
        if m:
            expect_imacros_value = False
        elif expect_imacros_value:
            m = _CLANGD_BARE_RE.match(line)
            expect_imacros_value = False
            if not m:
                continue
        else:
            if line.strip() == '- -imacros':
                expect_imacros_value = True
            continue
        val = m.group(2)
        new, status = fix_flag_value(val, keil_root)
        if status == 'fixed':
            lines[i] = m.group(1) + new + m.group(3)
            changes.append((val, new))
        elif status == 'dead':
            dead.append(val)
    return '\n'.join(lines), changes, dead


def reanchor_entries(entries, new_root, keil_root):
    """Mutate compile-command entries in place. Returns (changes, dead).

    Rewrites 'directory' to new_root and fixes dead toolchain -I/-imacros in
    'arguments'. 'command' is rebuilt as ' '.join(arguments) only when an
    argument actually changed, so hand-edited commands survive a pure
    directory re-anchor.
    """
    changes = []
    dead = []
    for entry in entries:
        args_changed = False
        old_dir = entry.get('directory')
        if old_dir != new_root:
            entry['directory'] = new_root
            changes.append((old_dir, new_root))
        args = entry.get('arguments')
        if not args:
            continue
        i = 0
        while i < len(args):
            a = args[i]
            val = prefix = None
            if a.startswith('-I'):
                val, prefix, at = a[2:], '-I', i
            elif a == '-imacros' and i + 1 < len(args):
                i += 1
                val, prefix, at = args[i], '', i
            if val is not None:
                new, status = fix_flag_value(val, keil_root)
                if status == 'fixed':
                    args[at] = prefix + new
                    changes.append((val, new))
                    args_changed = True
                elif status == 'dead':
                    dead.append(val)
            i += 1
        if args_changed and 'command' in entry:
            entry['command'] = ' '.join(args)
    return changes, dead


def check_ownership(entries, config_dir):
    """Does this database actually describe the project it now sits in?

    ReAnchor rewrites paths, never the file list, so a database copied in from
    a *different* project is re-anchored "successfully" while describing files
    that do not exist here. Catching that is the difference between a loud
    failure and a silently useless index.

    Returns (total, missing) where ``missing`` is the list of 'file' values
    that do not exist relative to ``config_dir``.
    """
    total = 0
    missing = []
    base = Path(config_dir)
    for entry in entries:
        f = entry.get('file')
        if not f:
            continue
        total += 1
        p = Path(f)
        if not p.is_absolute():
            p = base / p
        if not p.exists():
            missing.append(f)
    return total, missing


def report_ownership(name, total, missing, threshold, force):
    """Print the ownership verdict. Returns True when it is safe to continue."""
    if not total or not missing:
        return True
    ratio = len(missing) / float(total)
    if ratio <= threshold:
        print("{0}: {1}/{2} listed file(s) missing (deleted since generation?)"
              .format(name, len(missing), total))
        for f in missing[:3]:
            print("    " + f)
        return True

    print("{0}: WARNING this database does not belong to this project."
          .format(name))
    print("    {0} of {1} listed files ({2:.0%}) do not exist here."
          .format(len(missing), total, ratio))
    for f in missing[:5]:
        print("    missing: " + f)
    if len(missing) > 5:
        print("    ... and {0} more".format(len(missing) - 5))
    if force:
        print("    --force given: re-anchoring anyway.")
        return True
    print("    ReAnchor only rewrites paths, never the file list, so it cannot")
    print("    fix this. Re-run the clangd-config generator for THIS project.")
    print("    Use --force to re-anchor regardless.")
    return False


# Directories that never hold a project's own clangd config, but can hold
# copies of one (build output, vendored trees, VCS internals).
_SKIP_DIRS = {
    '.git', '.svn', '.hg', 'node_modules', '__pycache__', '.venv', 'venv',
    '.vs', '.vscode-test', 'build', 'dist', 'Objects', 'Listings', 'output',
    'Debug', 'Release', '.pytest_cache', '.mypy_cache',
}

_MAX_DEPTH = 6


def discover_config_dirs(root, max_depth=_MAX_DEPTH):
    """Directories at or below ``root`` holding a .clangd / compile_commands.json.

    The exe is meant to live at the project root and fix everything beneath it,
    but a Keil project keeps its config in Proj/ several levels down, and one
    repo can hold several projects (App/ and Boot/). So the search goes down,
    not just at the root -- while skipping trees that only ever hold copies.
    """
    root = Path(root).resolve()
    found = []
    stack = [(root, 0)]
    while stack:
        d, depth = stack.pop()
        try:
            children = list(d.iterdir())
        except OSError:
            continue
        if any(c.name in ('.clangd', 'compile_commands.json') and c.is_file()
               for c in children):
            found.append(d)
        if depth >= max_depth:
            continue
        for c in children:
            if c.is_dir() and c.name not in _SKIP_DIRS and not c.is_symlink():
                stack.append((c, depth + 1))
    found.sort()
    return found


def _default_root():
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).resolve().parent
    return Path.cwd()


def _backup(path):
    shutil.copy2(str(path), str(path) + '.bak')


def _report(name, changes, dead, dry_run):
    tag = 'would rewrite' if dry_run else 'rewrote'
    for old, new in _dedup([tuple(c) for c in changes]):
        print("{0}: {1} {2} -> {3}".format(name, tag, old, new))
    for p in _dedup(dead):
        print("{0}: WARNING kept dead path {1} "
              "(not found under new Keil; re-run the generator/skill)".format(name, p))


class ConfigSite:
    """One directory holding a .clangd and/or a compile_commands.json.

    Each site re-anchors against *its own* directory. Using one shared root for
    all of them would rewrite every 'directory' to the repo root, which is
    exactly the corruption the recursive search would otherwise introduce.
    """

    def __init__(self, directory):
        self.dir = Path(directory).resolve()
        self.new_root = str(self.dir).replace('\\', '/')
        self.clangd_path = self.dir / '.clangd'
        self.cc_path = self.dir / 'compile_commands.json'
        self.clangd_text = None
        self.entries = None

    def label(self, name, root):
        try:
            rel = self.dir.relative_to(root)
        except ValueError:
            rel = self.dir
        prefix = '' if str(rel) == '.' else '{0}/'.format(str(rel).replace('\\', '/'))
        return prefix + name

    def load(self):
        """Read both files. Returns an error string, or None on success."""
        if self.clangd_path.is_file():
            with open(str(self.clangd_path), 'r', encoding='utf-8', newline='') as f:
                self.clangd_text = f.read()
        if self.cc_path.is_file():
            # utf-8-sig also accepts BOM-less files; an editor may have added one.
            with open(str(self.cc_path), 'r', encoding='utf-8-sig') as f:
                self.entries = json.load(f)
            if not isinstance(self.entries, list):
                return ("compile_commands.json must be a JSON array (got {0}) in {1}"
                        .format(type(self.entries).__name__, self.dir))
        return None

    def scan_dead(self):
        """Dead toolchain paths, without touching anything."""
        dead = []
        if self.clangd_text is not None:
            _, _, d = reanchor_clangd_text(self.clangd_text, None)
            dead += d
        if self.entries is not None:
            _, d = reanchor_entries(copy.deepcopy(self.entries), self.new_root, None)
            dead += d
        return dead

    def apply(self, keil_root, root, dry_run):
        """Rewrite and persist. Returns the number of distinct path changes."""
        total = 0
        if self.clangd_text is not None:
            new_text, changes, dead = reanchor_clangd_text(self.clangd_text, keil_root)
            if new_text != self.clangd_text and not dry_run:
                _backup(self.clangd_path)
                with open(str(self.clangd_path), 'w', encoding='utf-8', newline='') as f:
                    f.write(new_text)
            _report(self.label('.clangd', root), changes, dead, dry_run)
            total += len(_dedup([tuple(c) for c in changes]))
        if self.entries is not None:
            changes, dead = reanchor_entries(self.entries, self.new_root, keil_root)
            if changes and not dry_run:
                _backup(self.cc_path)
                with open(str(self.cc_path), 'w', encoding='utf-8') as f:
                    json.dump(self.entries, f, indent=4, ensure_ascii=False)
            _report(self.label('compile_commands.json', root), changes, dead, dry_run)
            total += len(_dedup([tuple(c) for c in changes]))
        return total


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Re-anchor .clangd / compile_commands.json after moving a project")
    ap.add_argument('--root', default=None,
                    help='Directory to search (default: exe dir / cwd). '
                         'Configs are looked for here AND below.')
    ap.add_argument('-k', '--keil-path', default=None,
                    help='Keil installation path (skips auto-probe)')
    ap.add_argument('--dry-run', action='store_true',
                    help='Report changes without writing files')
    ap.add_argument('--force', action='store_true',
                    help='Re-anchor even when the file list does not match this project')
    ap.add_argument('--ownership-threshold', type=float, default=0.10,
                    help='Fraction of listed files allowed to be missing '
                         '(default: 0.10)')
    ap.add_argument('--max-depth', type=int, default=_MAX_DEPTH,
                    help='How deep to search below the root (default: {0})'
                         .format(_MAX_DEPTH))
    ap.add_argument('--no-pause', action='store_true',
                    help='Do not wait for Enter before exiting (frozen exe)')
    args = ap.parse_args(argv)

    root = Path(args.root).resolve() if args.root else _default_root()
    config_dirs = discover_config_dirs(root, max_depth=args.max_depth)

    if not config_dirs:
        print("ERROR: no .clangd or compile_commands.json found in or below "
              + str(root).replace('\\', '/'))
        return _finish(1, args)

    sites = [ConfigSite(d) for d in config_dirs]
    if len(sites) > 1:
        print("Found {0} config location(s) below {1}:".format(len(sites), root))
        for s in sites:
            rel = s.label('', root).rstrip('/')
            print("  " + (rel if rel else '.'))

    total = 0
    try:
        for site in sites:
            err = site.load()
            if err:
                print("ERROR: " + err)
                return _finish(1, args)

        # Ownership gate runs across every site BEFORE any write, so a bad
        # database cannot be half-applied.
        for site in sites:
            if site.entries is None:
                continue
            n, missing = check_ownership(site.entries, site.dir)
            if not report_ownership(site.label('compile_commands.json', root),
                                    n, missing, args.ownership_threshold, args.force):
                return _finish(1, args)

        dead_found = []
        for site in sites:
            dead_found += site.scan_dead()

        keil_root = None
        if dead_found:
            print("Dead toolchain paths detected:")
            for p in _dedup(dead_found):
                print("  " + p)
            keil_root = KeilPathResolver(keil_path=args.keil_path).keil_root
            if keil_root is None:
                print("WARNING: Keil installation not found -- "
                      "dead toolchain paths will be kept as-is.")

        for site in sites:
            total += site.apply(keil_root, root, args.dry_run)
    except json.JSONDecodeError as e:
        print("ERROR: failed to parse compile_commands.json: {0}".format(e))
        return _finish(1, args)
    except OSError as e:
        print("ERROR: file operation failed: {0}".format(e))
        return _finish(1, args)

    print("\n{0}: {1} path(s).".format(
        'Would change' if args.dry_run else 'Changed', total))
    return _finish(0, args)


def _finish(rc, args):
    if getattr(sys, 'frozen', False) and not args.no_pause:
        try:
            input("\nPress Enter to exit...")
        except EOFError:
            pass
    return rc


if __name__ == '__main__':
    raise SystemExit(main())
