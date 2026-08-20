import io
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import cli_common as cc


SAMPLE_HELP = """---
name: sample
description: probe tool for the kernel tests
ai_help_version: 0.1.0
---

## Quick Reference

- run me

## When to Use

Use this tool when asked to.

## Side Effects & Safety

Reads nothing, writes nothing.

## Exit Codes

0 / 1 / 2

## Errors & Recovery

see message
"""


def _sample_parser():
    p = cc.CliFriendlyParser(prog="sample")
    p.add_argument("--json", action="store_true")
    p.add_argument("--format", choices=("json",), default="json")
    p.add_argument("--ai-help", action="store_true")
    p.add_argument("--path")
    p.add_argument("--dry-run", action="store_true")
    return p


def _sample_command(argv, context):
    args = _sample_parser().parse_args(argv)
    if not args.path:
        return cc.fail("E_VALIDATION", "path required", exit_code=cc.EXIT_ARG)
    if args.path == "missing":
        return cc.fail("E_NOT_FOUND", "not found", details={"path": args.path})
    data = {"path": args.path}
    if args.dry_run:
        data["dry_run"] = True
    return cc.ok(data)


def _sample_main(argv, sinks=None):
    return cc.main(argv, sinks, command=_sample_command,
                   parser_factory=_sample_parser, ai_help=SAMPLE_HELP,
                   prog="sample", reconfigure=False)


def _run_sample(*argv):
    out, err = io.StringIO(), io.StringIO()
    sinks = cc.Sinks(out=out, err=err)
    code = _sample_main(list(argv), sinks)
    return code, out.getvalue(), err.getvalue()


def _load(text):
    return json.loads(text)


class TestEnvelopeHelpers(unittest.TestCase):
    def test_success_envelope_shape(self):
        obj = cc.envelope(True, data={"a": 1})
        self.assertEqual(obj["ok"], True)
        self.assertEqual(obj["data"], {"a": 1})
        self.assertIsNone(obj["error"])
        self.assertEqual(obj["meta"], {})

    def test_error_envelope_has_required_fields(self):
        obj = cc.envelope(False, error=cc.make_error("E_NOT_FOUND", "gone"))
        err = obj["error"]
        self.assertEqual(err["code"], "E_NOT_FOUND")
        self.assertIn("message", err)
        self.assertIn("retryable", err)
        self.assertIsNone(obj["data"])

    def test_make_error_defaults(self):
        err = cc.make_error("E_IO", "x")
        self.assertEqual(err["retryable"], False)
        self.assertNotIn("details", err)
        self.assertNotIn("suggestion", err)

    def test_emit_result_writes_single_json(self):
        sink = io.StringIO()
        cc.emit_result(sink, {"x": 1}, meta={"m": 1})
        obj = _load(sink.getvalue())
        self.assertTrue(obj["ok"])
        self.assertEqual(obj["meta"], {"m": 1})

    def test_emit_error_writes_envelope(self):
        sink = io.StringIO()
        cc.emit_error(sink, "E_VALIDATION", "bad", suggestion="fix it")
        obj = _load(sink.getvalue())
        self.assertFalse(obj["ok"])
        self.assertEqual(obj["error"]["code"], "E_VALIDATION")
        self.assertEqual(obj["error"]["suggestion"], "fix it")

    def test_emit_output_is_utf8_friendly(self):
        sink = io.StringIO()
        cc.emit_result(sink, {"msg": "仓库中文"})
        self.assertIn("仓库中文", sink.getvalue())


class TestJsonRequested(unittest.TestCase):
    def test_plain_json_flag(self):
        self.assertTrue(cc.json_requested(["--json", "x"]))
        self.assertTrue(cc.json_requested(["x", "--json"]))

    def test_format_json_space(self):
        self.assertTrue(cc.json_requested(["--format", "json", "x"]))

    def test_format_json_equals(self):
        self.assertTrue(cc.json_requested(["--format=json", "x"]))

    def test_no_json(self):
        self.assertFalse(cc.json_requested(["x", "-p", "."]))
        self.assertFalse(cc.json_requested([]))

    def test_format_not_json(self):
        self.assertFalse(cc.json_requested(["--format", "text"]))

    def test_terminator_stops_the_scan(self):
        self.assertFalse(cc.json_requested(["--", "--json"]))
        self.assertFalse(cc.json_requested(["-p", "x", "--", "--json"]))

    def test_uses_explicit_argv_not_sys_argv(self):
        old = sys.argv
        sys.argv = ["prog", "--json"]
        try:
            self.assertFalse(cc.json_requested([]))
            self.assertTrue(cc.json_requested(["--json"]))
        finally:
            sys.argv = old


