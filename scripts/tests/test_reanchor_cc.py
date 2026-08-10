import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ReAnchor import reanchor_entries

NEW_ROOT = "C:/NewPlace/Code"


def make_entry(dead_inc="D:/OldKeil/ARM/ARMCLANG/include"):
    args = ["arm-none-eabi-gcc", "-c", "App/main.c", "-D__DEBUG",
            "-IApp/Code", f"-I{dead_inc}",
            "-imacros", f"{dead_inc}/pre.h"]
    return {
        "command": " ".join(args),
        "arguments": list(args),
        "directory": "C:/old-machine/Proj/Code",
        "file": "App/main.c",
    }


class TestReanchorEntries(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.keil = Path(self.tmp.name) / "Keil_v5"
        inc = self.keil / "ARM" / "ARMCLANG" / "include"
        inc.mkdir(parents=True)
        (inc / "pre.h").write_text("", encoding="utf-8")
        self.new_inc = str(inc).replace("\\", "/")

    def tearDown(self):
        self.tmp.cleanup()

    def test_directory_rewritten_everywhere(self):
        entries = [make_entry(), make_entry()]
        reanchor_entries(entries, NEW_ROOT, self.keil)
        self.assertTrue(all(e["directory"] == NEW_ROOT for e in entries))

    def test_dead_toolchain_args_fixed_and_command_synced(self):
        entries = [make_entry()]
        reanchor_entries(entries, NEW_ROOT, self.keil)
        args = entries[0]["arguments"]
        self.assertIn(f"-I{self.new_inc}", args)
        self.assertIn(f"{self.new_inc}/pre.h", args)
        self.assertEqual(entries[0]["command"], " ".join(args))

    def test_relative_and_defines_untouched(self):
        entries = [make_entry()]
        reanchor_entries(entries, NEW_ROOT, self.keil)
        args = entries[0]["arguments"]
        self.assertIn("-IApp/Code", args)
        self.assertIn("-D__DEBUG", args)
        self.assertEqual(entries[0]["file"], "App/main.c")

    def test_command_untouched_when_only_directory_changes(self):
        alive = str(self.keil / "ARM" / "ARMCLANG" / "include").replace("\\", "/")
        entry = make_entry(dead_inc=alive)  # all -I alive
        original_command = "HAND-EDITED " + entry["command"]
        entry["command"] = original_command
        changes, dead = reanchor_entries([entry], NEW_ROOT, self.keil)
        self.assertEqual(entry["command"], original_command)
        self.assertEqual(entry["directory"], NEW_ROOT)
        self.assertEqual(dead, [])

    def test_idempotent(self):
        entries = [make_entry()]
        reanchor_entries(entries, NEW_ROOT, self.keil)
        changes, dead = reanchor_entries(entries, NEW_ROOT, self.keil)
        self.assertEqual(changes, [])
        self.assertEqual(dead, [])

    def test_unmappable_dead_reported(self):
        entries = [make_entry()]
        changes, dead = reanchor_entries(entries, NEW_ROOT, None)
        self.assertIn("D:/OldKeil/ARM/ARMCLANG/include", dead)
        self.assertEqual([c for c in changes if c[0] != "C:/old-machine/Proj/Code"], [])


if __name__ == "__main__":
    unittest.main()
