"""The generator must refuse to guess a project or a target.

Both choices used to be a silent `[0]`: a caller that never chose still got
exit 0 and a plausible-looking config for whichever project/target came first
in the XML. These tests pin the refusal, because the only thing that binds a
non-compliant caller is a non-zero exit.
"""

import os
import sys
import json
import tempfile
import unittest
import subprocess
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(os.path.dirname(HERE), "Keil2Clangd.py")
FIX = os.path.join(HERE, "fixtures", "sample.uvprojx")


def run(*extra):
    cmd = [sys.executable, SCRIPT, "-a", "-k", "/nonexistent", "--no-exe"]
    return subprocess.run(cmd + list(extra), capture_output=True, text=True)


def two_target_project(tmp, name="proj.uvprojx"):
    """Clone the single-target fixture into a Debug/Release pair."""
    text = Path(FIX).read_text(encoding="utf-8")
    start = text.index("    <Target>")
    end = text.index("</Target>") + len("</Target>\n")
    block = text[start:end]

    debug = block.replace("<TargetName>App</TargetName>",
                          "<TargetName>App_Debug</TargetName>")
    release = (block
               .replace("<TargetName>App</TargetName>",
                        "<TargetName>App_Release</TargetName>")
               .replace("<Define>__DEBUG, USE_HAL</Define>",
                        "<Define>NDEBUG, USE_HAL</Define>"))

    proj = Path(tmp) / name
    proj.write_text(text[:start] + debug + release + text[end:],
                    encoding="utf-8")
    return proj


class TestTargetGate(unittest.TestCase):
    def test_refuses_to_guess_between_targets(self):
        with tempfile.TemporaryDirectory() as tmp:
            two_target_project(tmp)
            r = run("-p", tmp, "-o", tmp)
            self.assertEqual(r.returncode, 2, r.stdout)
            # Nothing may be written when the choice was never made.
            self.assertFalse((Path(tmp) / ".clangd").exists())
            self.assertFalse((Path(tmp) / "compile_commands.json").exists())
            # The refusal must be actionable: both targets and their macros.
            self.assertIn("App_Debug", r.stderr)
            self.assertIn("App_Release", r.stderr)
            self.assertIn("__DEBUG", r.stderr)
            self.assertIn("NDEBUG", r.stderr)

    def test_explicit_target_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            two_target_project(tmp)
            r = run("-p", tmp, "-o", tmp, "-t", "App_Release")
            self.assertEqual(r.returncode, 0, r.stderr)
            cc = json.loads((Path(tmp) / "compile_commands.json").read_text())
            args = " ".join(cc[0]["arguments"])
            self.assertIn("-DNDEBUG", args)
            self.assertNotIn("-D__DEBUG", args)

    def test_escape_hatch_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            two_target_project(tmp)
            r = run("-p", tmp, "-o", tmp, "--use-first-target")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertTrue((Path(tmp) / ".clangd").exists())

    def test_dry_run_still_lists_targets(self):
        """--dry-run is the discovery command; the gate must not block it."""
        with tempfile.TemporaryDirectory() as tmp:
            two_target_project(tmp)
            r = run("-p", tmp, "-o", tmp, "--dry-run")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertFalse((Path(tmp) / ".clangd").exists())
            self.assertIn("App_Release", r.stdout)
            self.assertIn("ACTION REQUIRED", r.stdout)

    def test_default_marker_does_not_claim_ide_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            two_target_project(tmp)
            r = run("-p", tmp, "-o", tmp, "--dry-run")
            self.assertIn("FIRST IN XML", r.stdout)
            self.assertNotIn("<-- selected\n", r.stdout)

    def test_single_target_needs_no_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "proj.uvprojx").write_text(
                Path(FIX).read_text(encoding="utf-8"), encoding="utf-8")
            r = run("-p", tmp, "-o", tmp)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertTrue((Path(tmp) / ".clangd").exists())


class TestProjectGate(unittest.TestCase):
    def test_refuses_to_guess_between_projects(self):
        with tempfile.TemporaryDirectory() as tmp:
            body = Path(FIX).read_text(encoding="utf-8")
            Path(tmp, "boot.uvprojx").write_text(body, encoding="utf-8")
            sub = Path(tmp) / "app"
            sub.mkdir()
            (sub / "app.uvprojx").write_text(body, encoding="utf-8")

            r = run("-p", tmp, "-o", tmp)
            self.assertEqual(r.returncode, 2, r.stdout)
            self.assertFalse((Path(tmp) / ".clangd").exists())
            self.assertIn("boot.uvprojx", r.stderr)
            self.assertIn("app.uvprojx", r.stderr)
            self.assertIn("--project", r.stderr)

    def test_explicit_project_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            body = Path(FIX).read_text(encoding="utf-8")
            Path(tmp, "boot.uvprojx").write_text(body, encoding="utf-8")
            sub = Path(tmp) / "app"
            sub.mkdir()
            chosen = sub / "app.uvprojx"
            chosen.write_text(body, encoding="utf-8")

            r = run("--project", str(chosen), "-o", tmp)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertTrue((Path(tmp) / ".clangd").exists())

    def test_missing_explicit_project_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = run("--project", str(Path(tmp) / "nope.uvprojx"), "-o", tmp)
            self.assertEqual(r.returncode, 1)


IAR_SCRIPT = os.path.join(os.path.dirname(HERE), "Iar2Clangd.py")
EWP_FIX = os.path.join(HERE, "fixtures", "sample.ewp")


def run_iar(*extra):
    cmd = [sys.executable, IAR_SCRIPT, "-a", "--iar-path", "/nonexistent",
           "--no-probe"]
    return subprocess.run(cmd + list(extra), capture_output=True, text=True)


