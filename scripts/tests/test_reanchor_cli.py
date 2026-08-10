import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
REANCHOR = SCRIPTS / "ReAnchor.py"

CLANGD = """CompileFlags:
  Add:
    # AI-added compat macro (must survive)
    - -D__weak=__attribute__((weak))
    - -IApp/Code
    - -ID:/OldKeil/ARM/ARMCLANG/include
  Remove:
    - -W*
"""


def run_cli(*argv):
    return subprocess.run([sys.executable, str(REANCHOR)] + list(argv),
                          capture_output=True, text=True, stdin=subprocess.DEVNULL)


class TestReanchorCli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.proj = base / "proj"
        self.proj.mkdir()
        self.keil = base / "Keil_v5"
        (self.keil / "ARM" / "ARMCLANG" / "include").mkdir(parents=True)
        (self.proj / ".clangd").write_text(CLANGD, encoding="utf-8")
        # The listed source must really exist: ReAnchor refuses a database whose
        # file list does not match the tree it sits in.
        (self.proj / "App").mkdir()
        (self.proj / "App" / "main.c").write_text("int main(void){return 0;}\n",
                                                  encoding="utf-8")
        args = ["arm-none-eabi-gcc", "-c", "App/main.c",
                "-IApp/Code", "-ID:/OldKeil/ARM/ARMCLANG/include"]
        entries = [{"command": " ".join(args), "arguments": args,
                    "directory": "C:/old-machine/Proj/Code", "file": "App/main.c"}]
        (self.proj / "compile_commands.json").write_text(
            json.dumps(entries, indent=4), encoding="utf-8")
        self.new_root = str(self.proj).replace("\\", "/")
        self.new_inc = str(self.keil / "ARM/ARMCLANG/include").replace("\\", "/")

    def tearDown(self):
        self.tmp.cleanup()

    def test_full_reanchor_with_explicit_keil(self):
        r = run_cli("--root", str(self.proj), "--keil-path", str(self.keil))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        cc = json.loads((self.proj / "compile_commands.json").read_text(encoding="utf-8"))
        self.assertEqual(cc[0]["directory"], self.new_root)
        self.assertIn(f"-I{self.new_inc}", cc[0]["arguments"])
        text = (self.proj / ".clangd").read_text(encoding="utf-8")
        self.assertIn(f"-I{self.new_inc}", text)
        self.assertIn("- -D__weak=__attribute__((weak))", text)
        self.assertTrue((self.proj / ".clangd.bak").exists())
        self.assertTrue((self.proj / "compile_commands.json.bak").exists())

    def test_dry_run_writes_nothing(self):
        before = (self.proj / ".clangd").read_text(encoding="utf-8")
        r = run_cli("--root", str(self.proj), "--keil-path", str(self.keil), "--dry-run")
        self.assertEqual(r.returncode, 0)
        self.assertEqual((self.proj / ".clangd").read_text(encoding="utf-8"), before)
        self.assertFalse((self.proj / ".clangd.bak").exists())

    def test_no_dead_paths_zero_interaction(self):
        # make every absolute path alive: re-anchor once, then run again w/o keil-path
        run_cli("--root", str(self.proj), "--keil-path", str(self.keil))
        r = run_cli("--root", str(self.proj))  # stdin closed; would hang/EOF if probed
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("Keil", r.stdout.replace("OldKeil", ""))

    def test_unmappable_dead_path_kept_with_warning(self):
        # Suffix that exists under NO Keil install (even the real machine one),
        # so no matter what the probe finds, this path stays dead -> kept + warn.
        dead = "D:/OldKeil/ARM/NOSUCH_XYZ/include"
        args = ["arm-none-eabi-gcc", "-c", "App/main.c", f"-I{dead}"]
        entries = [{"command": " ".join(args), "arguments": args,
                    "directory": "C:/old-machine/Proj/Code", "file": "App/main.c"}]
        (self.proj / "compile_commands.json").write_text(
            json.dumps(entries, indent=4), encoding="utf-8")
        r = run_cli("--root", str(self.proj), "--keil-path", str(self.keil))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        cc = json.loads((self.proj / "compile_commands.json").read_text(encoding="utf-8"))
        self.assertEqual(cc[0]["directory"], self.new_root)   # directory still fixed
        self.assertIn(f"-I{dead}", cc[0]["arguments"])        # dead -I kept
        self.assertIn("WARNING", r.stdout)

    def test_missing_both_files_errors(self):
        empty = Path(self.tmp.name) / "empty"
        empty.mkdir()
        r = run_cli("--root", str(empty))
        self.assertEqual(r.returncode, 1)

    def test_truncated_json_errors_cleanly(self):
        # Malformed JSON must not crash past the exe's Enter-to-exit pause.
        (self.proj / "compile_commands.json").write_text(
            '[{"directory": "C:/x", "file"', encoding="utf-8")
        r = run_cli("--root", str(self.proj))
        self.assertEqual(r.returncode, 1)
        combined = r.stdout + r.stderr
        self.assertNotIn("Traceback", combined)
        self.assertIn("ERROR", combined)

    def test_object_shaped_json_errors_cleanly(self):
        # Valid JSON that parses but is not a list (e.g. an accidentally-saved
        # single object) must not reach entry.get('directory') on a str key.
        (self.proj / "compile_commands.json").write_text("{}", encoding="utf-8")
        r = run_cli("--root", str(self.proj))
        self.assertEqual(r.returncode, 1)
        combined = r.stdout + r.stderr
        self.assertNotIn("Traceback", combined)
        self.assertIn("ERROR", combined)

    def test_bom_prefixed_json_is_accepted(self):
        # An editor (Notepad, PowerShell's Set-Content -Encoding utf8) can save a
        # perfectly valid compile_commands.json with a UTF-8 BOM. Reading it as
        # plain utf-8 would reject it; utf-8-sig accepts BOM and BOM-less alike.
        cc = self.proj / "compile_commands.json"
        cc.write_text(cc.read_text(encoding="utf-8"), encoding="utf-8-sig")
        r = run_cli("--root", str(self.proj), "--keil-path", str(self.keil))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        entries = json.loads(cc.read_text(encoding="utf-8"))
        self.assertEqual(entries[0]["directory"], self.new_root)
        self.assertIn("-I" + self.new_inc, entries[0]["arguments"])


