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

import cli_common as cc
import Iar2Clangd


def build_arg_parser():
    ap = cc.CliFriendlyParser(
        prog="Ewp2Json",
        description="(deprecated) forward to Iar2Clangd for compile_commands.json. "
                    "LLMs/agents: run 'Ewp2Json.py --ai-help' for usage guidance.")
    ap.add_argument('--json', action='store_true',
                    help='以 JSON 信封输出(与 --format json 等价)')
    ap.add_argument('--format', choices=('json',), default='json',
                    help='输出格式:仅支持 json(与 --json 等价)')
    ap.add_argument('--ai-help', action='store_true',
                    help='输出 AI 优化的使用说明并退出')
    return ap


def command(argv, context):
    if not context.json_mode:
        context.sinks.err.write(
            "NOTE: Ewp2Json.py is deprecated -- running Iar2Clangd.py "
            "with --no-clangd instead.\n"
            "      It fixes ICCARM-only parsing, adds build-configuration "
            "selection, .clangd output and toolchain header resolution.\n"
            "      Use Iar2Clangd.py directly for other options.\n\n")

    if "--no-compile-commands" in argv:
        return cc.fail("E_VALIDATION",
                       "Ewp2Json always generates compile_commands.json; "
                       "use Iar2Clangd.py directly for other controls.",
                       exit_code=cc.EXIT_ARG)

    forwarded = []
    if "--no-clangd" not in argv:
        injected = False
        for token in argv:
            if token == "--" and not injected:
                forwarded.append("--no-clangd")
                injected = True
            forwarded.append(token)
        if not injected:
            forwarded.append("--no-clangd")
    else:
        forwarded = list(argv)

    result = Iar2Clangd.command(forwarded, context)
    if result.error is None:
        meta = dict(result.meta) if result.meta else {}
        meta["deprecated"] = {
            "replacement": "Iar2Clangd.py --no-clangd",
            "note": "Ewp2Json is deprecated; use Iar2Clangd.py directly for "
                    ".clangd and other options"}
        return cc.CliResult(data=result.data, meta=meta,
                            exit_code=result.exit_code)
    return result


AI_HELP = """---
name: Ewp2Json
description: >
  Deprecated shim that generates compile_commands.json from an IAR .ewp
  project by forwarding to Iar2Clangd.py. Use when a legacy caller still
  invokes Ewp2Json.py and expects compile_commands.json only.
ai_help_version: 0.1.0
---

# Ewp2Json AI Help Guide

## Quick Reference

- **Generate compile_commands.json:** `Ewp2Json.py --project <file.ewp> --json`
- **Search a directory:** `Ewp2Json.py -p <dir> --json`
- **Preview without writing:** `Ewp2Json.py -p <dir> --dry-run --json`

## When to Use

Use this tool only when an existing script or habit still calls Ewp2Json.py
and only needs compile_commands.json.

Do NOT use for:
- anything needing `.clangd` (use `Iar2Clangd.py` directly)
- Keil `.uvprojx` projects (use `Keil2Clangd.py`)

## Deprecated

Ewp2Json.py is deprecated; it forwards to Iar2Clangd.py with `--no-clangd`
always applied, so only compile_commands.json is produced. The old script
only parsed a settings node literally named ``ICCARM``, so on any other IAR
architecture (ICCRL78, ICCRX, ICC430, ...) it silently parsed zero macros and
zero include paths and still wrote a compile_commands.json full of useless
entries. It also ignored build configurations, never produced a .clangd, and
could only write to the current directory. Iar2Clangd.py fixes all of these;
this shim forwards to it.

`--no-clangd` is always implied (this tool only emits compile_commands.json),
and `--no-compile-commands` is rejected with E_VALIDATION.

## Side Effects & Safety

- Writes `compile_commands.json` into the output directory (default: the
  .ewp's own directory).
- `--dry-run` previews without writing.
- Self-check runs after generation unless `--no-verify`.

## Exit Codes

| code | meaning |
|---|---|
| 0 | success |
| 1 | runtime failure (see error.code) |
| 2 | parameter / usage error (E_VALIDATION) |

## Errors & Recovery

| code | meaning | recovery |
|---|---|---|
| `E_VALIDATION` | bad argument / unknown config / ambiguous config or .ewp / `--no-compile-commands` | fix the argument, pass `-c` / `--project`, or drop `--no-compile-commands` |
| `E_NOT_FOUND` | no `.ewp`, no ICC* compiler node, or `--project` missing | point at a real project file |
| `E_VERIFICATION_FAILED` | generated files disagree or self-check failed | inspect error.details |
| `E_INTERNAL` | unexpected bug | report it |
"""


def main(argv=None, sinks=None):
    return cc.main(argv, sinks, command=command,
                   parser_factory=lambda: cc.CliFriendlyParser(prog="Ewp2Json"),
                   ai_help=AI_HELP, prog="Ewp2Json")


if __name__ == '__main__':
    raise SystemExit(main())
