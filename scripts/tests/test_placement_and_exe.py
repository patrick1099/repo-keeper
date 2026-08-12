"""Regressions for --fix-placement under --dry-run, and re-anchor exe delivery.

--fix-placement used to write its pointer .clangd *before* the dry-run early
return, so `--dry-run --fix-placement` created a file and then announced "no
files written". The gate now lives inside the write functions themselves, so a
preview and a real write travel the same code path.
"""

import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent
SCRIPT = SCRIPTS / "Keil2Clangd.py"
FIX = HERE / "fixtures" / "sample.uvprojx"

sys.path.insert(0, str(SCRIPTS))
import k2c_common as common  # noqa: E402


def sibling_project(base):
    """Keil's real-world shape: Proj/ holds the project, sources live in Code/.

    The output dir is then a *sibling* of the sources, which is exactly the
    layout where clangd cannot discover the database on its own.
    """
    base = Path(base)
    proj = base / "Proj"
    code = base / "Code"
    (code / "User").mkdir(parents=True)
    (code / "bsp").mkdir(parents=True)
    proj.mkdir()
    xml = FIX.read_text(encoding="utf-8").replace(
        r".\User\main.c", r"..\Code\User\main.c").replace(
        r".\bsp\led.c", r"..\Code\bsp\led.c")
    (proj / "proj.uvprojx").write_text(xml, encoding="utf-8")
    (code / "User" / "main.c").write_text("int main(void){return 0;}\n",
                                          encoding="utf-8")
    (code / "bsp" / "led.c").write_text("void led(void){}\n", encoding="utf-8")
    return proj, code


def run(proj, *extra):
    # --no-build-exe: deployment is what these tests are about. Left to the
    # default a fresh checkout would freeze the exe with PyInstaller here,
    # turning a unit test into a minute-long build that writes into the
    # plugin's own dist/. --no-syntax-probe likewise: the fixture's includes
    # are fake, so asking a real clang about them proves nothing.
    cmd = [sys.executable, str(SCRIPT), "-p", str(proj), "-o", str(proj),
           "-k", "/nonexistent", "--no-build-exe",
           "--no-syntax-probe"] + list(extra)
    return subprocess.run(cmd, capture_output=True, text=True)


