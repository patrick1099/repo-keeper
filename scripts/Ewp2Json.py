#!/usr/bin/env python3
"""Deprecated: forwards to Iar2Clangd.py.

The original Ewp2Json.py only looked for a settings node literally named
``ICCARM``, so on any other IAR architecture (ICCRL78, ICCRX, ICC430, ...) it
silently parsed zero macros and zero include paths and still wrote a
compile_commands.json full of useless entries. It also ignored build
configurations, never produced a .clangd, and could only write to the current
directory.

Iar2Clangd.py replaces it. This shim keeps the old command working; -p and -a
mean the same thing there.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import Iar2Clangd


def main(argv=None):
    print("NOTE: Ewp2Json.py is deprecated -- running Iar2Clangd.py instead.")
    print("      It fixes ICCARM-only parsing, adds build-configuration")
    print("      selection, .clangd output and toolchain header resolution.")
    print()
    return Iar2Clangd.main(argv)


if __name__ == '__main__':
    raise SystemExit(main())
