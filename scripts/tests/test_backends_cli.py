import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(HERE)
KEIL = os.path.join(SCRIPTS, "Keil2Clangd.py")
IAR = os.path.join(SCRIPTS, "Iar2Clangd.py")
CMAKE = os.path.join(SCRIPTS, "Cmake2Clangd.py")
UVPROJX = os.path.join(HERE, "fixtures", "sample.uvprojx")
EWP = os.path.join(HERE, "fixtures", "sample.ewp")


def run(script, *extra):
    return subprocess.run([sys.executable, script] + list(extra),
                          capture_output=True, text=True, cwd=SCRIPTS,
                          encoding="utf-8")


def load_json(text):
    return json.loads(text)


class BackendCliContract(unittest.TestCase):
    def _single_proj(self, tmp, name="proj.uvprojx"):
        (Path(tmp) / name).write_text(
            Path(UVPROJX).read_text(encoding="utf-8"), encoding="utf-8")

    def _single_ewp(self, tmp, name="proj.ewp"):
        (Path(tmp) / name).write_text(
            Path(EWP).read_text(encoding="utf-8"), encoding="utf-8")


class TestKeil2ClangdCli(BackendCliContract):
    def test_json_success_envelope(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._single_proj(tmp)
            r = run(KEIL, "-a", "-k", "/nonexistent", "--no-exe", "--no-verify",
                    "-p", tmp, "-o", tmp, "--json")
            self.assertEqual(r.returncode, 0, r.stderr)
            obj = load_json(r.stdout)
            self.assertTrue(obj["ok"])
            self.assertIsNone(obj["error"])
            self.assertIn("output_dir", obj["data"])

    def test_ai_help_eager(self):
        r = run(KEIL, "--ai-help")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("name: Keil2Clangd", r.stdout)
        self.assertIn("## Quick Reference", r.stdout)

    def test_bad_arg_json_validation_envelope(self):
        r = run(KEIL, "--definitely-not-an-arg", "--json")
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertEqual(r.stdout, "")
        obj = load_json(r.stderr)
        self.assertFalse(obj["ok"])
        self.assertEqual(obj["error"]["code"], "E_VALIDATION")

    def test_dry_run_json_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._single_proj(tmp)
            r = run(KEIL, "-a", "-k", "/nonexistent", "--no-exe", "--dry-run",
                    "-p", tmp, "-o", tmp, "--json")
            self.assertEqual(r.returncode, 0, r.stderr)
            obj = load_json(r.stdout)
            self.assertTrue(obj["ok"])
            self.assertTrue(obj["data"]["dry_run"])


class TestIar2ClangdCli(BackendCliContract):
    def test_json_success_envelope(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._single_ewp(tmp)
            r = run(IAR, "-a", "--iar-path", "/nonexistent", "--no-probe",
                    "--no-verify", "-c", "Release",
                    "-p", tmp, "-o", tmp, "--json")
            self.assertEqual(r.returncode, 0, r.stderr)
            obj = load_json(r.stdout)
            self.assertTrue(obj["ok"])
            self.assertIsNone(obj["error"])
            self.assertIn("output_dir", obj["data"])

    def test_ai_help_eager(self):
        r = run(IAR, "--ai-help")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("name: Iar2Clangd", r.stdout)

    def test_bad_arg_json_validation_envelope(self):
        r = run(IAR, "--definitely-not-an-arg", "--json")
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertEqual(r.stdout, "")
        obj = load_json(r.stderr)
        self.assertEqual(obj["error"]["code"], "E_VALIDATION")

    def test_dry_run_json_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._single_ewp(tmp)
            r = run(IAR, "-a", "--iar-path", "/nonexistent", "--no-probe",
                    "--dry-run", "-c", "Release", "-p", tmp, "-o", tmp, "--json")
            self.assertEqual(r.returncode, 0, r.stderr)
            obj = load_json(r.stdout)
            self.assertTrue(obj["ok"])
            self.assertTrue(obj["data"]["dry_run"])


class TestCmake2ClangdCli(BackendCliContract):
    def test_not_found_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = run(CMAKE, "-p", tmp, "--json")
            self.assertEqual(r.returncode, 1, r.stdout)
            self.assertEqual(r.stdout, "")
            obj = load_json(r.stderr)
            self.assertFalse(obj["ok"])
            self.assertEqual(obj["error"]["code"], "E_NOT_FOUND")

    def test_ai_help_eager(self):
        r = run(CMAKE, "--ai-help")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("name: Cmake2Clangd", r.stdout)

    def test_bad_arg_json_validation_envelope(self):
        r = run(CMAKE, "--definitely-not-an-arg", "--json")
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertEqual(r.stdout, "")
        obj = load_json(r.stderr)
        self.assertEqual(obj["error"]["code"], "E_VALIDATION")


if __name__ == "__main__":
    unittest.main()