class TestScanAiHelp(unittest.TestCase):
    def test_eager_hit(self):
        out = io.StringIO()
        self.assertTrue(cc.scan_ai_help(["--ai-help"], SAMPLE_HELP, out))
        self.assertIn("name: sample", out.getvalue())

    def test_hit_even_with_bad_arg_before(self):
        out = io.StringIO()
        self.assertTrue(cc.scan_ai_help(["--bad", "--ai-help"], SAMPLE_HELP, out))
        self.assertIn("name: sample", out.getvalue())

    def test_terminator_disables(self):
        out = io.StringIO()
        self.assertFalse(cc.scan_ai_help(["--", "--ai-help"], SAMPLE_HELP, out))
        self.assertEqual(out.getvalue(), "")

    def test_absent_returns_false(self):
        out = io.StringIO()
        self.assertFalse(cc.scan_ai_help(["-p", "."], SAMPLE_HELP, out))


class TestClassifyException(unittest.TestCase):
    def test_external_tool_wins_over_file_not_found_cause(self):
        exc = cc.ExternalToolError("E_EXTERNAL_TOOL", "git missing")
        try:
            try:
                raise FileNotFoundError("git")
            except FileNotFoundError as cause:
                raise exc from cause
        except cc.ExternalToolError as raised:
            code, retryable = cc.classify_exception(raised)
        self.assertEqual(code, "E_EXTERNAL_TOOL")
        self.assertFalse(retryable)

    def test_cli_error_code_passthrough(self):
        exc = cc.CliError("E_PLATFORM", "unsupported")
        self.assertEqual(cc.classify_exception(exc)[0], "E_PLATFORM")

    def test_keyboard_interrupt(self):
        code, retryable = cc.classify_exception(KeyboardInterrupt())
        self.assertEqual(code, "E_INTERRUPTED")
        self.assertTrue(retryable)

    def test_file_not_found_cause(self):
        try:
            try:
                raise FileNotFoundError("x")
            except FileNotFoundError as cause:
                raise ValueError("wrapped") from cause
        except ValueError as exc:
            code, _ = cc.classify_exception(exc)
        self.assertEqual(code, "E_NOT_FOUND")

    def test_permission_cause(self):
        try:
            try:
                raise PermissionError("denied")
            except PermissionError as cause:
                raise ValueError("wrapped") from cause
        except ValueError as exc:
            code, _ = cc.classify_exception(exc)
        self.assertEqual(code, "E_PERMISSION")

    def test_io_cause(self):
        try:
            try:
                raise OSError("disk")
            except OSError as cause:
                raise ValueError("wrapped") from cause
        except ValueError as exc:
            code, _ = cc.classify_exception(exc)
        self.assertEqual(code, "E_IO")

    def test_unknown_is_internal(self):
        self.assertEqual(cc.classify_exception(ValueError("boom"))[0],
                         "E_INTERNAL")

    def test_system_exit_propagates_not_mapped(self):
        def exit_command(argv, context):
            raise SystemExit(3)

        out, err = io.StringIO(), io.StringIO()
        with self.assertRaises(SystemExit):
            cc.main(["--json"], cc.Sinks(out=out, err=err),
                    command=exit_command, parser_factory=_sample_parser,
                    ai_help=SAMPLE_HELP, prog="sample", reconfigure=False)


