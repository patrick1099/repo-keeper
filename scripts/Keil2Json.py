#!/usr/bin/env python3
"""Deprecated: forwards to Keil2Clangd.py.

The original Keil2Json.py had no --json mode, always wrote compile_commands.json
to the current working directory (silently ignoring the project's own
location), picked the first .uvprojx it found without asking, and returned no
reliable exit code on error. It only produced compile_commands.json and never
generated a .clangd.

Keil2Clangd.py replaces it. This shim keeps the old command working; -p and -a
mean the same thing there, and --no-clangd is applied by default so only
compile_commands.json is written, matching the old behaviour.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import cli_common as cc
import Keil2Clangd


def build_arg_parser():
    ap = cc.CliFriendlyParser(
        prog="Keil2Json",
        description="(deprecated) forward to Keil2Clangd for compile_commands.json. "
                    "LLMs/agents: run 'Keil2Json.py --ai-help' for usage guidance.")
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
            "NOTE: Keil2Json.py is deprecated -- running Keil2Clangd.py "
            "with --no-clangd instead.\n"
            "      It fixes silent first-project selection, CWD output, "
            "missing --json and unreliable exit codes.\n"
            "      Use Keil2Clangd.py directly for .clangd and other "
            "options.\n\n")

    if "--no-compile-commands" in argv:
        return cc.fail("E_VALIDATION",
                       "Keil2Json always generates compile_commands.json; "
                       "use Keil2Clangd.py directly for other controls.",
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

    result = Keil2Clangd.command(forwarded, context)
    if result.error is None:
        meta = dict(result.meta) if result.meta else {}
        meta["deprecated"] = {
            "replacement": "Keil2Clangd.py --no-clangd",
            "note": "Keil2Json is deprecated; use Keil2Clangd.py directly for "
                    ".clangd and other options"}
        return cc.CliResult(data=result.data, meta=meta,
                            exit_code=result.exit_code)
    return result


AI_HELP = """---
name: Keil2Json
description: >
  Deprecated shim that generates compile_commands.json from a Keil .uvprojx
  project by forwarding to Keil2Clangd.py. Use when a legacy caller still
  invokes Keil2Json.py and expects compile_commands.json only.
ai_help_version: 0.1.0
---

# Keil2Json AI Help Guide

## Quick Reference

- **Generate compile_commands.json:** `Keil2Json.py --project <file.uvprojx> --json`
- **Search a directory:** `Keil2Json.py -p <dir> --json`
- **Preview without writing:** `Keil2Json.py -p <dir> --dry-run --json`

## When to Use

Use this tool only when an existing script or habit still calls Keil2Json.py
and only needs compile_commands.json.

Do NOT use for:
- anything needing `.clangd` (use `Keil2Clangd.py` directly)
- IAR `.ewp` projects (use `Iar2Clangd.py`)

## Deprecated

Keil2Json.py is deprecated; it forwards to Keil2Clangd.py with `--no-clangd`
always applied, so only compile_commands.json is produced. Behaviour changes
vs the old script:

1. Output directory defaults to the .uvprojx's own directory, not the CWD
   (the old unconditional CWD write was a bug and is not preserved). Use
   `-o/--output` to override.
2. Multiple .uvprojx files are no longer silently picked in order; an
   ambiguity gate returns E_VALIDATION (exit 2) and `--project` is required.
3. Projects with several build targets require `-t/--target-name` (or
   `--use-first-target` when the choice truly does not matter).
4. compile_commands.json is upgraded to the canonical clangd format,
   including .dep enrichment when build output exists.
5. A post-generation self-check runs by default; failure is reported as
   E_VERIFICATION_FAILED (exit 1), `--no-verify` skips it.
6. `--dry-run`, `--json` and `--ai-help` are new.

`--no-clangd` is always implied (this tool only emits compile_commands.json),
and `--no-compile-commands` is rejected with E_VALIDATION.

## Side Effects & Safety

- Writes `compile_commands.json` into the output directory (default: the
  .uvprojx's own directory).
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
| `E_VALIDATION` | bad argument / ambiguous project or target / `--no-compile-commands` | fix the argument, pass `--project` / `-t`, or drop `--no-compile-commands` |
| `E_NOT_FOUND` | no `.uvprojx`, or `--project` missing | point at a real project file |
| `E_VERIFICATION_FAILED` | generated files disagree or self-check failed | inspect error.details |
| `E_INTERNAL` | unexpected bug | report it |
"""


def main(argv=None, sinks=None):
    return cc.main(argv, sinks, command=command,
                   parser_factory=lambda: cc.CliFriendlyParser(prog="Keil2Json"),
                   ai_help=AI_HELP, prog="Keil2Json")


if __name__ == '__main__':
    raise SystemExit(main())
