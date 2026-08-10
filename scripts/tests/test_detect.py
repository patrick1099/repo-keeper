import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import Proj2Clangd as detect_mod


def _touch(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")


class TestDetect(unittest.TestCase):
    def test_keil(self):
        with tempfile.TemporaryDirectory() as tmp:
            _touch(Path(tmp) / "Proj" / "app.uvprojx")
            self.assertEqual([k for k, _, _ in detect_mod.detect(tmp)], ["keil"])

    def test_iar(self):
        with tempfile.TemporaryDirectory() as tmp:
            _touch(Path(tmp) / "Proj" / "app.ewp")
            self.assertEqual([k for k, _, _ in detect_mod.detect(tmp)], ["iar"])

    def test_cmake(self):
        with tempfile.TemporaryDirectory() as tmp:
            _touch(Path(tmp) / "CMakeLists.txt")
            self.assertEqual([k for k, _, _ in detect_mod.detect(tmp)], ["cmake"])

    def test_vendor_project_listed_before_cmake(self):
        with tempfile.TemporaryDirectory() as tmp:
            _touch(Path(tmp) / "app.ewp")
            _touch(Path(tmp) / "CMakeLists.txt")
            self.assertEqual([k for k, _, _ in detect_mod.detect(tmp)],
                             ["iar", "cmake"])

    def test_build_scaffolding_ignored(self):
        # A CMakeLists.txt fetched into _deps or emitted under CMakeFiles is not
        # this project.
        with tempfile.TemporaryDirectory() as tmp:
            _touch(Path(tmp) / "build" / "_deps" / "dep-src" / "CMakeLists.txt")
            _touch(Path(tmp) / "build" / "CMakeFiles" / "CMakeLists.txt")
            self.assertEqual(detect_mod.detect(tmp), [])

    def test_nothing_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(detect_mod.detect(tmp), [])


class TestCli(unittest.TestCase):
    def test_detect_only_succeeds_when_something_is_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            _touch(Path(tmp) / "app.ewp")
            self.assertEqual(detect_mod.main(["-p", tmp, "--detect-only"]), 0)

    def test_detect_only_fails_on_empty_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(detect_mod.main(["-p", tmp, "--detect-only"]), 1)

    def test_ambiguous_tree_refuses_to_guess(self):
        with tempfile.TemporaryDirectory() as tmp:
            _touch(Path(tmp) / "app.ewp")
            _touch(Path(tmp) / "CMakeLists.txt")
            self.assertEqual(detect_mod.main(["-p", tmp]), 1)

    def test_forced_kind_reaches_the_backend(self):
        with tempfile.TemporaryDirectory() as tmp:
            _touch(Path(tmp) / "CMakeLists.txt")
            _touch(Path(tmp) / "app.ewp")
            # cmake backend with no database -> its own error path, not the
            # ambiguity error, proving the dispatch happened
            self.assertEqual(
                detect_mod.main(["-p", tmp, "--kind", "cmake", "--no-configure"]), 1)


if __name__ == "__main__":
    unittest.main()
