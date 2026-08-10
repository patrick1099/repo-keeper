"""Tests for the hidden-macro scan.

The shape to defend: include guards must not drown the report, macros a header
defines for itself must be separated from genuinely unresolved ones, and GB2312
comments must not derail the parse.
"""

import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import k2c_macroscan as macroscan  # noqa: E402


def write(tmp, name, text, encoding="utf-8"):
    p = Path(tmp) / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding=encoding)
    return p


class TestScanSources(unittest.TestCase):
    def test_include_guard_is_not_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = write(tmp, "a.h", "#ifndef __A_H__\n#define __A_H__\nint a;\n#endif\n")
            tested, defined = macroscan.scan_sources([f])
            self.assertNotIn("__A_H__", tested)
            self.assertIn("__A_H__", defined)

    def test_guard_with_a_blank_line_still_recognised(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = write(tmp, "a.h", "#ifndef G\n\n#define G\n#endif\n")
            tested, _ = macroscan.scan_sources([f])
            self.assertNotIn("G", tested)

    def test_ifndef_used_as_a_real_switch_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = write(tmp, "a.c", "#ifndef NO_FEATURE\nvoid f(void){}\n#endif\n")
            tested, _ = macroscan.scan_sources([f])
            self.assertIn("NO_FEATURE", tested)

    def test_ifdef_and_defined_forms_all_counted(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = write(tmp, "a.c",
                      "#ifdef ALPHA\n#endif\n"
                      "#if defined(BETA) && defined GAMMA\n#endif\n"
                      "#if 0\n#elif defined ( DELTA )\n#endif\n"
                      "#ifdef ALPHA\n#endif\n")
            tested, _ = macroscan.scan_sources([f])
            self.assertEqual(set(tested), {"ALPHA", "BETA", "GAMMA", "DELTA"})
            self.assertEqual(tested["ALPHA"].count, 2)

    def test_first_location_is_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = write(tmp, "a.c", "\n\n#ifdef LATE\n#endif\n")
            tested, _ = macroscan.scan_sources([f])
            self.assertEqual(tested["LATE"].first_line, 3)
            self.assertEqual(tested["LATE"].first_file, f)

    def test_gb2312_comments_do_not_break_parsing(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = write(tmp, "a.c",
                      "/* 中文注释：这是一个开关 */\n#ifdef DEF_XPQZ\n#endif\n",
                      encoding="gb2312")
            tested, _ = macroscan.scan_sources([f])
            self.assertIn("DEF_XPQZ", tested)


class TestClassify(unittest.TestCase):
    def test_three_way_split(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = write(tmp, "a.c",
                      "#ifdef __LG048\n#endif\n"          # known: a target defines it
                      "#ifdef FL_ADC_DRIVER_ENABLED\n#endif\n"   # self-defined below
                      "#define FL_ADC_DRIVER_ENABLED\n"
                      "#ifdef DEF_RCGHDZ\n#endif\n")      # unresolved
            tested, defined = macroscan.scan_sources([f])
            unresolved, self_defined = macroscan.classify(
                tested, defined, {"__LG048"})
            self.assertEqual([u.name for u in unresolved], ["DEF_RCGHDZ"])
            self.assertEqual([u.name for u in self_defined],
                             ["FL_ADC_DRIVER_ENABLED"])

    def test_value_macros_match_by_name(self):
        self.assertEqual(macroscan.macro_names(["FOO=1", " BAR ", ""]),
                         {"FOO", "BAR"})

    def test_names_from_probe_define_lines(self):
        self.assertEqual(
            macroscan.names_from_define_lines(
                ["#define __ICCRL78__ 1", "#define __CORE__ 2", "garbage"]),
            {"__ICCRL78__", "__CORE__"})

    def test_language_standard_probes_are_not_findings(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = write(tmp, "a.h",
                      "#ifdef __cplusplus\nextern \"C\" {\n#endif\n"
                      "#ifdef REAL_SWITCH\n#endif\n")
            tested, defined = macroscan.scan_sources([f])
            unresolved, _ = macroscan.classify(tested, defined, set())
            self.assertEqual([u.name for u in unresolved], ["REAL_SWITCH"])

    def test_unresolved_sorted_by_frequency(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = write(tmp, "a.c",
                      "#ifdef RARE\n#endif\n"
                      "#ifdef COMMON\n#endif\n#ifdef COMMON\n#endif\n")
            tested, defined = macroscan.scan_sources([f])
            unresolved, _ = macroscan.classify(tested, defined, set())
            self.assertEqual([u.name for u in unresolved], ["COMMON", "RARE"])


class TestReport(unittest.TestCase):
    def test_report_returns_unresolved_names_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = write(tmp, "a.c",
                      "#ifndef __A_H__\n#define __A_H__\n#endif\n"
                      "#ifdef KNOWN\n#endif\n"
                      "#ifdef MYSTERY\n#endif\n")
            names = macroscan.report([f], {"KNOWN"}, base_dir=tmp)
            self.assertEqual(names, ["MYSTERY"])

    def test_missing_files_are_skipped(self):
        self.assertEqual(macroscan.report([Path("nope/absent.c")], set()), [])


if __name__ == "__main__":
    unittest.main()
