import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ReAnchor import _is_windows_abs, remap_dead_path, fix_flag_value


class TestIsWindowsAbs(unittest.TestCase):
    def test_drive_paths_are_absolute(self):
        self.assertTrue(_is_windows_abs("C:/Keil_v5/ARM"))
        self.assertTrue(_is_windows_abs("d:\\Keil_v5"))

    def test_relative_paths_are_not(self):
        self.assertFalse(_is_windows_abs("App/Code"))
        self.assertFalse(_is_windows_abs("../up"))
        self.assertFalse(_is_windows_abs("-IApp"))


class TestRemapDeadPath(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.keil = Path(self.tmp.name) / "Keil_v5"
        (self.keil / "ARM" / "ARMCLANG" / "include").mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_remaps_via_arm_suffix_when_target_exists(self):
        got = remap_dead_path("D:/OldKeil/ARM/ARMCLANG/include", self.keil)
        self.assertEqual(got, str(self.keil / "ARM/ARMCLANG/include").replace("\\", "/"))

    def test_none_when_suffix_missing_under_new_root(self):
        self.assertIsNone(remap_dead_path("D:/OldKeil/ARM/PACK/ARM/CMSIS/9.9.9/x", self.keil))

    def test_none_without_arm_marker_or_keil_root(self):
        self.assertIsNone(remap_dead_path("D:/Other/include", self.keil))
        self.assertIsNone(remap_dead_path("D:/OldKeil/ARM/ARMCLANG/include", None))

    def test_none_when_keil_root_is_empty_string(self):
        # empty string (not None) must not fall through to Path('') / suffix,
        # which resolves relative to CWD instead of being treated as unknown.
        # Prove it by chdir-ing somewhere the /ARM/... suffix genuinely exists
        # under CWD: a buggy `if keil_root is None` guard would find it there
        # and "remap" onto an unrelated directory; the fix must still say None.
        cwd_keil = Path(self.tmp.name) / "cwd_root"
        (cwd_keil / "ARM" / "ARMCLANG" / "include").mkdir(parents=True)
        old_cwd = os.getcwd()
        os.chdir(str(cwd_keil))
        try:
            self.assertIsNone(remap_dead_path("D:/OldKeil/ARM/ARMCLANG/include", ""))
        finally:
            os.chdir(old_cwd)


class TestFixFlagValue(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.keil = Path(self.tmp.name) / "Keil_v5"
        (self.keil / "ARM" / "ARMCLANG" / "include").mkdir(parents=True)
        self.alive = Path(self.tmp.name) / "alive"
        self.alive.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_relative_never_touched(self):
        self.assertEqual(fix_flag_value("App/Code", self.keil), (None, None))

    def test_alive_absolute_never_touched(self):
        p = str(self.alive).replace("\\", "/")
        self.assertEqual(fix_flag_value(p, self.keil), (None, None))

    def test_dead_and_remappable_is_fixed(self):
        new, status = fix_flag_value("D:/OldKeil/ARM/ARMCLANG/include", self.keil)
        self.assertEqual(status, "fixed")
        self.assertTrue(new.endswith("ARM/ARMCLANG/include"))

    def test_dead_and_unmappable_is_dead(self):
        self.assertEqual(fix_flag_value("D:/Gone/NoArm/include", self.keil), (None, "dead"))
        self.assertEqual(fix_flag_value("D:/OldKeil/ARM/ARMCLANG/include", None), (None, "dead"))

    def test_dead_with_empty_string_keil_root_is_dead(self):
        # keil_root='' (falsy but not None) must behave like "unknown", not like
        # a resolved root -- otherwise Path('') / suffix resolves against CWD.
        # Prove it the same way as TestRemapDeadPath: chdir somewhere the
        # /ARM/... suffix genuinely exists under CWD.
        cwd_keil = Path(self.tmp.name) / "cwd_root2"
        (cwd_keil / "ARM" / "ARMCLANG" / "include").mkdir(parents=True)
        old_cwd = os.getcwd()
        os.chdir(str(cwd_keil))
        try:
            self.assertEqual(
                fix_flag_value("D:/OldKeil/ARM/ARMCLANG/include", ""), (None, "dead"))
        finally:
            os.chdir(old_cwd)


if __name__ == "__main__":
    unittest.main()
