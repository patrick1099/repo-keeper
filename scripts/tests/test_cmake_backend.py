import os
import sys
import json
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import Cmake2Clangd as cm
import k2c_common as common


def _project(tmp, build="build", compiler="clang", sources=("src/main.c",)):
    """A CMake-shaped tree with a build dir holding a compile database."""
    root = Path(tmp).resolve()
    (root / "CMakeLists.txt").write_text("project(x C)\n", encoding="utf-8")
    build_dir = root / build
    build_dir.mkdir(parents=True, exist_ok=True)
    entries = []
    for rel in sources:
        source = root / rel
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("int main(void){return 0;}\n", encoding="utf-8")
        entries.append({
            "directory": str(build_dir).replace("\\", "/"),
            "command": "{0} -c {1}".format(compiler, source),
            "file": str(source).replace("\\", "/"),
        })
    (build_dir / "compile_commands.json").write_text(
        json.dumps(entries), encoding="utf-8")
    return root, build_dir


class TestSourceRoot(unittest.TestCase):
    def test_found_at_the_directory_itself(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, _ = _project(tmp)
            self.assertEqual(cm.find_source_root(root), root)

    def test_found_by_walking_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, _ = _project(tmp)
            self.assertEqual(cm.find_source_root(root / "src"), root)

    def test_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(cm.find_source_root(Path(tmp) / "empty"))


class TestDatabaseDiscovery(unittest.TestCase):
    def test_explicit_build_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, build = _project(tmp)
            self.assertEqual(cm.find_database(root, build),
                             build / "compile_commands.json")

    def test_conventional_build_dirs_searched(self):
        for name in ("build", "cmake-build-debug"):
            with tempfile.TemporaryDirectory() as tmp:
                root, build = _project(tmp, build=name)
                self.assertEqual(cm.find_database(root),
                                 build / "compile_commands.json")

    def test_missing_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "CMakeLists.txt").write_text("project(x C)\n", encoding="utf-8")
            self.assertIsNone(cm.find_database(root))


class TestDatabase(unittest.TestCase):
    def test_sources_and_compilers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, build = _project(tmp, sources=("src/a.c", "src/b.c"))
            db = cm.Database(build / "compile_commands.json")
            self.assertEqual(len(db.source_files()), 2)
            self.assertEqual(db.compilers(), ["clang"])

    def test_relative_file_entries_resolve_against_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, build = _project(tmp)
            db_path = build / "compile_commands.json"
            db_path.write_text(json.dumps([{
                "directory": str(build).replace("\\", "/"),
                "command": "clang -c ../src/main.c",
                "file": "../src/main.c",
            }]), encoding="utf-8")
            self.assertEqual(cm.Database(db_path).source_files(),
                             [(root / "src" / "main.c").resolve()])

    def test_cross_driver_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, build = _project(tmp, compiler="arm-none-eabi-gcc")
            self.assertEqual(cm.Database(build / "compile_commands.json")
                             .cross_drivers(), ["arm-none-eabi-gcc"])

    def test_host_compiler_is_not_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, build = _project(tmp, compiler="clang")
            self.assertEqual(cm.Database(build / "compile_commands.json")
                             .cross_drivers(), [])


class TestPlacementForCmakeLayout(unittest.TestCase):
    def test_build_beside_src_is_the_failing_case(self):
        # The whole reason this backend exists: build/ is a sibling of src/,
        # and clangd never searches siblings.
        with tempfile.TemporaryDirectory() as tmp:
            root, build = _project(tmp)
            db = cm.Database(build / "compile_commands.json")
            report = common.check_placement(db.directory, db.source_files())
            self.assertFalse(report.ok)
            self.assertEqual(report.anchor, root / "src")

    def test_pointer_written_at_the_anchor(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, build = _project(tmp)
            rc = cm.main(["-p", str(root), "--no-configure"])
            self.assertEqual(rc, 0)
            pointer = root / "src" / ".clangd"
            self.assertTrue(pointer.is_file())
            self.assertIn("CompilationDatabase:",
                          pointer.read_text(encoding="utf-8"))

    def test_no_pointer_when_database_already_reachable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, _ = _project(tmp, build=".")
            rc = cm.main(["-p", str(root), "--no-configure"])
            self.assertEqual(rc, 0)
            self.assertFalse((root / "src" / ".clangd").exists())


class TestGenerator(unittest.TestCase):
    def test_explicit_generator_wins(self):
        self.assertEqual(cm.pick_generator("Ninja Multi-Config"),
                         "Ninja Multi-Config")

    def test_ide_generators_are_refused_before_running_cmake(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, _ = _project(tmp)
            result = cm.configure("cmake", root, root / "b", "Visual Studio 17 2022")
            self.assertFalse(result.ok)
            self.assertFalse(result.ran)
            self.assertIn("Ninja", result.reason)

    def test_missing_cmake_is_reported_not_raised(self):
        result = cm.configure(None, ".", "b")
        self.assertFalse(result.ok)
        self.assertIn("not found", result.reason)


class TestCompilerCheckDetection(unittest.TestCase):
    def test_link_failure_recognised(self):
        output = ("The C compiler is not able to compile a simple test program\n"
                  "lld-link: error: could not open 'advapi32.lib'\n"
                  "clang: error: linker command failed with exit code 1\n")
        self.assertTrue(cm._looks_like_compiler_check_failure(output))

    def test_cross_toolchain_link_failure_recognised(self):
        output = ("The C compiler is not able to compile a simple test program\n"
                  "ld: cannot find -lc\n")
        self.assertTrue(cm._looks_like_compiler_check_failure(output))

    def test_ordinary_project_error_not_mistaken_for_it(self):
        output = ("CMake Error at CMakeLists.txt:7 (add_executable):\n"
                  "  Cannot find source file: src/missing.c\n")
        self.assertFalse(cm._looks_like_compiler_check_failure(output))


class TestCli(unittest.TestCase):
    def test_no_cmakelists_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(cm.main(["-p", tmp, "--no-configure"]), 1)

    def test_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, _ = _project(tmp)
            self.assertEqual(cm.main(["-p", str(root), "--dry-run"]), 0)
            self.assertFalse((root / "src" / ".clangd").exists())


if __name__ == "__main__":
    unittest.main()
