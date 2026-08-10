import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import Keil2Clangd as k2c

SAMPLE_DEP = r"""Dependencies for Project 'App', Target 'App': (DO NOT MODIFY !)
Toolchain Path:  D:\Keil_v5\ARM\ARMCC\Bin
F (.\User\main.c)(0x5F3A1B2C)(--c99 -c --cpu Cortex-M3 -D__DEBUG  -I.\User  -I..\bsp  --preinclude .\User\preinc.h  -o .\Objects\main.o  --depend .\Objects\main.d)
I (.\User\stm32.h)(0x5F000000)
F (.\bsp\led.c)(0x5F3A1B2D)(--c99 -c --cpu Cortex-M3 -D__DEBUG  -I.\User  --preinclude .\User\preinc.h  -o .\Objects\led.o)
"""


class TestParseDepText(unittest.TestCase):
    def test_extracts_source_files_in_order(self):
        r = k2c._parse_dep_text(SAMPLE_DEP)
        self.assertEqual(r["source_files"], ["./User/main.c", "./bsp/led.c"])

    def test_extracts_preinclude_deduped(self):
        r = k2c._parse_dep_text(SAMPLE_DEP)
        self.assertEqual(r["preinclude_files"], ["./User/preinc.h"])

    def test_toolchain_path_becomes_include(self):
        r = k2c._parse_dep_text(SAMPLE_DEP)
        self.assertEqual(r["system_includes"], ["D:/Keil_v5/ARM/ARMCC/Include"])

    def test_ignores_i_dependency_lines(self):
        r = k2c._parse_dep_text(SAMPLE_DEP)
        self.assertNotIn("./User/stm32.h", r["source_files"])

    def test_empty_text_returns_empty_lists(self):
        r = k2c._parse_dep_text("")
        self.assertEqual(r, {"system_includes": [], "preinclude_files": [], "source_files": []})

    def test_extracts_quoted_preinclude_with_spaces(self):
        dep = (
            r'F (.\User\main.c)(0x5F3A1B2C)(--c99 -c --cpu Cortex-M3 -D__DEBUG '
            r' -I.\User  --preinclude "./My Proj/preinc.h"  -o .\Objects\main.o)'
            + "\n"
        )
        r = k2c._parse_dep_text(dep)
        self.assertIn("./My Proj/preinc.h", r["preinclude_files"])


if __name__ == "__main__":
    unittest.main()
