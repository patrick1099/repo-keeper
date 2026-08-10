import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import Iar2Clangd as iar

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "sample.ewp")
PROJ_DIR = Path(FIX).parent


class TestConfigurations(unittest.TestCase):
    def test_lists_all_configurations(self):
        self.assertEqual(iar.EwpParser(FIX).list_configs(), ["Debug", "Release"])

    def test_first_configuration_is_default(self):
        self.assertEqual(iar.EwpParser(FIX).get_config_name(), "Debug")

    def test_named_configuration_selected(self):
        self.assertEqual(
            iar.EwpParser(FIX, config_name="Release").get_config_name(), "Release")

    def test_unknown_configuration_rejected(self):
        with self.assertRaises(ValueError):
            iar.EwpParser(FIX, config_name="Nope")


class TestCompilerNode(unittest.TestCase):
    def test_finds_iccrl78_not_just_iccarm(self):
        # The old Ewp2Json looked for a node literally named ICCARM, so every
        # non-ARM IAR project silently parsed as empty.
        self.assertEqual(iar.EwpParser(FIX).get_compiler_id(), "ICCRL78")

    def test_toolchain_name(self):
        self.assertEqual(iar.EwpParser(FIX).get_toolchain(), "RL78")


class TestBuildSettings(unittest.TestCase):
    def test_defines_are_per_configuration(self):
        self.assertEqual(iar.EwpParser(FIX).get_defines(),
                         ["_CODE_DEBUG_", "USE_ASSERT"])
        self.assertEqual(iar.EwpParser(FIX, config_name="Release").get_defines(),
                         ["DEF_RELEASE"])

    def test_include_paths_resolved_deduped_and_empty_dropped(self):
        paths = iar.EwpParser(FIX).get_include_paths()
        self.assertEqual(len(paths), 2)
        self.assertTrue(all(p.is_absolute() for p in paths))
        self.assertEqual(paths[0], (PROJ_DIR / ".." / "Code").resolve())

    def test_defines_by_config(self):
        by_config = iar.EwpParser(FIX).defines_by_config()
        self.assertEqual(by_config["Release"], {"DEF_RELEASE"})
        self.assertIn("USE_ASSERT", by_config["Debug"])

    def test_extra_options_only_when_enabled(self):
        self.assertEqual(iar.EwpParser(FIX).get_extra_options(), [])
        self.assertEqual(
            iar.EwpParser(FIX, config_name="Release").get_extra_options(),
            ["--no_cse"])

    def test_empty_preinclude_is_none(self):
        self.assertIsNone(iar.EwpParser(FIX).get_preinclude())


class TestGeneralSettings(unittest.TestCase):
    def test_device_strips_the_description_field(self):
        self.assertEqual(iar.EwpParser(FIX).get_device(), "R5F10WMG")

    def test_device_absent_is_none_not_crash(self):
        parser = iar.EwpParser(FIX, config_name="Release")
        self.assertEqual(parser.get_device(), "R5F10WMG")

    def test_dlib_config_needs_toolkit_dir(self):
        parser = iar.EwpParser(FIX)
        # Unexpanded $TOOLKIT_DIR$ must not be mistaken for a real path.
        self.assertIsNone(parser.get_dlib_config())
        resolved = parser.get_dlib_config(toolkit_dir=Path("D:/iar/rl78"))
        self.assertIsNotNone(resolved)
        self.assertTrue(str(resolved).replace("\\", "/").endswith(
            "iar/rl78/LIB/dlrl78fnd22n.h"))


class TestSourceFiles(unittest.TestCase):
    def test_nested_groups_are_walked(self):
        names = [p.name for p in iar.EwpParser(FIX).get_source_files()]
        self.assertIn("board.c", names)

    def test_headers_excluded_by_default(self):
        names = [p.name for p in iar.EwpParser(FIX).get_source_files()]
        self.assertNotIn("api.h", names)
        names = [p.name for p in
                 iar.EwpParser(FIX).get_source_files(include_headers=True)]
        self.assertIn("api.h", names)

    def test_per_configuration_exclusion_respected(self):
        debug = [p.name for p in iar.EwpParser(FIX).get_source_files()]
        release = [p.name for p in
                   iar.EwpParser(FIX, config_name="Release").get_source_files()]
        self.assertIn("debug_only.c", debug)
        self.assertNotIn("debug_only.c", release)


class TestExpansion(unittest.TestCase):
    def test_proj_dir_expanded(self):
        parser = iar.EwpParser(FIX)
        out = parser.expand(r"$PROJ_DIR$\x")
        self.assertNotIn("$PROJ_DIR$", out)
        self.assertNotIn("\\", out)

    def test_unknown_variable_left_for_the_caller_to_report(self):
        parser = iar.EwpParser(FIX)
        self.assertIn("$MYSTERY$", parser.expand("$MYSTERY$/x"))


if __name__ == "__main__":
    unittest.main()
