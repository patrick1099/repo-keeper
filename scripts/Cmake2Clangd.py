#!/usr/bin/env python3
"""Cmake2Clangd - Make a CMake project's compile database discoverable by clangd.

Unlike the Keil and IAR backends this one does NOT parse the project or invent
compile flags: CMake already emits a compile_commands.json of its own, and
re-deriving it would only produce a worse copy. What CMake does not do is make
that database *findable*.

The database lands in the build directory, which is almost always a sibling of
the sources (``build/`` next to ``src/``). clangd searches a file's own
directory and its ancestors, never siblings, so out of the box cross-file
jump-to-definition silently fails while same-file navigation keeps working --
which is exactly the failure mode that looks like clangd is fine.

So this backend does three things: run the configure step with the export
switch on, verify the database landed and covers the sources, and drop a
pointer ``.clangd`` at the sources' ancestor so clangd can find it.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import k2c_common as common

import cli_common as cc


DB_NAME = 'compile_commands.json'

# Where a compile database tends to end up when this tool did not configure it.
BUILD_DIR_GLOBS = ['build', 'Build', 'out/build/*', 'cmake-build-*', 'bin']

# Multi-config IDE generators do not honour CMAKE_EXPORT_COMPILE_COMMANDS.
NON_EXPORTING_GENERATORS = ('Visual Studio', 'Xcode', 'Green Hills')

# Drivers clangd cannot infer a system include set from without help.
CROSS_DRIVER_HINTS = ('arm-none-eabi', 'riscv', 'avr-gcc', 'msp430',
                      'xtensa', 'aarch64-none', 'arm-linux')


# ---------------------------------------------------------------------------
# Locating things
# ---------------------------------------------------------------------------

def find_source_root(start):
    """Nearest directory at or above ``start`` holding a CMakeLists.txt."""
    start = Path(start).resolve()
    for candidate in [start] + list(start.parents):
        if (candidate / 'CMakeLists.txt').is_file():
            return candidate
    matches = sorted(start.glob('**/CMakeLists.txt'))
    return matches[0].parent if matches else None


def find_database(source_root, build_dir=None):
    """Find an existing compile_commands.json for this project."""
    if build_dir:
        candidate = Path(build_dir) / DB_NAME
        return candidate if candidate.is_file() else None
    direct = source_root / DB_NAME
    if direct.is_file():
        return direct
    for pattern in BUILD_DIR_GLOBS:
        for match in sorted(source_root.glob(pattern)):
            candidate = match / DB_NAME
            if candidate.is_file():
                return candidate
    return None


def find_cmake(explicit=None):
    if explicit:
        return explicit if Path(explicit).is_file() or shutil.which(explicit) else None
    return shutil.which('cmake')


def pick_generator(explicit=None):
    """Prefer Ninja: it exports a compile database, IDE generators do not."""
    if explicit:
        return explicit
    if shutil.which('ninja'):
        return 'Ninja'
    if shutil.which('make') or shutil.which('mingw32-make'):
        return 'Unix Makefiles' if os.name != 'nt' else 'MinGW Makefiles'
    return None


# ---------------------------------------------------------------------------
# Configure
# ---------------------------------------------------------------------------

class ConfigureResult:
    def __init__(self, ran, ok, command=None, output='', reason=''):
        self.ran = ran
        self.ok = ok
        self.command = command or []
        self.output = output
        self.reason = reason


def configure(cmake_exe, source_root, build_dir, generator=None, extra_args=()):
    """Run the CMake configure step with the compile-database export on."""
    if cmake_exe is None:
        return ConfigureResult(False, False, reason="cmake executable not found on PATH")

    cmd = [str(cmake_exe), '-S', str(source_root), '-B', str(build_dir),
           '-DCMAKE_EXPORT_COMPILE_COMMANDS=ON']
    if generator:
        cmd += ['-G', generator]
        if any(generator.startswith(g) for g in NON_EXPORTING_GENERATORS):
            return ConfigureResult(
                False, False, cmd,
                reason="generator '{0}' does not write {1}; use -G Ninja"
                       .format(generator, DB_NAME))
    cmd += list(extra_args)

    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=900)
    except (OSError, subprocess.SubprocessError) as exc:
        return ConfigureResult(True, False, cmd, reason=str(exc))

    output = ((proc.stdout or b'') + (proc.stderr or b'')).decode('utf-8', 'replace')
    return ConfigureResult(True, proc.returncode == 0, cmd, output,
                           '' if proc.returncode == 0
                           else 'cmake exited {0}'.format(proc.returncode))


# ---------------------------------------------------------------------------
# Database inspection
# ---------------------------------------------------------------------------

class Database:
    """A parsed compile_commands.json."""

    def __init__(self, path):
        self.path = Path(path).resolve()
        self.entries = json.loads(self.path.read_text(encoding='utf-8'))

    @property
    def directory(self):
        return self.path.parent

    def source_files(self):
        files = []
        for entry in self.entries:
            raw = entry.get('file')
            if not raw:
                continue
            base = Path(entry.get('directory') or self.directory)
            files.append((base / raw).resolve() if not Path(raw).is_absolute()
                         else Path(raw).resolve())
        return files

    def compilers(self):
        names = []
        for entry in self.entries:
            arguments = entry.get('arguments')
            first = (arguments[0] if arguments
                     else (entry.get('command') or '').split(' ')[0])
            if first and first not in names:
                names.append(first)
        return names

    def cross_drivers(self):
        """Compilers clangd will not derive a system include set from."""
        return [c for c in self.compilers()
                if any(hint in c.lower() for hint in CROSS_DRIVER_HINTS)]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_arg_parser():
    ap = cc.CliFriendlyParser(
        prog="Cmake2Clangd",
        description="LLMs/agents: run 'Cmake2Clangd --ai-help' for usage guidance. "
                    "Configure a CMake project and make its compile database "
                    "discoverable by clangd")
    ap.add_argument('-p', '--path', default='.',
                    help='Project directory to start from (default: current dir)')
    ap.add_argument('-b', '--build-dir', default=None,
                    help='Build directory (default: <source root>/build)')
    ap.add_argument('-G', '--generator', default=None,
                    help='CMake generator (default: Ninja when available)')
    ap.add_argument('--cmake', default=None, help='Path to the cmake executable')
    ap.add_argument('--cmake-args', default=None,
                    help='Extra arguments forwarded to the configure step. The '
                         'value starts with a dash, so it must be attached with '
                         '"=": --cmake-args="-DFOO=BAR"')
    ap.add_argument('--no-configure', action='store_true',
                    help='Do not run cmake; use an existing compile database')
    ap.add_argument('-o', '--output', default=None,
                    help='Where to write the pointer .clangd '
                         '(default: the sources\' common ancestor)')
    ap.add_argument('--no-clangd', action='store_true',
                    help='Only report; do not write a .clangd')
    ap.add_argument('--dry-run', action='store_true',
                    help='Print the analysis without writing or configuring')
    ap.add_argument('--json', action='store_true',
                    help='以 JSON 信封输出(与 --format json 等价)')
    ap.add_argument('--format', choices=('json',), default='json',
                    help='输出格式:仅支持 json(与 --json 等价)')
    ap.add_argument('--ai-help', action='store_true',
                    help='输出 AI 优化的使用说明并退出')
    return ap


_COMPILER_CHECK_MARKERS = (
    'linker command failed',
    'lld-link: error',
    'ld: cannot find',
    'could not open',
    'The C compiler',
    'The CXX compiler',
    'is not able to compile a simple test program',
)


def _looks_like_compiler_check_failure(output):
    """Did configure die in CMake's compiler check rather than on the project?"""
    return sum(marker in output for marker in _COMPILER_CHECK_MARKERS) >= 2


