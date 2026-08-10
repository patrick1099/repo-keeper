import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import Keil2Clangd as k2c

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "sample.uvprojx")


class _NoKeil:
    def found(self):
        return False


class TestClangdMerge(unittest.TestCase):
    def setUp(self):
        self.parser = k2c.UvprojxParser(FIX)
        self.base = Path(FIX).parent

    def _yaml(self, enrichment):
        return k2c.ClangdGenerator(
            self.parser, _NoKeil(), use_absolute=True,
            base_dir=self.base, enrichment=enrichment).generate()

    def test_defines_present_regardless(self):
        for enr in (None, k2c.DepEnrichment(
                found=True, stale=False,
                system_includes=[Path("/opt/keil/Include")],
                preinclude_files=[self.base / "User" / "preinc.h"])):
            y = self._yaml(enr)
            self.assertIn("-D__DEBUG", y)
            self.assertIn("-DUSE_HAL", y)

    def test_enrichment_adds_system_include_and_imacros(self):
        enr = k2c.DepEnrichment(
            found=True, stale=False,
            system_includes=[Path("/opt/keil/Include")],
            preinclude_files=[self.base / "User" / "preinc.h"])
        y = self._yaml(enr)
        self.assertIn("Include", y)
        self.assertIn("-imacros", y)

    def test_stale_enrichment_ignored(self):
        y = self._yaml(k2c.DepEnrichment(found=True, stale=True))
        self.assertNotIn("-imacros", y)


if __name__ == "__main__":
    unittest.main()