def ewp_project(tmp, name="proj.ewp"):
    """The fixture already carries a Debug/Release pair."""
    proj = Path(tmp) / name
    proj.write_text(Path(EWP_FIX).read_text(encoding="utf-8"), encoding="utf-8")
    return proj


class TestIarConfigGate(unittest.TestCase):
    def test_refuses_to_guess_between_configs(self):
        with tempfile.TemporaryDirectory() as tmp:
            ewp_project(tmp)
            r = run_iar("-p", tmp, "-o", tmp)
            self.assertEqual(r.returncode, 2, r.stdout)
            self.assertFalse((Path(tmp) / ".clangd").exists())
            self.assertFalse((Path(tmp) / "compile_commands.json").exists())
            self.assertIn("Debug", r.stderr)
            self.assertIn("Release", r.stderr)
            self.assertIn("_CODE_DEBUG_", r.stderr)
            self.assertIn("DEF_RELEASE", r.stderr)

    def test_explicit_config_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            ewp_project(tmp)
            r = run_iar("-p", tmp, "-o", tmp, "-c", "Release")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertTrue((Path(tmp) / ".clangd").exists())

    def test_escape_hatch_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            ewp_project(tmp)
            r = run_iar("-p", tmp, "-o", tmp, "--use-first-config")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertTrue((Path(tmp) / ".clangd").exists())

    def test_list_configs_still_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            ewp_project(tmp)
            r = run_iar("-p", tmp, "-o", tmp, "--list-configs")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("Release", r.stdout)

    def test_dry_run_still_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            ewp_project(tmp)
            r = run_iar("-p", tmp, "-o", tmp, "--dry-run")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertFalse((Path(tmp) / ".clangd").exists())
            self.assertIn("FIRST IN XML", r.stdout)
            self.assertIn("ACTION REQUIRED", r.stdout)

    def test_refuses_to_guess_between_ewp_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            ewp_project(tmp, "boot.ewp")
            sub = Path(tmp) / "app"
            sub.mkdir()
            ewp_project(sub, "app.ewp")

            r = run_iar("-p", tmp, "-o", tmp, "-c", "Debug")
            self.assertEqual(r.returncode, 1, r.stdout)
            self.assertFalse((Path(tmp) / ".clangd").exists())
            self.assertIn("boot.ewp", r.stderr)
            self.assertIn("--project", r.stderr)


class TestVerify(unittest.TestCase):
    """The self-check replaces three sections of "please verify by hand"."""

    def _project(self, tmp, all_sources=True):
        """Lay the project one level down.

        The fixture's second source and its ``-I`` are siblings of the project
        dir (``..\\bsp``), so the project must not sit directly in the system
        temp dir -- otherwise those siblings land in a shared directory and
        leak into whatever runs next.
        """
        proj_dir = Path(tmp) / "proj"
        proj_dir.mkdir()
        (proj_dir / "proj.uvprojx").write_text(
            Path(FIX).read_text(encoding="utf-8"), encoding="utf-8")
        (proj_dir / "User").mkdir()
        (proj_dir / "User" / "main.c").write_text("int main(void){return 0;}\n",
                                                  encoding="utf-8")
        if all_sources:
            bsp = Path(tmp) / "bsp"
            bsp.mkdir()
            (bsp / "led.c").write_text("void led(void){}\n", encoding="utf-8")
        return proj_dir

    def test_reports_missing_source_and_include(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj = self._project(tmp, all_sources=False)
            r = run("-p", str(proj), "-o", str(proj))
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("verify:", r.stdout)
            self.assertIn("source file does not exist", r.stdout)
            self.assertIn("include path does not exist", r.stdout)

    def test_runs_without_being_asked(self):
        """Opt-in checks get omitted; this one must be on by default."""
        with tempfile.TemporaryDirectory() as tmp:
            proj = self._project(tmp)
            r = run("-p", str(proj), "-o", str(proj))
            self.assertIn("verify:", r.stdout)
            self.assertIn("consistent across both files", r.stdout)

    def test_no_verify_skips(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj = self._project(tmp)
            r = run("-p", str(proj), "-o", str(proj), "--no-verify")
            self.assertNotIn("verify:", r.stdout)

    def test_strict_promotes_warnings(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj = self._project(tmp, all_sources=False)
            r = run("-p", str(proj), "-o", str(proj), "--verify-strict")
            self.assertEqual(r.returncode, 3, r.stdout)

    def test_macro_disagreement_is_an_error(self):
        """A -D in one file but not the other can never be legitimate."""
        import k2c_common as common
        with tempfile.TemporaryDirectory() as tmp:
            proj = self._project(tmp)
            run("-p", str(proj), "-o", str(proj))
            cc_path = proj / "compile_commands.json"
            cc = json.loads(cc_path.read_text())
            for entry in cc:
                entry["arguments"] = [a for a in entry["arguments"]
                                      if a != "-D__DEBUG"]
            cc_path.write_text(json.dumps(cc), encoding="utf-8")

            report = common.verify_output(proj)
            self.assertFalse(report.ok)
            self.assertTrue(any("disagree on -D" in e for e in report.errors))

    def test_relative_directory_is_an_error(self):
        """clangd refuses a relative 'directory'; it must never pass verify."""
        import k2c_common as common
        with tempfile.TemporaryDirectory() as tmp:
            proj = self._project(tmp)
            run("-p", str(proj), "-o", str(proj))
            cc_path = proj / "compile_commands.json"
            cc = json.loads(cc_path.read_text())
            for entry in cc:
                entry["directory"] = "."
            cc_path.write_text(json.dumps(cc), encoding="utf-8")

            report = common.verify_output(proj)
            self.assertFalse(report.ok)
            self.assertTrue(any("absolute" in e for e in report.errors))


if __name__ == "__main__":
    unittest.main()
