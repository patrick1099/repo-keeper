"""Regressions for ReAnchor's two new guards.

Ownership: ReAnchor rewrites paths but never the file list, so a database
copied in from a different project used to be re-anchored "successfully" while
describing files that do not exist. It now refuses.

Discovery: the exe is meant to sit at the project root, but a Keil project
keeps its config in Proj/ several levels down and one repo can hold several
projects. The search therefore runs downwards -- and each config must be
anchored to its OWN directory, never to the search root.
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
REANCHOR = SCRIPTS / "ReAnchor.py"


def run_cli(*argv):
    return subprocess.run([sys.executable, str(REANCHOR)] + list(argv),
                          capture_output=True, text=True, stdin=subprocess.DEVNULL)


def write_db(config_dir, files, directory="C:/somewhere/else"):
    entries = []
    for f in files:
        args = ["arm-none-eabi-gcc", "-c", f, "-IApp/Code"]
        entries.append({"command": " ".join(args), "arguments": args,
                        "directory": directory, "file": f})
    (config_dir / "compile_commands.json").write_text(
        json.dumps(entries, indent=4), encoding="utf-8")
    return entries


def make_sources(root, files):
    for f in files:
        p = root / f
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("/* x */\n", encoding="utf-8")


class TestOwnership(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.proj = Path(self.tmp.name) / "proj"
        self.proj.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_foreign_database_is_refused_and_nothing_written(self):
        write_db(self.proj, ["App/a.c", "App/b.c", "App/c.c"])
        before = (self.proj / "compile_commands.json").read_text(encoding="utf-8")
        r = run_cli("--root", str(self.proj))
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("does not belong to this project", r.stdout)
        self.assertEqual((self.proj / "compile_commands.json").read_text(encoding="utf-8"),
                         before)
        self.assertFalse((self.proj / "compile_commands.json.bak").exists())

    def test_force_overrides_the_refusal(self):
        write_db(self.proj, ["App/a.c", "App/b.c", "App/c.c"])
        r = run_cli("--root", str(self.proj), "--force")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        cc = json.loads((self.proj / "compile_commands.json").read_text(encoding="utf-8"))
        self.assertEqual(cc[0]["directory"], str(self.proj).replace("\\", "/"))

    def test_a_few_deleted_files_are_tolerated(self):
        files = ["App/f{0}.c".format(i) for i in range(20)]
        write_db(self.proj, files)
        make_sources(self.proj, files[:19])          # one file deleted since generation
        r = run_cli("--root", str(self.proj))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("does not belong", r.stdout)

    def test_threshold_is_configurable(self):
        files = ["App/f{0}.c".format(i) for i in range(20)]
        write_db(self.proj, files)
        make_sources(self.proj, files[:19])
        r = run_cli("--root", str(self.proj), "--ownership-threshold", "0.0")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)

    def test_absolute_file_paths_are_checked_too(self):
        src = self.proj / "App" / "main.c"
        make_sources(self.proj, ["App/main.c"])
        write_db(self.proj, [str(src).replace("\\", "/")])
        r = run_cli("--root", str(self.proj))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


class TestRecursiveDiscovery(unittest.TestCase):
    """A repo root with two projects below it, the exe sitting at the top."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "repo"
        self.app = self.root / "Code" / "App" / "Proj"
        self.boot = self.root / "Code" / "Boot" / "Proj"
        self.app.mkdir(parents=True)
        self.boot.mkdir(parents=True)
        make_sources(self.app, ["../Code/main.c"])
        make_sources(self.boot, ["../Code/boot.c"])
        write_db(self.app, ["../Code/main.c"])
        write_db(self.boot, ["../Code/boot.c"])

    def tearDown(self):
        self.tmp.cleanup()

    def test_each_config_is_anchored_to_its_own_directory(self):
        r = run_cli("--root", str(self.root))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        app_cc = json.loads((self.app / "compile_commands.json").read_text(encoding="utf-8"))
        boot_cc = json.loads((self.boot / "compile_commands.json").read_text(encoding="utf-8"))
        self.assertEqual(app_cc[0]["directory"], str(self.app).replace("\\", "/"))
        self.assertEqual(boot_cc[0]["directory"], str(self.boot).replace("\\", "/"))
        # The bug this guards against: everything collapsed onto the search root.
        self.assertNotEqual(app_cc[0]["directory"], str(self.root).replace("\\", "/"))

    def test_reports_relative_labels_for_each_site(self):
        r = run_cli("--root", str(self.root))
        self.assertIn("Code/App/Proj/compile_commands.json", r.stdout)
        self.assertIn("Code/Boot/Proj/compile_commands.json", r.stdout)

    def test_pointer_clangd_is_a_no_op_not_an_error(self):
        pointer_dir = self.root / "Code" / "App" / "Code"
        pointer_dir.mkdir(parents=True, exist_ok=True)
        (pointer_dir / ".clangd").write_text(
            "CompileFlags:\n  CompilationDatabase: ../Proj\n", encoding="utf-8")
        r = run_cli("--root", str(self.root))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(
            (pointer_dir / ".clangd").read_text(encoding="utf-8"),
            "CompileFlags:\n  CompilationDatabase: ../Proj\n")
        self.assertFalse((pointer_dir / ".clangd.bak").exists())

    def test_build_output_dirs_are_not_searched(self):
        objs = self.app / "Objects"
        objs.mkdir()
        write_db(objs, ["nope/gone.c"])          # a stale copy that would fail ownership
        r = run_cli("--root", str(self.root))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        stale = json.loads((objs / "compile_commands.json").read_text(encoding="utf-8"))
        self.assertEqual(stale[0]["directory"], "C:/somewhere/else")   # untouched

    def test_nothing_found_errors(self):
        empty = Path(self.tmp.name) / "empty"
        empty.mkdir()
        r = run_cli("--root", str(empty))
        self.assertEqual(r.returncode, 1)
        self.assertIn("no .clangd or compile_commands.json", r.stderr)

    def test_dry_run_across_several_sites_writes_nothing(self):
        before = (self.app / "compile_commands.json").read_text(encoding="utf-8")
        r = run_cli("--root", str(self.root), "--dry-run")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual((self.app / "compile_commands.json").read_text(encoding="utf-8"),
                         before)
        self.assertFalse((self.app / "compile_commands.json.bak").exists())

    def test_one_bad_site_blocks_the_whole_run(self):
        # Boot's database is foreign; App's is fine. Nothing may be written.
        write_db(self.boot, ["../Code/does_not_exist.c"])
        before = (self.app / "compile_commands.json").read_text(encoding="utf-8")
        r = run_cli("--root", str(self.root))
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertEqual((self.app / "compile_commands.json").read_text(encoding="utf-8"),
                         before)


if __name__ == "__main__":
    unittest.main()
