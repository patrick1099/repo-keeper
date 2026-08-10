import os
import sys
import time
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import Keil2Clangd as k2c

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "sample.uvprojx")

DEP_BODY = (
    "Toolchain Path:  D:\\Keil_v5\\ARM\\ARMCC\\Bin\n"
    "F (.\\User\\main.c)(0x1)(--c99 -c --preinclude .\\User\\preinc.h -o x.o)\n"
)


class TestDepParser(unittest.TestCase):
    def _project(self, tmp):
        """Copy sample.uvprojx into tmp dir as proj.uvprojx, return parser."""
        proj = Path(tmp) / "proj.uvprojx"
        proj.write_text(Path(FIX).read_text(encoding="utf-8"), encoding="utf-8")
        return k2c.UvprojxParser(str(proj))

    def _write_dep(self, tmp):
        objs = Path(tmp) / "Objects"
        objs.mkdir(exist_ok=True)
        dep = objs / "proj_App.dep"
        dep.write_text(DEP_BODY, encoding="utf-8")
        return dep

    def test_missing_dep_returns_not_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            parser = self._project(tmp)
            enr = k2c.DepParser(parser).parse()
            self.assertFalse(enr.found)
            self.assertFalse(enr.stale)

    def test_fresh_dep_parsed(self):
        with tempfile.TemporaryDirectory() as tmp:
            parser = self._project(tmp)
            dep = self._write_dep(tmp)
            # make dep newer than uvprojx
            future = time.time() + 10
            os.utime(str(dep), (future, future))
            enr = k2c.DepParser(parser).parse()
            self.assertTrue(enr.found)
            self.assertFalse(enr.stale)
            names = [p.name for p in enr.source_files]
            self.assertIn("main.c", names)
            self.assertIn("preinc.h", [p.name for p in enr.preinclude_files])
            self.assertTrue(any("Include" in str(p) for p in enr.system_includes))

    def test_stale_dep_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            parser = self._project(tmp)
            dep = self._write_dep(tmp)
            past = time.time() - 100
            os.utime(str(dep), (past, past))  # dep older than uvprojx
            enr = k2c.DepParser(parser).parse()
            self.assertTrue(enr.found)
            self.assertTrue(enr.stale)
            self.assertEqual(enr.source_files, [])

    def test_override_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            parser = self._project(tmp)
            custom = Path(tmp) / "custom.dep"
            custom.write_text(DEP_BODY, encoding="utf-8")
            future = time.time() + 10
            os.utime(str(custom), (future, future))
            enr = k2c.DepParser(parser, dep_path_override=str(custom)).parse()
            self.assertTrue(enr.found)
            self.assertFalse(enr.stale)


if __name__ == "__main__":
    unittest.main()