class TestDriverSuccess(unittest.TestCase):
    def test_json_success_envelope(self):
        code, out, err = _run_sample("--path", "x", "--json")
        self.assertEqual(code, 0)
        obj = _load(out)
        self.assertTrue(obj["ok"])
        self.assertEqual(obj["data"]["path"], "x")
        self.assertIsNone(obj["error"])
        self.assertEqual(err, "")

    def test_human_success_no_envelope(self):
        code, out, err = _run_sample("--path", "x")
        self.assertEqual(code, 0)
        self.assertEqual(out, "")
        self.assertEqual(err, "")

    def test_dry_run_json_marks_dry_run(self):
        code, out, err = _run_sample("--path", "x", "--dry-run", "--json")
        self.assertEqual(code, 0)
        self.assertTrue(_load(out)["data"]["dry_run"])

    def test_format_json_equiv(self):
        code, out, err = _run_sample("--format", "json", "--path", "x")
        self.assertEqual(code, 0)
        self.assertTrue(_load(out)["ok"])


class TestDriverFailure(unittest.TestCase):
    def test_json_failure_envelope_on_stderr(self):
        code, out, err = _run_sample("--path", "missing", "--json")
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        obj = _load(err)
        self.assertFalse(obj["ok"])
        self.assertEqual(obj["error"]["code"], "E_NOT_FOUND")
        self.assertEqual(obj["error"]["details"]["path"], "missing")

    def test_bad_arg_json_validation_envelope(self):
        code, out, err = _run_sample("--bad-arg", "--json")
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        obj = _load(err)
        self.assertFalse(obj["ok"])
        self.assertEqual(obj["error"]["code"], "E_VALIDATION")

    def test_bad_arg_human_text(self):
        code, out, err = _run_sample("--bad-arg")
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("error:", err)
        with self.assertRaises(ValueError):
            json.loads(err)

    def test_missing_required_human_validation(self):
        code, out, err = _run_sample()
        self.assertEqual(code, 2)
        self.assertEqual(out, "")

    def test_format_json_equiv_bad_arg(self):
        code, out, err = _run_sample("--format", "json", "--bad-arg")
        self.assertEqual(code, 2)
        obj = _load(err)
        self.assertEqual(obj["error"]["code"], "E_VALIDATION")


class TestDriverAiHelp(unittest.TestCase):
    def test_ai_help_ok(self):
        code, out, err = _run_sample("--ai-help")
        self.assertEqual(code, 0)
        self.assertIn("name: sample", out)
        self.assertEqual(err, "")

    def test_ai_help_is_eager(self):
        code, out, err = _run_sample("--bad-arg", "--ai-help")
        self.assertEqual(code, 0)
        self.assertIn("name: sample", out)

    def test_ai_help_respects_terminator(self):
        code, out, err = _run_sample("--", "--ai-help")
        self.assertEqual(code, 2)
        self.assertNotIn("name: sample", out)


class TestDriverExceptionMapping(unittest.TestCase):
    def test_internal_error_maps_to_e_internal(self):
        def boom_command(argv, context):
            raise ValueError("unexpected")

        out, err = io.StringIO(), io.StringIO()
        code = cc.main(["--json"], cc.Sinks(out=out, err=err),
                       command=boom_command, parser_factory=_sample_parser,
                       ai_help=SAMPLE_HELP, prog="sample", reconfigure=False)
        self.assertEqual(code, 1)
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(_load(err.getvalue())["error"]["code"], "E_INTERNAL")

    def test_internal_error_human_has_no_traceback(self):
        def boom_command(argv, context):
            raise ValueError("unexpected")

        out, err = io.StringIO(), io.StringIO()
        code = cc.main([], cc.Sinks(out=out, err=err),
                       command=boom_command, parser_factory=_sample_parser,
                       ai_help=SAMPLE_HELP, prog="sample", reconfigure=False)
        self.assertEqual(code, 1)
        self.assertNotIn("Traceback", err.getvalue())
        self.assertIn("unexpected", err.getvalue())

    def test_external_tool_error_maps_to_e_external_tool(self):
        def boom_command(argv, context):
            raise cc.ExternalToolError("E_EXTERNAL_TOOL", "git failed",
                                       details={"tool": "git"})

        out, err = io.StringIO(), io.StringIO()
        code = cc.main(["--json"], cc.Sinks(out=out, err=err),
                       command=boom_command, parser_factory=_sample_parser,
                       ai_help=SAMPLE_HELP, prog="sample", reconfigure=False)
        self.assertEqual(code, 1)
        obj = _load(err.getvalue())
        self.assertEqual(obj["error"]["code"], "E_EXTERNAL_TOOL")
        self.assertEqual(obj["error"]["details"]["tool"], "git")

    def test_keyboard_interrupt_maps_to_e_interrupted(self):
        def boom_command(argv, context):
            raise KeyboardInterrupt()

        out, err = io.StringIO(), io.StringIO()
        code = cc.main(["--json"], cc.Sinks(out=out, err=err),
                       command=boom_command, parser_factory=_sample_parser,
                       ai_help=SAMPLE_HELP, prog="sample", reconfigure=False)
        self.assertEqual(code, 1)
        self.assertEqual(_load(err.getvalue())["error"]["code"],
                         "E_INTERRUPTED")


