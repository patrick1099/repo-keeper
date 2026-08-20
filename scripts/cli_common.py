#!/usr/bin/env python3
"""CLI-AI spec shared kernel (repo-keeper batch T-A pilot).

A pure-stdlib common library that takes the reusable half of the CLI-AI spec
(Obsidian vault ``CLI-AI规范.md``) and keeps it in one place:

  * the unified light envelope ``{ok, data, error, meta}`` and exit codes 0/1/2
  * machine-mode progress buffering: success -> ``meta``, failure ->
    ``error.details``; stdout/stderr are written exactly once by the outer
    emitter
  * an equivalence pre-scan for ``--json`` / ``--format json`` /
    ``--format=json`` before argument parsing (honours the ``--`` terminator;
    never decides JSON mode from ``sys.argv`` itself)
  * an eager ``--ai-help`` scanner plus front-matter output
  * argparse errors raise the internal ``CliUsageError``, emitted exactly once
    by the outer layer (E_VALIDATION envelope + rc2 under JSON)
  * an exception-mapping chain: CliError/ExternalToolError business mapping
    first -> KeyboardInterrupt -> classify by ``__cause__`` (Permission /
    FileNotFound / IO) -> E_INTERNAL

Design constraints:

  * ``command(argv, context) -> CliResult`` is the business layer and touches
    no I/O stream; everything it needs to say travels through ``context`` or
    the returned ``CliResult``.
  * ``main(argv, sinks) -> exit_code`` is the entry layer; eager help, parse
    errors, exception mapping and the final emit happen only there.
  * Sinks are text file objects (``.write``). Never write
    ``sys.stdout.buffer`` directly -- that breaks StringIO /
    ``redirect_stdout``-based tests.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_ARG = 2

#: Canonical error-code table (spec section 1.2.1). New codes must be
#: registered here before they are used anywhere.
ERROR_CODES = (
    "E_VALIDATION", "E_NOT_FOUND", "E_PERMISSION", "E_NETWORK", "E_IO",
    "E_PLATFORM", "E_EXTERNAL_TOOL", "E_PARTIAL_FAILURE", "E_COMMENTS_FOUND",
    "E_CONTRACT_VIOLATION", "E_INTERRUPTED", "E_VERIFICATION_FAILED",
    "E_INTERNAL",
)

SUGGESTIONS = {
    "E_VALIDATION": "fix the arguments per the message",
    "E_NOT_FOUND": "check whether the target path/file exists",
    "E_PERMISSION": "check read/write permission",
    "E_NETWORK": "check the network and retry",
    "E_IO": "check disk and file state",
    "E_PLATFORM": "run on a supported platform",
    "E_EXTERNAL_TOOL": "check the external tool is installed and callable",
    "E_PARTIAL_FAILURE": "inspect error.details for the failed items",
    "E_COMMENTS_FOUND": "remove the new comments and re-run",
    "E_CONTRACT_VIOLATION": "fix the contract violation per error.details",
    "E_INTERRUPTED": "state was preserved; safe to re-run",
    "E_VERIFICATION_FAILED": "inspect error.details for the failed acceptance",
    "E_INTERNAL": "report a bug",
}


def is_known_error_code(code):
    return code in ERROR_CODES


def reconfigure_stdio():
    """Force UTF-8 on the machine channels (JSON / --ai-help)."""
    for stream in (sys.stdout, sys.stderr):
        if stream is None:
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def envelope(ok, data=None, error=None, meta=None):
    return {"ok": ok, "data": data, "error": error, "meta": meta or {}}


def make_error(code, message, details=None, retryable=False, suggestion=None):
    error = {"code": code, "message": message, "retryable": retryable}
    if details is not None:
        error["details"] = details
    if suggestion is not None:
        error["suggestion"] = suggestion
    return error


def emit_result(sink, data, meta=None):
    sink.write(json.dumps(envelope(True, data=data, meta=meta),
                          ensure_ascii=False, indent=2) + "\n")


def emit_error(sink, code, message, details=None, retryable=False,
               suggestion=None):
    sink.write(json.dumps(
        envelope(False, error=make_error(code, message, details=details,
                                         retryable=retryable,
                                         suggestion=suggestion)),
        ensure_ascii=False, indent=2) + "\n")


class Sinks:
    """Injectable text output targets. Never the binary ``.buffer``."""

    __slots__ = ("out", "err")

    def __init__(self, out=None, err=None):
        self.out = out if out is not None else sys.stdout
        self.err = err if err is not None else sys.stderr


def default_sinks():
    return Sinks()


class Context:
    """Business context handed to ``command()``: raw argv, JSON-mode flag and
    the output sinks."""

    __slots__ = ("argv", "json_mode", "sinks")

    def __init__(self, argv, json_mode, sinks):
        self.argv = argv
        self.json_mode = json_mode
        self.sinks = sinks


class CliResult:
    """Business outcome of ``command()``. ``error`` is None on success,
    otherwise a ``(code, message, details, retryable, suggestion)`` tuple.
    ``exit_code`` is an explicit override (e.g. 2 for argument-class errors)."""

    __slots__ = ("data", "meta", "error", "exit_code")

    def __init__(self, data=None, meta=None, error=None, exit_code=None):
        self.data = data
        self.meta = meta
        self.error = error
        self.exit_code = exit_code


def ok(data=None, meta=None):
    return CliResult(data=data, meta=meta)


def fail(code, message, details=None, retryable=False, suggestion=None,
         exit_code=EXIT_FAIL):
    return CliResult(error=(code, message, details, retryable, suggestion),
                     exit_code=exit_code)


class CliError(Exception):
    """Business error carrying a stable machine error code. Classified by
    ``isinstance`` before anything else."""

    def __init__(self, code, message, details=None, retryable=False,
                 suggestion=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details
        self.retryable = retryable
        self.suggestion = suggestion


class ExternalToolError(CliError, RuntimeError):
    """An external command/subprocess failed or is missing.

    Also a RuntimeError so legacy ``except RuntimeError`` call sites keep
    working during the transition."""


class CliUsageError(Exception):
    """Argument/usage error raised by ``ArgumentParser.error()``. The entry
    layer emits it exactly once instead of calling ``sys.exit`` directly."""

    def __init__(self, message, usage="", prog=""):
        super().__init__(message)
        self.message = message
        self.usage = usage
        self.prog = prog


class CliFriendlyParser(argparse.ArgumentParser):
    """``error()`` raises ``CliUsageError`` instead of exiting, so the outer
    layer controls the single emit and exit code."""

    def error(self, message):
        raise CliUsageError(message, usage=self.format_usage(), prog=self.prog)


def json_requested(argv=None):
    """Pre-scan argv for JSON mode. Honours the ``--`` terminator and never
    reads ``sys.argv`` when an explicit argv is passed."""
    if argv is None:
        argv = sys.argv[1:]
    for i, token in enumerate(argv):
        if token == "--":
            break
        if token == "--json":
            return True
        if token == "--format":
            if i + 1 < len(argv) and argv[i + 1] == "json":
                return True
        elif token.startswith("--format=") and token.split("=", 1)[1] == "json":
            return True
    return False


def scan_ai_help(argv, ai_help, out):
    """Eager ``--ai-help`` scan: on a hit, write the help text to ``out`` and
    return True. Honours the ``--`` terminator (an operand after ``--`` must
    not trigger it)."""
    if argv is None:
        argv = sys.argv[1:]
    stopped = False
    for token in argv:
        if stopped:
            break
        if token == "--":
            stopped = True
            continue
        if token == "--ai-help":
            out.write(ai_help if ai_help.endswith("\n") else ai_help + "\n")
            return True
    return False


def classify_exception(exc):
    """Map an exception to ``(error_code, retryable)``. Order: business
    mapping (CliError/ExternalToolError) -> KeyboardInterrupt -> ``__cause__``
    (Permission / FileNotFound / IO) -> E_INTERNAL. SystemExit is never turned
    into E_INTERNAL here."""
    if isinstance(exc, ExternalToolError):
        return "E_EXTERNAL_TOOL", exc.retryable
    if isinstance(exc, CliError):
        return exc.code, exc.retryable
    if isinstance(exc, CliUsageError):
        return "E_VALIDATION", False
    if isinstance(exc, KeyboardInterrupt):
        return "E_INTERRUPTED", True
    cause = exc.__cause__ or exc.__context__
    if isinstance(cause, PermissionError):
        return "E_PERMISSION", False
    if isinstance(cause, FileNotFoundError):
        return "E_NOT_FOUND", False
    if isinstance(cause, (OSError, IOError)):
        return "E_IO", False
    return "E_INTERNAL", False


@contextlib.contextmanager
def _redirect_io(out, err):
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out, err
    try:
        yield
    finally:
        sys.stdout, sys.stderr = old_out, old_err


def _take_log(log_buf):
    if log_buf is None:
        return None
    text = log_buf.getvalue()
    return text if text else None


def _merge_log(details, log_buf):
    details = dict(details) if details else {}
    log = _take_log(log_buf)
    if log:
        details["log"] = log
    return details or None


def main(argv=None, sinks=None, *, command, parser_factory, ai_help,
         prog=None, reconfigure=True):
    """Unified entry: eager ai-help -> JSON pre-scan -> parse -> business ->
    single emit. ``command(argv, context) -> CliResult`` is the business layer
    (no I/O); everything here is emission and mapping. Returns 0/1/2."""
    if argv is None:
        argv = sys.argv[1:]
    if sinks is None:
        sinks = default_sinks()
    if reconfigure:
        reconfigure_stdio()
    if scan_ai_help(argv, ai_help, sinks.out):
        return EXIT_OK
    json_mode = json_requested(argv)
    context = Context(argv=argv, json_mode=json_mode, sinks=sinks)
    log_buf = io.StringIO() if json_mode else None
    try:
        with _redirect_io(log_buf if json_mode else sinks.out, sinks.err):
            result = command(argv, context)
    except CliUsageError as exc:
        if json_mode:
            emit_error(sinks.err, "E_VALIDATION", exc.message,
                       suggestion=SUGGESTIONS.get("E_VALIDATION"))
        else:
            if exc.usage:
                sinks.err.write(exc.usage)
            sinks.err.write("{0}: error: {1}\n".format(
                exc.prog or prog or "cli", exc.message))
        return EXIT_ARG
    except KeyboardInterrupt:
        details = _merge_log(None, log_buf)
        if json_mode:
            emit_error(sinks.err, "E_INTERRUPTED", "interrupted",
                       details=details, retryable=True,
                       suggestion=SUGGESTIONS.get("E_INTERRUPTED"))
        else:
            sinks.err.write("interrupted\n")
        return EXIT_FAIL
    except Exception as exc:
        code, retryable = classify_exception(exc)
        message = str(exc) or type(exc).__name__
        details = None
        if isinstance(exc, CliError) and exc.details:
            details = dict(exc.details)
        if log_buf is not None:
            log = _take_log(log_buf)
            if log:
                details = dict(details) if details else {}
                details["log"] = log
        if json_mode:
            emit_error(sinks.err, code, message, details=details,
                       retryable=retryable, suggestion=SUGGESTIONS.get(code))
        else:
            sinks.err.write(message + "\n")
        return EXIT_FAIL

    if json_mode:
        if result.error is not None:
            code, message, details, retryable, suggestion = result.error
            merged = _merge_log(details, log_buf)
            emit_error(sinks.err, code, message, details=merged,
                       retryable=retryable, suggestion=suggestion)
            return result.exit_code if result.exit_code is not None \
                else EXIT_FAIL
        meta = dict(result.meta) if result.meta else {}
        log = _take_log(log_buf)
        if log:
            meta["log"] = log
        emit_result(sinks.out, result.data, meta=meta)
        return EXIT_OK

    if result.error is not None:
        code, message, _details, _retryable, _suggestion = result.error
        if message:
            sinks.err.write(message + "\n")
        return result.exit_code if result.exit_code is not None else EXIT_FAIL
    return result.exit_code if result.exit_code is not None else EXIT_OK
