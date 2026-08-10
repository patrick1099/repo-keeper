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


def _defines(entry):
    return sorted(a for a in entry["arguments"] if a.startswith("-D"))


class TestCompileCommandsMerge(unittest.TestCase):
    def setUp(self):
        self.parser = k2c.UvprojxParser(FIX)
        self.base = Path(FIX).parent

    def _gen(self, enrichment):
        return k2c.CompileCommandsGenerator(
            self.parser, _NoKeil(), use_absolute=True,
            base_dir=self.base, enrichment=enrichment).generate()

    def test_defines_invariant_with_and_without_dep(self):
        plain = self._gen(None)
        enr = k2c.DepEnrichment(
            found=True, stale=False,
            system_includes=[Path("/opt/keil/Include")],
            preinclude_files=[self.base / "User" / "preinc.h"],
            source_files=[self.base / "User" / "main.c"])
        enriched = self._gen(enr)
        self.assertEqual(_defines(plain[0]), _defines(enriched[0]))

    def test_enrichment_adds_system_include_and_imacros(self):
        enr = k2c.DepEnrichment(
            found=True, stale=False,
            system_includes=[Path("/opt/keil/Include")],
            preinclude_files=[self.base / "User" / "preinc.h"],
            source_files=[self.base / "User" / "main.c"])
        e = self._gen(enr)[0]
        self.assertTrue(any(a.startswith("-I") and "Include" in a for a in e["arguments"]))
        self.assertIn("-imacros", e["arguments"])

    def test_stale_enrichment_ignored(self):
        plain = self._gen(None)
        stale = self._gen(k2c.DepEnrichment(found=True, stale=True))
        self.assertEqual(len(plain), len(stale))
        self.assertFalse(any("-imacros" in a for e in stale for a in e["arguments"]))


if __name__ == "__main__":
    unittest.main()