def _split(raw):
    if not raw:
        return []
    import shlex
    return shlex.split(raw)


def command(argv, context):
    args = build_arg_parser().parse_args(argv)

    source_root = find_source_root(args.path)
    if source_root is None:
        return cc.fail("E_NOT_FOUND",
                       "ERROR: no CMakeLists.txt at or below {0}".format(
                           Path(args.path).resolve()),
                       details={"search_path": str(Path(args.path).resolve())})
    print("Source root: {0}".format(source_root))

    build_dir = Path(args.build_dir).resolve() if args.build_dir \
        else (source_root / 'build')

    # --- configure -------------------------------------------------------
    if not args.no_configure and not args.dry_run:
        cmake_exe = find_cmake(args.cmake)
        generator = pick_generator(args.generator)

        if cmake_exe is None:
            print("cmake: NOT FOUND on PATH -- skipping the configure step.")
            print("  Install cmake, or pass --no-configure to use an existing "
                  "{0}.".format(DB_NAME))
        else:
            if generator is None:
                print("WARNING: neither ninja nor make found; falling back to "
                      "CMake's default generator.")
                print("  If that default is a Visual Studio generator it will "
                      "NOT write {0}.".format(DB_NAME))
            if build_dir.is_dir() and not (build_dir / 'CMakeCache.txt').is_file() \
                    and any(build_dir.iterdir()):
                print("WARNING: {0} already has files but no CMakeCache.txt; "
                      "configuring there will mix build systems.".format(build_dir))
                print("  Pass --build-dir to use a separate directory.")
            print("Configuring: {0}".format(build_dir))
            result = configure(cmake_exe, source_root, build_dir, generator,
                               _split(args.cmake_args))
            if not result.ok:
                for line in result.output.splitlines()[-15:]:
                    print("  | {0}".format(line))
                compiler_check = _looks_like_compiler_check_failure(result.output)
                if compiler_check:
                    print()
                    print("  This looks like CMake's compiler check failing at the")
                    print("  LINK step, not a problem with the project. That is the")
                    print("  normal case for a cross toolchain, or a host clang with")
                    print("  no MSVC/SDK libraries installed. Make the check")
                    print("  compile-only and try again:")
                    print('    --cmake-args="-DCMAKE_TRY_COMPILE_TARGET_TYPE=STATIC_LIBRARY"')
                return cc.fail(
                    "E_EXTERNAL_TOOL",
                    "ERROR: cmake configure failed -- {0}".format(result.reason),
                    details={
                        "tool": "cmake",
                        "reason": result.reason,
                        "output_tail": result.output.splitlines()[-15:],
                    },
                    suggestion=('--cmake-args="-DCMAKE_TRY_COMPILE_TARGET_TYPE='
                                'STATIC_LIBRARY"' if compiler_check else None))
            print("  cmake configure OK ({0})".format(generator or 'default generator'))

    # --- locate the database ---------------------------------------------
    db_path = find_database(source_root, build_dir if build_dir.is_dir() else None)
    if db_path is None:
        db_path = find_database(source_root)
    if db_path is None:
        return cc.fail(
            "E_NOT_FOUND",
            "ERROR: no {0} found under {1}.".format(DB_NAME, source_root),
            details={"source_root": str(source_root)},
            suggestion="Configure with -DCMAKE_EXPORT_COMPILE_COMMANDS=ON and a "
                       "generator that supports it (Ninja, Makefiles)")
    print("Database:    {0}".format(db_path))

    database = Database(db_path)
    sources = database.source_files()
    print("  {0} entries, {1} distinct source files".format(
        len(database.entries), len(set(sources))))
    print("  compilers: {0}".format(', '.join(database.compilers()) or '(none)'))

    missing = [s for s in sources if not s.is_file()]
    if missing:
        print("  WARNING: {0} listed source file(s) do not exist, e.g. {1}"
              .format(len(missing), missing[0]))

    cross = database.cross_drivers()
    if cross:
        print("  NOTE: cross compiler in the database ({0}).".format(', '.join(cross)))
        print("  clangd cannot infer that toolchain's system headers on its own.")
        print("  Add to the .clangd if headers come up unresolved:")
        print("    CompileFlags:")
        print("      Compiler: {0}".format(cross[0]))
        print("  and start clangd with --query-driver=<path-to-that-compiler>.")

    # --- placement --------------------------------------------------------
    placement = common.check_placement(database.directory, sources)
    print(placement.describe())

    data = {
        "source_root": str(source_root),
        "build_dir": str(build_dir),
        "database": str(db_path),
        "entries": len(database.entries),
        "sources": len(set(sources)),
        "placement_ok": placement.ok,
    }
    if args.dry_run:
        data["dry_run"] = True
        print("--dry-run: nothing written.")
        return cc.ok(data)
    if args.no_clangd:
        return cc.ok(data)

    if placement.ok:
        print("No pointer needed: clangd finds {0} from the sources on its own."
              .format(DB_NAME))
        return cc.ok(data)

    anchor = Path(args.output).resolve() if args.output else placement.anchor
    if anchor is None:
        return cc.fail(
            "E_VALIDATION",
            "ERROR: sources span several drives; no single anchor exists. "
            "Pass -o explicitly.",
            details={"reason": "sources_span_drives"},
            exit_code=cc.EXIT_ARG,
            suggestion="-o <dir>")
    common.write_pointer_clangd(database.directory, anchor)
    print("Restart clangd: Ctrl+Shift+P -> 'clangd: Restart language server'")
    data["anchor"] = str(anchor)
    data["pointer_written"] = True
    return cc.ok(data)