class TestFixPlacementDryRun(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.proj, self.code = sibling_project(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_layout_actually_triggers_placement_problem(self):
        r = run(self.proj, "--no-exe")
        self.assertIn("placement: PROBLEM", r.stdout, r.stdout + r.stderr)

    def test_dry_run_with_fix_placement_writes_nothing(self):
        r = run(self.proj, "--dry-run", "--fix-placement", "--no-exe")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse((self.code / ".clangd").exists(),
                         "pointer .clangd was written during --dry-run")
        self.assertFalse((self.proj / ".clangd").exists())
        self.assertFalse((self.proj / "compile_commands.json").exists())
        self.assertIn("Would generate", r.stdout)
        self.assertIn("no files written", r.stdout)

    def test_fix_placement_writes_pointer_for_real(self):
        r = run(self.proj, "--fix-placement", "--no-exe")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        pointer = self.code / ".clangd"
        self.assertTrue(pointer.exists())
        text = pointer.read_text(encoding="utf-8")
        self.assertIn("CompilationDatabase: ../Proj", text)
        self.assertTrue((self.proj / "compile_commands.json").exists())


class TestReanchorExeDelivery(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.proj, self.code = sibling_project(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_no_exe_flag_places_nothing(self):
        run(self.proj, "--fix-placement", "--no-exe")
        self.assertEqual(list(self.root.rglob(common.REANCHOR_EXE_NAME)), [])

    def test_exe_dest_override(self):
        dest = self.root / "elsewhere"
        dest.mkdir()
        r = run(self.proj, "--fix-placement", "--exe-dest", str(dest))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        if common.reanchor_exe_source() is None:
            self.skipTest("re-anchor exe not built")
        self.assertTrue((dest / common.REANCHOR_EXE_NAME).is_file())

    def test_default_lands_at_git_root_not_next_to_clangd(self):
        (self.root / ".git").mkdir()
        r = run(self.proj, "--fix-placement")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        if common.reanchor_exe_source() is None:
            self.skipTest("re-anchor exe not built")
        self.assertTrue((self.root / common.REANCHOR_EXE_NAME).is_file(),
                        "exe should land at the repo root")
        self.assertFalse((self.proj / common.REANCHOR_EXE_NAME).exists())

    def test_dry_run_places_no_exe(self):
        (self.root / ".git").mkdir()
        r = run(self.proj, "--dry-run")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse((self.root / common.REANCHOR_EXE_NAME).exists())


class TestReanchorExeStaleness(unittest.TestCase):
    """A prebuilt exe older than its sources must not ship in silence.

    dist/ is gitignored, so the exe drifts behind the scripts with nothing to
    say so: the 0.4.0 recursive-search fix sat in ReAnchor.py for weeks while
    projects kept receiving a July build that still refused to look one
    directory down.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fake_exe = Path(self.tmp.name) / common.REANCHOR_EXE_NAME
        self.fake_exe.write_bytes(b"MZ fake")

    def tearDown(self):
        self.tmp.cleanup()

    def _set_mtime(self, path, when):
        os.utime(str(path), (when, when))

    def test_exe_older_than_sources_is_reported_stale(self):
        self._set_mtime(self.fake_exe, 0)
        stale = common.reanchor_exe_stale_sources(self.fake_exe)
        self.assertEqual(sorted(stale), sorted(common.REANCHOR_EXE_SOURCES))

    def test_exe_newer_than_sources_is_clean(self):
        self._set_mtime(self.fake_exe, time.time() + 3600)
        self.assertEqual(common.reanchor_exe_stale_sources(self.fake_exe), [])

    def test_missing_exe_is_not_stale(self):
        self.assertEqual(common.reanchor_exe_stale_sources(
            Path(self.tmp.name) / "nope.exe"), [])

    def test_deploy_warns_and_still_copies(self):
        self._set_mtime(self.fake_exe, 0)
        dest_root = Path(self.tmp.name) / "project"
        dest_root.mkdir()
        buf = io.StringIO()
        with mock.patch.object(common, "reanchor_exe_source",
                               return_value=self.fake_exe):
            with redirect_stdout(buf):
                dest = common.deploy_reanchor_exe(dest_root, auto_build=False)
        out = buf.getvalue()
        self.assertIn("OUT OF DATE", out)
        self.assertIn("ReAnchor.py", out)
        self.assertIn("build_exe.bat", out)
        self.assertTrue(dest.is_file())

    def test_stale_warning_survives_the_already_current_shortcut(self):
        """The exe already sitting in the project is the quietest stale case."""
        self._set_mtime(self.fake_exe, 0)
        dest_root = Path(self.tmp.name) / "project"
        dest_root.mkdir()
        (dest_root / common.REANCHOR_EXE_NAME).write_bytes(
            self.fake_exe.read_bytes())
        buf = io.StringIO()
        with mock.patch.object(common, "reanchor_exe_source",
                               return_value=self.fake_exe):
            with redirect_stdout(buf):
                common.deploy_reanchor_exe(dest_root, auto_build=False)
        out = buf.getvalue()
        self.assertIn("OUT OF DATE", out)
        self.assertIn("already current", out)

    def test_stale_exe_is_rebuilt_rather_than_only_warned_about(self):
        self._set_mtime(self.fake_exe, 0)
        fresh = Path(self.tmp.name) / "fresh.exe"
        fresh.write_bytes(b"MZ fresh build")
        dest_root = Path(self.tmp.name) / "project"
        dest_root.mkdir()
        with mock.patch.object(common, "reanchor_exe_source",
                               return_value=self.fake_exe):
            with mock.patch.object(common, "build_reanchor_exe",
                                   return_value=fresh) as build:
                with redirect_stdout(io.StringIO()):
                    dest = common.deploy_reanchor_exe(dest_root)
        build.assert_called_once()
        self.assertEqual(dest.read_bytes(), b"MZ fresh build")

    def test_same_size_different_build_is_replaced(self):
        """Equal sizes were treated as 'already current' -- they are not."""
        self._set_mtime(self.fake_exe, time.time() + 3600)
        dest_root = Path(self.tmp.name) / "project"
        dest_root.mkdir()
        dest = dest_root / common.REANCHOR_EXE_NAME
        dest.write_bytes(b"MZ OLD_")  # same length, different bytes
        self.assertEqual(dest.stat().st_size, self.fake_exe.stat().st_size)
        with mock.patch.object(common, "reanchor_exe_source",
                               return_value=self.fake_exe):
            with redirect_stdout(io.StringIO()):
                common.deploy_reanchor_exe(dest_root)
        self.assertEqual(dest.read_bytes(), b"MZ fake")


class TestReanchorExeIsBuiltWhenMissing(unittest.TestCase):
    """dist/ is gitignored, so "never built" is the state of every clone.

    Deployment used to print one "not built -- skipped" line and let the run
    report success, which is how a repo ends up with clangd config and no way
    to repair it after a move.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dest_root = Path(self.tmp.name) / "project"
        self.dest_root.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_exe_triggers_a_build(self):
        fresh = Path(self.tmp.name) / "fresh.exe"
        fresh.write_bytes(b"MZ built on demand")
        with mock.patch.object(common, "reanchor_exe_source",
                               return_value=None):
            with mock.patch.object(common, "build_reanchor_exe",
                                   return_value=fresh) as build:
                with redirect_stdout(io.StringIO()):
                    dest = common.deploy_reanchor_exe(self.dest_root)
        build.assert_called_once()
        self.assertEqual(dest.read_bytes(), b"MZ built on demand")

    def test_no_auto_build_keeps_the_old_skip(self):
        buf = io.StringIO()
        with mock.patch.object(common, "reanchor_exe_source",
                               return_value=None):
            with mock.patch.object(common, "build_reanchor_exe") as build:
                with redirect_stdout(buf):
                    dest = common.deploy_reanchor_exe(self.dest_root,
                                                      auto_build=False)
        build.assert_not_called()
        self.assertIsNone(dest)
        self.assertIn("skipped", buf.getvalue())

    def test_dry_run_never_builds(self):
        with mock.patch.object(common, "reanchor_exe_source",
                               return_value=None):
            with mock.patch.object(common, "build_reanchor_exe") as build:
                with redirect_stdout(io.StringIO()):
                    common.deploy_reanchor_exe(self.dest_root, dry_run=True)
        build.assert_not_called()

    def test_a_failed_build_deploys_nothing_and_says_so(self):
        with mock.patch.object(common, "reanchor_exe_source",
                               return_value=None):
            with mock.patch.object(common, "build_reanchor_exe",
                                   return_value=None):
                with redirect_stdout(io.StringIO()):
                    dest = common.deploy_reanchor_exe(self.dest_root)
        self.assertIsNone(dest)
        self.assertEqual(list(self.dest_root.iterdir()), [])


class TestFindProjectRoot(unittest.TestCase):
    def test_git_dir_wins_over_common_ancestor(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            (root / ".git").mkdir(parents=True)
            out = root / "Code" / "App" / "Proj"
            out.mkdir(parents=True)
            src = root / "Code" / "App" / "Code" / "main.c"
            src.parent.mkdir(parents=True)
            src.write_text("", encoding="utf-8")
            self.assertEqual(common.find_project_root(out, [src]), root)

    def test_git_file_worktree_is_recognised(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "wt"
            root.mkdir()
            # A linked worktree stores a .git *file*, not a directory.
            (root / ".git").write_text("gitdir: ../main/.git/worktrees/wt\n",
                                       encoding="utf-8")
            out = root / "Proj"
            out.mkdir()
            self.assertEqual(common.find_project_root(out, []), root)

    def test_falls_back_to_common_ancestor_without_git(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "nogit"
            out = base / "Proj"
            out.mkdir(parents=True)
            src = base / "Code" / "main.c"
            src.parent.mkdir(parents=True)
            src.write_text("", encoding="utf-8")
            self.assertEqual(common.find_project_root(out, [src]), base)


class TestWriteGates(unittest.TestCase):
    """The dry-run gate must live in the write functions, not in the callers."""

    def test_pointer_writer_honours_dry_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            anchor = Path(tmp) / "Code"
            db = Path(tmp) / "Proj"
            anchor.mkdir()
            db.mkdir()
            common.write_pointer_clangd(db, anchor, dry_run=True)
            self.assertFalse((anchor / ".clangd").exists())
            common.write_pointer_clangd(db, anchor)
            self.assertTrue((anchor / ".clangd").exists())

    def test_compile_commands_writer_honours_dry_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            common.write_compile_commands([{"file": "a.c"}], out, dry_run=True)
            self.assertFalse((out / "compile_commands.json").exists())
            common.write_compile_commands([{"file": "a.c"}], out)
            self.assertEqual(
                json.loads((out / "compile_commands.json").read_text()),
                [{"file": "a.c"}])

    def test_clangd_doc_writer_honours_dry_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            doc = common.ClangdDoc().add_group(None, ["-DX"])
            doc.write(out, dry_run=True)
            self.assertFalse((out / ".clangd").exists())
            doc.write(out)
            self.assertIn("-DX", (out / ".clangd").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
