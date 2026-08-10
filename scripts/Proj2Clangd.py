#!/usr/bin/env python3
"""Proj2Clangd - one entry point for every supported project format.

Detects whether a directory holds a Keil MDK (.uvprojx), IAR EW (.ewp) or
CMake (CMakeLists.txt) project and hands off to the matching backend, passing
the remaining arguments through untouched. The per-backend scripts remain
usable directly; this only removes the need to know which one applies.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# What each backend is recognised by, in the order preferred when a directory
# somehow contains more than one (a vendor project is more specific than the
# CMakeLists.txt that may sit beside it).
DETECTORS = [
    ("keil", "**/*.uvprojx", "Keil MDK"),
    ("iar", "**/*.ewp", "IAR Embedded Workbench"),
    ("cmake", "**/CMakeLists.txt", "CMake"),
]

# Directories that hold copies of a project rather than the project itself.
IGNORED_DIR_PARTS = {'.git', 'node_modules', '_deps', 'CMakeFiles'}


def _interesting(path):
    return not any(part in IGNORED_DIR_PARTS for part in path.parts)


def detect(search_path):
    """Return [(kind, label, [matching files])] for every format found."""
    search_path = Path(search_path).resolve()
    found = []
    for kind, pattern, label in DETECTORS:
        matches = [m for m in sorted(search_path.glob(pattern)) if _interesting(m)]
        if matches:
            found.append((kind, label, matches))
    return found


def run_backend(kind, argv):
    if kind == 'keil':
        import Keil2Clangd
        return Keil2Clangd.main(argv)
    if kind == 'iar':
        import Iar2Clangd
        return Iar2Clangd.main(argv)
    if kind == 'cmake':
        import Cmake2Clangd
        return Cmake2Clangd.main(argv)
    raise ValueError("unknown backend {0!r}".format(kind))


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)

    ap = argparse.ArgumentParser(
        description="Detect the project type and run the matching "
                    "clangd-config backend",
        epilog="Any other arguments are forwarded to the backend. "
               "Run '<script> --kind iar -- --help' to see a backend's options.")
    ap.add_argument('-p', '--path', default='.',
                    help='Directory to search (default: current dir)')
    ap.add_argument('--kind', choices=[d[0] for d in DETECTORS], default=None,
                    help='Force a backend instead of detecting one')
    ap.add_argument('--detect-only', action='store_true',
                    help='Report what was found and exit')
    args, passthrough = ap.parse_known_args(argv)

    found = detect(args.path)

    if args.detect_only or not args.kind:
        if not found:
            print("No Keil (.uvprojx), IAR (.ewp) or CMake (CMakeLists.txt) "
                  "project found under {0}".format(Path(args.path).resolve()))
            return 1
        for kind, label, matches in found:
            print("{0:<6} {1:<26} {2} file(s), e.g. {3}".format(
                kind, label, len(matches), matches[0]))

    if args.detect_only:
        return 0

    kind = args.kind
    if kind is None:
        if len(found) > 1:
            print("\nMore than one project format found. "
                  "Choose one with --kind {0}".format(
                      '|'.join(k for k, _, _ in found)))
            return 1
        kind = found[0][0]
        print("Backend: {0}\n".format(kind))

    # -p is consumed above, so hand it back to the backend, which needs it too.
    return run_backend(kind, ['-p', args.path] + passthrough)


if __name__ == '__main__':
    raise SystemExit(main())
