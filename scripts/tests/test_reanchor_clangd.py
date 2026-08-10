import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ReAnchor import reanchor_clangd_text

SAMPLE = """CompileFlags:
  Add:
    # Keil project macros
    - -D__DEBUG
    # AI-added ARMCC compat macro (must survive)
    - -D__weak=__attribute__((weak))
    # Include paths
    - -IApp/Code
    - -ID:/OldKeil/ARM/ARMCLANG/include
    # Preinclude headers (from .dep)
    - -imacros
    - D:/OldKeil/ARM/ARMCLANG/include/pre.h
  Remove:
    - -W*
"""


class TestReanchorClangd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.keil = Path(self.tmp.name) / "Keil_v5"
        inc = self.keil / "ARM" / "ARMCLANG" / "include"
        inc.mkdir(parents=True)
        (inc / "pre.h").write_text("", encoding="utf-8")
        self.new_inc = str(inc).replace("\\", "/")

    def tearDown(self):
        self.tmp.cleanup()

    def test_dead_I_and_imacros_fixed_others_untouched(self):
        new_text, changes, dead = reanchor_clangd_text(SAMPLE, self.keil)
        self.assertIn(f"    - -I{self.new_inc}\n", new_text)
        self.assertIn(f"    - {self.new_inc}/pre.h\n", new_text)
        self.assertEqual(len(changes), 2)
        self.assertEqual(dead, [])
        # non-path lines byte-identical
        for line in SAMPLE.split("\n"):
            if "OldKeil" not in line:
                self.assertIn(line + ("\n" if line else ""), new_text + "\n")

    def test_ai_added_lines_and_comments_survive(self):
        new_text, _, _ = reanchor_clangd_text(SAMPLE, self.keil)
        self.assertIn("- -D__weak=__attribute__((weak))", new_text)
        self.assertIn("# AI-added ARMCC compat macro (must survive)", new_text)
        self.assertIn("- -IApp/Code", new_text)

    def test_no_keil_found_keeps_text_reports_dead(self):
        new_text, changes, dead = reanchor_clangd_text(SAMPLE, None)
        self.assertEqual(new_text, SAMPLE)
        self.assertEqual(changes, [])
        self.assertEqual(sorted(dead), sorted([
            "D:/OldKeil/ARM/ARMCLANG/include",
            "D:/OldKeil/ARM/ARMCLANG/include/pre.h",
        ]))

    def test_idempotent_when_paths_alive(self):
        fixed, _, _ = reanchor_clangd_text(SAMPLE, self.keil)
        again, changes, dead = reanchor_clangd_text(fixed, self.keil)
        self.assertEqual(again, fixed)
        self.assertEqual(changes, [])
        self.assertEqual(dead, [])

    def test_crlf_preserved(self):
        crlf = SAMPLE.replace("\n", "\r\n")
        new_text, changes, _ = reanchor_clangd_text(crlf, self.keil)
        self.assertEqual(len(changes), 2)
        self.assertNotIn("\n    - -D__DEBUG\n", new_text.replace("\r\n", "\x00"))
        self.assertIn("\r\n", new_text)
        self.assertEqual(new_text.count("\r\n"), crlf.count("\r\n"))


if __name__ == "__main__":
    unittest.main()
