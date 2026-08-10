#!/usr/bin/env python3
"""Find macros the code branches on that no build target defines.

The skill's macro-validation step used to be "grep for #ifdef and cross-check
by hand". That is a set difference over two sets nobody can hold in their head:
every macro every target defines, against every macro the sources test. Doing
it by hand is slow and, worse, not reproducible -- two runs disagree.

So the scan lives here. It reads the source list from the generated
compile_commands.json (the authoritative file set) and splits the macros the
code tests into three buckets:

  * defined by a target or by the compiler  -> resolved, nothing to do
  * #define'd somewhere in the sources      -> self-contained (a chip-family
                                               macro, FL_*_DRIVER_ENABLED, ...)
  * neither                                 -> UNRESOLVED: either dead code in
                                               every build, or a macro the
                                               project passes in some other way

Include guards are excluded: an `#ifndef X` immediately followed by
`#define X` is a guard, not a build switch, and they would otherwise drown
every real finding.
"""

import re
from pathlib import Path


_IFDEF_RE = re.compile(r'^\s*#\s*(ifdef|ifndef)\s+([A-Za-z_]\w*)')
_IF_RE = re.compile(r'^\s*#\s*(?:if|elif)\b(.*)$')
_DEFINED_RE = re.compile(r'defined\s*\(?\s*([A-Za-z_]\w*)')
_DEFINE_RE = re.compile(r'^\s*#\s*define\s+([A-Za-z_]\w*)')

# Chinese comments in this corner of the world are GB2312/CP936, never UTF-8.
# gb18030 is a superset of both and decodes ASCII identically, so it is the
# safe single choice; 'replace' keeps one odd byte from killing a whole file.
_ENCODING = 'gb18030'


def _read_lines(path):
    try:
        with open(str(path), 'r', encoding=_ENCODING, errors='replace') as f:
            return f.read().split('\n')
    except OSError:
        return []


def _is_guard(lines, i, name):
    """True when `#ifndef name` on line i is immediately followed by its #define."""
    for line in lines[i + 1:i + 3]:
        if not line.strip():
            continue
        m = _DEFINE_RE.match(line)
        return bool(m and m.group(1) == name)
    return False


class MacroUse:
    __slots__ = ('name', 'count', 'first_file', 'first_line')

    def __init__(self, name, path, lineno):
        self.name = name
        self.count = 1
        self.first_file = path
        self.first_line = lineno

    def bump(self):
        self.count += 1


def scan_sources(source_files):
    """Return (tested, defined) over the given files.

    ``tested`` maps macro name -> MacroUse for every macro the preprocessor
    branches on; ``defined`` is the set of names #define'd anywhere in them.
    """
    tested = {}
    defined = set()

    def note(name, path, lineno):
        if name in tested:
            tested[name].bump()
        else:
            tested[name] = MacroUse(name, path, lineno)

    for path in source_files:
        path = Path(path)
        lines = _read_lines(path)
        for i, line in enumerate(lines):
            d = _DEFINE_RE.match(line)
            if d:
                defined.add(d.group(1))
                continue
            m = _IFDEF_RE.match(line)
            if m:
                kind, name = m.group(1), m.group(2)
                if kind == 'ifndef' and _is_guard(lines, i, name):
                    continue
                note(name, path, i + 1)
                continue
            m = _IF_RE.match(line)
            if m:
                for name in _DEFINED_RE.findall(m.group(1)):
                    note(name, path, i + 1)
    return tested, defined


def macro_names(defines):
    """Strip any ``=value`` so ``FOO=1`` compares equal to ``FOO``."""
    out = set()
    for d in defines:
        d = str(d).strip()
        if not d:
            continue
        out.add(d.split('=', 1)[0].strip())
    return out


def names_from_define_lines(lines):
    """Macro names out of raw ``#define NAME ...`` text (the IAR probe's format)."""
    out = set()
    for line in lines:
        m = _DEFINE_RE.match(str(line))
        if m:
            out.add(m.group(1))
    return out


# Macros whose *absence* is the normal state: language-standard and
# compiler-identity probes. `#ifdef __cplusplus` guards appear in every vendor
# header, so leaving them in would put a 400-hit non-finding at the top of the
# report and bury the real ones.
WELL_KNOWN_ABSENT = frozenset({
    '__cplusplus', '__STDC__', '__STDC_VERSION__', '__STDC_HOSTED__',
    '__GNUC__', '__clang__', '_MSC_VER', '__ICCARM__', '__TI_COMPILER_VERSION__',
})


def classify(tested, defined, known):
    """Split tested macros into (unresolved, self_defined), both sorted."""
    unresolved = []
    self_defined = []
    for name in sorted(tested):
        if name in known or name in WELL_KNOWN_ABSENT:
            continue
        (self_defined if name in defined else unresolved).append(tested[name])
    unresolved.sort(key=lambda u: (-u.count, u.name))
    self_defined.sort(key=lambda u: (-u.count, u.name))
    return unresolved, self_defined


def _rel(path, base):
    try:
        return str(Path(path).relative_to(base)).replace('\\', '/')
    except (ValueError, TypeError):
        return str(path).replace('\\', '/')


def report(source_files, known, base_dir=None, limit=40):
    """Print the hidden-macro report. Returns the list of unresolved names."""
    source_files = [Path(s) for s in source_files if Path(s).is_file()]
    print("\n[Hidden macro scan] {0} source file(s)".format(len(source_files)))
    if not source_files:
        print("  nothing to scan")
        return []

    tested, defined = scan_sources(source_files)
    unresolved, self_defined = classify(tested, defined, known)

    if self_defined:
        print("  Defined by the sources themselves ({0}) -- no action needed:"
              .format(len(self_defined)))
        for u in self_defined[:limit]:
            print("    {0}  (x{1})".format(u.name, u.count))
        if len(self_defined) > limit:
            print("    ... and {0} more".format(len(self_defined) - limit))

    if not unresolved:
        print("  UNRESOLVED: none -- every macro the code tests is accounted for.")
        return []

    print("  UNRESOLVED ({0}) -- tested by the code, defined by no target and no "
          "header.".format(len(unresolved)))
    print("  These branches are inactive in every build. Confirm that is intended;")
    print("  if one should be on, add it as -D in .clangd (and in Keil).")
    for u in unresolved[:limit]:
        print("    {0}  (x{1})  first at {2}:{3}".format(
            u.name, u.count, _rel(u.first_file, base_dir), u.first_line))
    if len(unresolved) > limit:
        print("    ... and {0} more".format(len(unresolved) - limit))
    return [u.name for u in unresolved]