AI_HELP = """---
name: Cmake2Clangd
description: >
  Configure a CMake project with the compile-commands export switch on and drop
  a pointer .clangd so clangd can discover the database. Use when user asks to
  set up clangd for a CMake project, or mentions compile_commands.json / cmake
  build + clangd jump not working.
ai_help_version: 0.1.0
---

# Cmake2Clangd AI Help Guide

## Quick Reference

- **Analyze a project:** `Cmake2Clangd.py -p <src> --json`
- **Configure + pointer .clangd:** `Cmake2Clangd.py -p <src> --dry-run`
- **Use an existing database:** `Cmake2Clangd.py -p <src> --no-configure`

## When to Use

Use this tool when the user asks to:
- set up clangd jump-to-definition for a CMake project
- make a `compile_commands.json` discoverable from the sources
- fix cross-file navigation silently failing in a CMake project

Do NOT use for:
- Keil (.uvprojx) or IAR (.ewp) projects -- use Keil2Clangd / Iar2Clangd

## Command Reference

- `-p, --path <dir>`: project directory to start from (default: current dir)
- `-b, --build-dir <dir>`: build directory (default: `<source root>/build`)
- `-G, --generator <name>`: CMake generator (default: Ninja when available)
- `--cmake <path>`: cmake executable
- `--cmake-args=<args>`: extra arguments for the configure step
- `--no-configure`: use an existing compile database, do not run cmake
- `-o, --output <dir>`: where to write the pointer .clangd
- `--no-clangd`: only report; do not write a .clangd
- `--dry-run`: analyze without configuring or writing
- `--json`: machine envelope output (equivalent to `--format json`)

## Input / Output

- `--json` success: `{ok:true, data:{source_root, build_dir, database, entries,
  sources, placement_ok, dry_run?}, error:null, meta:{log}}`
- `--json` failure: envelope on stderr, stdout empty; codes below

## Side Effects & Safety

- Runs `cmake` to configure (skipped by `--no-configure` / `--dry-run`).
- Writes a pointer `.clangd` unless `--no-clangd` or placement is already OK.
- Idempotent: re-running converges; `--dry-run` never writes.

## Exit Codes

- 0 success
- 1 runtime failure (E_NOT_FOUND / E_EXTERNAL_TOOL)
- 2 parameter / usage error (E_VALIDATION)

## Errors & Recovery

| error.code | meaning | fix |
|---|---|---|
| E_NOT_FOUND | no CMakeLists.txt or no compile database | point `-p` at the source root; enable export on configure |
| E_EXTERNAL_TOOL | cmake configure failed | inspect error.details.output_tail; use the suggested `--cmake-args` |
| E_VALIDATION | sources span drives without `-o` | pass `-o <dir>` |
"""


def main(argv=None, sinks=None):
    return cc.main(argv, sinks, command=command,
                   parser_factory=build_arg_parser,
                   ai_help=AI_HELP, prog="Cmake2Clangd")


if __name__ == '__main__':
    raise SystemExit(main())
