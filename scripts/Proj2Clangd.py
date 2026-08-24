#!/usr/bin/env python3
"""Proj2Clangd - one entry point for every supported project format.

Detects whether a directory holds a Keil MDK (.uvprojx), IAR EW (.ewp) or
CMake (CMakeLists.txt) project and hands off to the matching backend, passing
the remaining arguments through untouched. The per-backend scripts remain
usable directly; this only removes the need to know which one applies.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import cli_common as cc

DETECTORS = [
    ("keil", "**/*.uvprojx", "Keil MDK"),
    ("iar", "**/*.ewp", "IAR Embedded Workbench"),
    ("cmake", "**/CMakeLists.txt", "CMake"),
]

IGNORED_DIR_PARTS = {'.git', 'node_modules', '_deps', 'CMakeFiles'}


def _interesting(path):
    return not any(part in IGNORED_DIR_PARTS for part in path.parts)


def detect(search_path):
    search_path = Path(search_path).resolve()
    found = []
    for kind, pattern, label in DETECTORS:
        matches = [m for m in sorted(search_path.glob(pattern)) if _interesting(m)]
        if matches:
            found.append((kind, label, matches))
    return found


def run_backend(kind, argv, context):
    if kind == 'keil':
        import Keil2Clangd
        return Keil2Clangd.command(argv, context)
    if kind == 'iar':
        import Iar2Clangd
        return Iar2Clangd.command(argv, context)
    if kind == 'cmake':
        import Cmake2Clangd
        return Cmake2Clangd.command(argv, context)
    raise ValueError("unknown backend {0!r}".format(kind))


def build_arg_parser():
    ap = cc.CliFriendlyParser(
        prog="Proj2Clangd",
        description="LLMs/agents: run 'Proj2Clangd --ai-help' for usage guidance. "
                    "Detect the project type and run the matching "
                    "clangd-config backend",
        epilog="Any other arguments are forwarded to the backend. "
               "Run '<script> --kind iar -- --help' to see a backend's options.")
    ap.add_argument('-p', '--path', default='.',
                    help='Directory to search (default: current dir)')
    ap.add_argument('--kind', choices=[d[0] for d in DETECTORS], default=None,
                    help='Force a backend instead of detecting one')
    ap.add_argument('--detect-only', action='store_true',
                    help='Report what was found and exit')
    ap.add_argument('--json', action='store_true',
                    help='以 JSON 信封输出(与 --format json 等价)')
    ap.add_argument('--format', choices=('json',), default='json',
                    help='输出格式:仅支持 json(与 --json 等价)')
    ap.add_argument('--ai-help', action='store_true',
                    help='输出 AI 优化的使用说明并退出')
    return ap


def command(argv, context):
    args, passthrough = build_arg_parser().parse_known_args(argv)
    json_mode = context.json_mode
    found = detect(args.path)

    if args.detect_only or not args.kind:
        if not found:
            return cc.fail("E_NOT_FOUND",
                           "No Keil (.uvprojx), IAR (.ewp) or CMake (CMakeLists.txt) "
                           "project found under {0}".format(Path(args.path).resolve()),
                           details={"path": str(Path(args.path).resolve())})
        for kind, label, matches in found:
            print("{0:<6} {1:<26} {2} file(s), e.g. {3}".format(
                kind, label, len(matches), matches[0]))

    if args.detect_only:
        if json_mode:
            return cc.ok({"detected": [
                {"kind": k, "label": l, "files": [str(m) for m in ms]}
                for k, l, ms in found]})
        return cc.ok()

    kind = args.kind
    if kind is None:
        if len(found) > 1:
            return cc.fail("E_VALIDATION",
                           "More than one project format found. Choose one with --kind {0}"
                           .format('|'.join(k for k, _, _ in found)),
                           details={"kinds": [k for k, _, _ in found]},
                           exit_code=cc.EXIT_ARG,
                           suggestion="--kind <backend>")
        kind = found[0][0]
        print("Backend: {0}\n".format(kind))

    return run_backend(kind, ['-p', args.path] + passthrough, context)


AI_HELP = """---
name: Proj2Clangd
description: >
  Detect whether a directory holds a Keil MDK (.uvprojx), IAR EW (.ewp) or
  CMake (CMakeLists.txt) project and hand off to the matching clangd-config
  backend, forwarding extra arguments through untouched. Use when user asks to
  set up clangd for a project of unknown format.
ai_help_version: 0.1.0
---

# Proj2Clangd AI Help Guide

## Quick Reference

- **Set up clangd for whatever project is here:** `Proj2Clangd.py -p <dir> --json`
- **See what was found first:** `Proj2Clangd.py -p <dir> --detect-only --json`
- **Force a backend:** `Proj2Clangd.py -p <dir> --kind iar --json`

## When to Use

Use this tool when the user asks to:
- set up clangd jump-to-definition for a project whose format you don't know
- run whichever backend matches the directory

Do NOT use for:
- a known format -- call Keil2Clangd / Iar2Clangd / Cmake2Clangd directly
- re-anchoring configs after a move -- use ReAnchor

## Command Reference

- `-p, --path <dir>`: directory to search (default: current dir)
- `--kind <keil|iar|cmake>`: force a backend instead of detecting
- `--detect-only`: report what was found and exit
- `--json`: machine envelope output (equivalent to `--format json`)

Any other arguments are forwarded to the chosen backend.

## Input / Output

- `--json --detect-only` success: `{ok:true, data:{detected:[{kind,label,files}]}, error:null}`
- `--json` success (dispatch): the backend's envelope, passed through unchanged
- `--json` failure: envelope on stderr, stdout empty; codes below

## Side Effects & Safety

- Does not write anything itself; the chosen backend may write .clangd / compile_commands.json.
- `--detect-only` is read-only.

## Exit Codes

| code | meaning |
|---|---|
| 0 | success |
| 1 | runtime failure (see error.code) |
| 2 | parameter / usage error (E_VALIDATION) |

## Errors & Recovery

| code | meaning | recovery |
|---|---|---|
| `E_NOT_FOUND` | no .uvprojx / .ewp / CMakeLists.txt found | point `-p` at the project dir |
| `E_VALIDATION` | several formats found, or bad --kind | pass `--kind` to choose a backend |
"""


def main(argv=None, sinks=None):
    return cc.main(argv, sinks, command=command,
                   parser_factory=build_arg_parser, ai_help=AI_HELP,
                   prog="Proj2Clangd")


if __name__ == '__main__':
    raise SystemExit(main())
