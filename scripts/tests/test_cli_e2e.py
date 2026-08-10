import os
import sys
import time
import json
import tempfile
import unittest
import subprocess
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(os.path.dirname(HERE), "Keil2Clangd.py")
FIX = os.path.join(HERE, "fixtures", "sample.uvprojx")

DEP_BODY = (
    "Toolchain Path:  D:\\Keil_v5\\ARM\\ARMCC\\Bin\n"
    "F (.\\User\\main.c)(0x1)(--c99 -c --preinclude .\\User\\preinc.h -o x.o)\n"
)


def run(project_dir, *extra):
    # --no-exe: these cases are about flag/dep handling, and copying the 9 MB
    # re-anchor exe into every temp dir would dominate their runtime.
    cmd = [sys.executable, SCRIPT, "-p", str(project_dir), "-o", str(project_dir),
           "-a", "-k", "/nonexistent", "--no-exe"] + list(extra)
    return subprocess.run(cmd, capture_output=True, text=True)


class TestCliE2E(unittest.TestCase):
    def _project(self, tmp, with_dep=True, fresh=True):
        proj = Path(tmp) / "proj.uvprojx"
        proj.write_text(Path(FIX).read_text(encoding="utf-8"), encoding="utf-8")
        if with_dep:
            objs = Path(tmp) / "Objects"
            objs.mkdir()
            dep = objs / "proj_App.dep"
            dep.write_text(DEP_BODY, encoding="utf-8")
            t = time.time() + (10 if fresh else -100)
            os.utime(str(dep), (t, t))
        return proj

    def test_fresh_dep_used(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._project(tmp, with_dep=True, fresh=True)
            r = run(tmp)
            self.assertEqual(r.returncode, 0, r.stderr)
            cc = json.loads((Path(tmp) / "compile_commands.json").read_text())
            self.assertTrue(any("-imacros" in e["arguments"] for e in cc))

    def test_stale_dep_warns_and_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._project(tmp, with_dep=True, fresh=False)
            r = run(tmp)
            self.assertIn("stale", (r.stdout + r.stderr).lower())
            cc = json.loads((Path(tmp) / "compile_commands.json").read_text())
            self.assertFalse(any("-imacros" in e["arguments"] for e in cc))

    def test_no_dep_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._project(tmp, with_dep=True, fresh=True)
            r = run(tmp, "--no-dep")
            cc = json.loads((Path(tmp) / "compile_commands.json").read_text())
            self.assertFalse(any("-imacros" in e["arguments"] for e in cc))


if __name__ == "__main__":
    unittest.main()