class TestProgressBuffering(unittest.TestCase):
    def test_success_progress_lands_in_meta(self):
        def printing_command(argv, context):
            print("progress line")
            return cc.ok({"x": 1})

        out, err = io.StringIO(), io.StringIO()
        code = cc.main(["--json"], cc.Sinks(out=out, err=err),
                       command=printing_command, parser_factory=_sample_parser,
                       ai_help=SAMPLE_HELP, prog="sample", reconfigure=False)
        self.assertEqual(code, 0)
        obj = _load(out.getvalue())
        self.assertIn("progress line", obj["meta"]["log"])
        self.assertNotIn("progress line", out.getvalue().replace(
            "progress line", "", 1))

    def test_failure_progress_lands_in_details(self):
        def printing_command(argv, context):
            print("partial output")
            return cc.fail("E_IO", "write failed")

        out, err = io.StringIO(), io.StringIO()
        code = cc.main(["--json"], cc.Sinks(out=out, err=err),
                       command=printing_command, parser_factory=_sample_parser,
                       ai_help=SAMPLE_HELP, prog="sample", reconfigure=False)
        self.assertEqual(code, 1)
        self.assertEqual(out.getvalue(), "")
        obj = _load(err.getvalue())
        self.assertIn("partial output", obj["error"]["details"]["log"])

    def test_human_mode_progress_goes_to_sink(self):
        def printing_command(argv, context):
            print("human output")
            return cc.ok({"x": 1})

        out, err = io.StringIO(), io.StringIO()
        code = cc.main([], cc.Sinks(out=out, err=err),
                       command=printing_command, parser_factory=_sample_parser,
                       ai_help=SAMPLE_HELP, prog="sample", reconfigure=False)
        self.assertEqual(code, 0)
        self.assertIn("human output", out.getvalue())


class TestErrorCodeTable(unittest.TestCase):
    def test_all_table_codes_are_known(self):
        for code in ("E_VALIDATION", "E_NOT_FOUND", "E_PERMISSION",
                     "E_NETWORK", "E_IO", "E_PLATFORM", "E_EXTERNAL_TOOL",
                     "E_PARTIAL_FAILURE", "E_COMMENTS_FOUND",
                     "E_CONTRACT_VIOLATION", "E_INTERRUPTED",
                     "E_VERIFICATION_FAILED", "E_INTERNAL"):
            self.assertTrue(cc.is_known_error_code(code), code)

    def test_unknown_code_rejected(self):
        self.assertFalse(cc.is_known_error_code("E_BOGUS"))

    def test_every_table_code_has_a_suggestion(self):
        for code in cc.ERROR_CODES:
            self.assertIn(code, cc.SUGGESTIONS, code)


class TestUsageErrorNoExit(unittest.TestCase):
    def test_parse_error_raises_not_exits(self):
        parser = _sample_parser()
        with self.assertRaises(cc.CliUsageError):
            parser.parse_args(["--bad-arg"])
        try:
            parser.parse_args(["--bad-arg"])
        except cc.CliUsageError as exc:
            self.assertIn("usage:", exc.usage)


if __name__ == "__main__":
    unittest.main()
