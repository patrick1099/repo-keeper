import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import Proj2Clangd as detect_mod


def _touch(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")


class TestDetect(unittest.TestCase):
    def test_keil(self):
        with tempfile.TemporaryDirectory() as tmp:
            _touch(Path(tmp) / "Proj" / "app.uvprojx")
            self.assertEqual([k for k, _, _ in detect_mod.detect(tmp)], ["keil"])

    def test_iar(self):
        with tempfile.TemporaryDirectory() as tmp:
            _touch(Path(tmp) / "Proj" / "app.ewp")
            self.assertEqual([k for k, _, _ in detect_mod.detect(tmp)], ["iar"])

    def test_cmake(self):
        with tempfile.TemporaryDirectory() as tmp:
            _touch(Path(tmp) / "CMakeLists.txt")
            self.assertEqual([k for k, _, _ in detect_mod.detect(tmp)], ["cmake"])

    def test_vendor_project_listed_before_cmake(self):
        with tempfile.TemporaryDirectory() as tmp:
            _touch(Path(tmp) / "app.ewp")
            _touch(Path(tmp) / "CMakeLists.txt")
            self.assertEqual([k for k, _, _ in detect_mod.detect(tmp)],
                             ["iar", "cmake"])

    def test_build_scaffolding_ignored(self):
        # A CMakeLists.txt fetched into _deps or emitted under CMakeFiles is not
        # this project.
        with tempfile.TemporaryDirectory() as tmp:
            _touch(Path(tmp) / "build" / "_deps" / "dep-src" / "CMakeLists.txt")
            _touch(Path(tmp) / "build" / "CMakeFiles" / "CMakeLists.txt")
            self.assertEqual(detect_mod.detect(tmp), [])

    def test_nothing_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(detect_mod.detect(tmp), [])


class TestCli(unittest.TestCase):
    def test_detect_only_succeeds_when_something_is_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            _touch(Path(tmp) / "app.ewp")
            self.assertEqual(detect_mod.main(["-p", tmp, "--detect-only"]), 0)

    def test_detect_only_fails_on_empty_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(detect_mod.main(["-p", tmp, "--detect-only"]), 1)

    def test_ambiguous_tree_refuses_to_guess(self):
        with tempfile.TemporaryDirectory() as tmp:
            _touch(Path(tmp) / "app.ewp")
            _touch(Path(tmp) / "CMakeLists.txt")
            self.assertEqual(detect_mod.main(["-p", tmp]), 2)

    def test_forced_kind_reaches_the_backend(self):
        with tempfile.TemporaryDirectory() as tmp:
            _touch(Path(tmp) / "CMakeLists.txt")
            _touch(Path(tmp) / "app.ewp")
            # cmake backend with no database -> its own error path, not the
            # ambiguity error, proving the dispatch happened
            self.assertEqual(
                detect_mod.main(["-p", tmp, "--kind", "cmake", "--no-configure"]), 1)


P2C = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))) / "Proj2Clangd.py"


def run_cli(*argv):
    return subprocess.run([sys.executable, str(P2C)] + list(argv),
                          capture_output=True, text=True, stdin=subprocess.DEVNULL)


class TestJsonCli(unittest.TestCase):
    def test_detect_only_json_envelope(self):
        with tempfile.TemporaryDirectory() as tmp:
            _touch(Path(tmp) / "app.ewp")
            r = run_cli("-p", tmp, "--detect-only", "--json")
            self.assertEqual(r.returncode, 0, r.stderr)
            obj = json.loads(r.stdout)
            self.assertTrue(obj["ok"])
            self.assertEqual(obj["data"]["detected"][0]["kind"], "iar")

    def test_no_project_json_envelope(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = run_cli("-p", tmp, "--json")
            self.assertEqual(r.returncode, 1, r.stdout)
            self.assertEqual(r.stdout, "")
            obj = json.loads(r.stderr)
            self.assertFalse(obj["ok"])
            self.assertEqual(obj["error"]["code"], "E_NOT_FOUND")

    def test_ambiguous_json_envelope(self):
        with tempfile.TemporaryDirectory() as tmp:
            _touch(Path(tmp) / "app.ewp")
            _touch(Path(tmp) / "CMakeLists.txt")
            r = run_cli("-p", tmp, "--json")
            self.assertEqual(r.returncode, 2, r.stdout)
            self.assertEqual(r.stdout, "")
            obj = json.loads(r.stderr)
            self.assertEqual(obj["error"]["code"], "E_VALIDATION")

    def test_bad_kind_json_envelope(self):
        r = run_cli("--kind", "bogus", "--json")
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertEqual(r.stdout, "")
        obj = json.loads(r.stderr)
        self.assertEqual(obj["error"]["code"], "E_VALIDATION")

    def test_ai_help_eager(self):
        r = run_cli("--ai-help")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("name: Proj2Clangd", r.stdout)
        self.assertIn("## Quick Reference", r.stdout)

    def test_format_json_equiv(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = run_cli("-p", tmp, "--format", "json")
            self.assertEqual(r.returncode, 1)
            obj = json.loads(r.stderr)
            self.assertEqual(obj["error"]["code"], "E_NOT_FOUND")


if __name__ == "__main__":
    unittest.main()
