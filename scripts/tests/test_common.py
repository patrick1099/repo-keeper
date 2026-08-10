import os
import sys
import json
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import k2c_common as common


class TestFormatPath(unittest.TestCase):
    def test_relative_uses_forward_slashes(self):
        base = Path(tempfile.gettempdir()).resolve()
        target = base / "a" / "b.c"
        self.assertEqual(common.format_path(target, base), "a/b.c")

    def test_absolute_mode(self):
        base = Path(tempfile.gettempdir()).resolve()
        target = base / "a" / "b.c"
        out = common.format_path(target, base, use_absolute=True)
        self.assertTrue(out.endswith("a/b.c"))
        self.assertNotIn("\\", out)


class TestClangdDoc(unittest.TestCase):
    def test_duplicate_flags_dropped(self):
        doc = common.ClangdDoc()
        doc.add_group("first", ["-DA", "-DB"])
        doc.add_group("second", ["-DB", "-DC"])
        text = doc.render()
        self.assertEqual(text.count("- -DB"), 1)
        self.assertIn("- -DC", text)

    def test_group_omitted_when_all_flags_seen(self):
        doc = common.ClangdDoc()
        doc.add_group("first", ["-DA"])
        doc.add_group("second", ["-DA"])
        self.assertNotIn("# second", doc.render())

    def test_allow_duplicates_keeps_repeated_tokens(self):
        # Two preinclude headers need two -imacros tokens; deduplicating would
        # silently drop the second header.
        doc = common.ClangdDoc()
        doc.add_group("pre", ["-imacros", "a.h", "-imacros", "b.h"],
                      allow_duplicates=True)
        text = doc.render()
        self.assertEqual(text.count("- -imacros"), 2)

    def test_render_has_both_sections(self):
        text = common.ClangdDoc().add_group("x", ["-DA"]).render()
        self.assertIn("CompileFlags:", text)
        self.assertIn("  Remove:", text)
        self.assertIn("Diagnostics:", text)
        self.assertTrue(text.endswith("\n"))


class TestCompileEntries(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.gettempdir()).resolve()

    def test_entry_shape(self):
        entry = common.make_compile_entries(
            "clang", ["-DA", "-Iinc"], [self.base / "m.c"], self.base)[0]
        self.assertEqual(entry["file"], "m.c")
        self.assertEqual(entry["arguments"], ["clang", "-c", "m.c", "-DA", "-Iinc"])
        self.assertEqual(entry["command"], "clang -c m.c -DA -Iinc")
        self.assertNotIn("\\", entry["directory"])

    def test_command_quotes_arguments_with_spaces(self):
        entry = common.make_compile_entries(
            "clang", ["-ID:/Program Files/inc"], [self.base / "m.c"], self.base)[0]
        self.assertIn('"-ID:/Program Files/inc"', entry["command"])
        # arguments stays unquoted -- it is already split
        self.assertIn("-ID:/Program Files/inc", entry["arguments"])

    def test_write_round_trips(self):
        with tempfile.TemporaryDirectory() as tmp:
            entries = common.make_compile_entries(
                "clang", ["-DA"], [Path(tmp) / "m.c"], tmp)
            common.write_compile_commands(entries, tmp)
            loaded = json.loads(
                (Path(tmp) / "compile_commands.json").read_text(encoding="utf-8"))
            self.assertEqual(loaded, entries)


class TestPlacement(unittest.TestCase):
    def test_output_above_sources_is_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            report = common.check_placement(root, [root / "Code" / "main.c"])
            self.assertTrue(report.ok)
            self.assertIn("OK", report.describe())

    def test_sibling_output_is_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            report = common.check_placement(root / "Proj",
                                            [root / "Code" / "main.c",
                                             root / "Code" / "bsp" / "b.c"])
            self.assertFalse(report.ok)
            self.assertIn("PROBLEM", report.describe())

    def test_anchor_is_deepest_dir_above_sources_only(self):
        # The anchor must not be dragged shallow by the output dir: it only has
        # to sit above the sources.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            report = common.check_placement(
                root / "Proj",
                [root / "Code" / "a" / "x.c", root / "Code" / "b" / "y.c"])
            self.assertEqual(report.anchor, root / "Code")

    def test_pointer_clangd_points_at_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            anchor = root / "Code"
            anchor.mkdir()
            common.write_pointer_clangd(root / "Proj", anchor)
            text = (anchor / ".clangd").read_text(encoding="utf-8")
            self.assertIn("CompilationDatabase: ../Proj", text)
            # diagnostics travel with the pointer: the real .clangd sits where
            # clangd will never read it for these sources
            self.assertIn("Diagnostics:", text)


class TestCommonAncestor(unittest.TestCase):
    def test_single_path(self):
        base = Path(tempfile.gettempdir()).resolve()
        self.assertEqual(common.common_ancestor([base / "a" / "b.c"]), base / "a")

    def test_empty(self):
        self.assertIsNone(common.common_ancestor([]))


class TestConfig(unittest.TestCase):
    def setUp(self):
        # Both files, always. load_config reads the predecessor's path as a
        # fallback, so a test that redirects only CONFIG_FILE still reads
        # whatever the developer happens to have in their home directory.
        self._config = common.CONFIG_FILE
        self._legacy = common.LEGACY_CONFIG_FILE
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        common.CONFIG_FILE = Path(self._tmp.name) / "cfg.json"
        common.LEGACY_CONFIG_FILE = Path(self._tmp.name) / "legacy.json"

    def tearDown(self):
        common.CONFIG_FILE = self._config
        common.LEGACY_CONFIG_FILE = self._legacy

    def test_missing_file_gives_empty_dict(self):
        self.assertEqual(common.load_config(), {})

    def test_set_then_get(self):
        common.config_set("iar_path", "D:/IAR")
        common.config_set("keil_path", "C:/Keil")
        self.assertEqual(common.config_get("iar_path"), "D:/IAR")
        self.assertEqual(common.config_get("keil_path"), "C:/Keil")

    def test_falls_back_to_the_predecessors_file(self):
        # A rename must not cost the user their probed toolchain paths.
        common.LEGACY_CONFIG_FILE.write_bytes(b'{"keil_path": "C:/OldKeil"}')
        self.assertEqual(common.config_get("keil_path"), "C:/OldKeil")

    def test_current_file_wins_over_the_legacy_one(self):
        common.LEGACY_CONFIG_FILE.write_bytes(b'{"keil_path": "C:/OldKeil"}')
        common.config_set("keil_path", "C:/NewKeil")
        self.assertEqual(common.config_get("keil_path"), "C:/NewKeil")

    def test_saving_never_writes_back_to_the_legacy_file(self):
        common.LEGACY_CONFIG_FILE.write_bytes(b'{"keil_path": "C:/OldKeil"}')
        common.config_set("iar_path", "D:/IAR")
        self.assertEqual(common.LEGACY_CONFIG_FILE.read_bytes(),
                         b'{"keil_path": "C:/OldKeil"}')


if __name__ == "__main__":
    unittest.main()