class TestReanchorCliWriteFailure(unittest.TestCase):
    """compile_commands.json is read-only (simulates locked-by-editor / permission
    denied). _backup() succeeds (copy, not open-for-write of the original), then the
    real write must fail cleanly through _finish() instead of a bare traceback."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.proj = Path(self.tmp.name) / "proj"
        self.proj.mkdir()
        (self.proj / "App").mkdir()
        (self.proj / "App" / "main.c").write_text("int main(void){return 0;}\n",
                                                  encoding="utf-8")
        # No absolute toolchain -I here: only the 'directory' mismatch triggers a
        # write, so no Keil auto-probe (and no stdin interaction) is involved.
        entries = [{"command": "arm-none-eabi-gcc -c App/main.c -IApp/Code",
                    "arguments": ["arm-none-eabi-gcc", "-c", "App/main.c", "-IApp/Code"],
                    "directory": "C:/Old/Dir", "file": "App/main.c"}]
        self.cc_path = self.proj / "compile_commands.json"
        self.cc_path.write_text(json.dumps(entries, indent=4), encoding="utf-8")
        os.chmod(str(self.cc_path), stat.S_IREAD)

    def tearDown(self):
        os.chmod(str(self.cc_path), stat.S_IWRITE)
        self.tmp.cleanup()

    def test_write_failure_returns_error_and_keeps_backup(self):
        r = run_cli("--root", str(self.proj))
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        combined = r.stdout + r.stderr
        self.assertNotIn("Traceback", combined)
        self.assertIn("ERROR", combined)
        self.assertTrue((self.proj / "compile_commands.json.bak").exists())


if __name__ == "__main__":
    unittest.main()
