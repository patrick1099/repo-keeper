import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import Iar2Clangd as iar
import k2c_common as common


class _FakeProbe:
    """Stands in for a real --predef_macros run."""

    def __init__(self, lines, ok=True):
        self.macros = list(lines)
        self.ok = ok and bool(lines)
        self.error = None if self.ok else "probe failed"

    macro_value = iar.PredefProbe.macro_value
    defined_names = iar.PredefProbe.defined_names
    char_is_unsigned = iar.PredefProbe.char_is_unsigned


RL78_MACROS = [
    "#define __ICCRL78__ 1",
    "#define __CORE__ 3",
    "#define __RL78_0__ 5",
    "#define __RL78_1__ 3",
    "#define __RL78_2__ 4",
    "#define __INT_SIZE__ 2",
    "#define __DEF_PTR_SIZE__ 2",
    "#define __CHAR_MIN__ 0",
    "#define __DATA_MEM2__ __near",
    "#define __CODE_MEM1__ __near_func",
]


class TestProbeParsing(unittest.TestCase):
    def setUp(self):
        self.probe = _FakeProbe(RL78_MACROS)

    def test_macro_value(self):
        self.assertEqual(self.probe.macro_value("__CORE__"), "3")
        self.assertIsNone(self.probe.macro_value("__NOPE__"))

    def test_defined_names(self):
        self.assertIn("__ICCRL78__", self.probe.defined_names())
        self.assertNotIn("__near", self.probe.defined_names())

    def test_char_signedness_read_not_guessed(self):
        self.assertTrue(self.probe.char_is_unsigned())
        self.assertFalse(_FakeProbe(["#define __CHAR_MIN__ (-128)"]).char_is_unsigned())
        self.assertIsNone(_FakeProbe(["#define __X__ 1"]).char_is_unsigned())


class TestCoreCandidates(unittest.TestCase):
    def test_candidates_derived_from_probe_macros(self):
        found = iar.CoreNegotiator.candidates_from(_FakeProbe(RL78_MACROS), "RL78")
        self.assertEqual(found, ["rl78_0", "rl78_1", "rl78_2"])

    def test_memory_macros_are_not_mistaken_for_cores(self):
        found = iar.CoreNegotiator.candidates_from(_FakeProbe(RL78_MACROS), "RL78")
        self.assertNotIn("data_mem2", found)
        self.assertNotIn("code_mem1", found)

    def test_no_candidates_without_a_probe(self):
        self.assertEqual(iar.CoreNegotiator.candidates_from(None, "RL78"), [])
        self.assertEqual(
            iar.CoreNegotiator.candidates_from(_FakeProbe([], ok=False), "RL78"), [])

    def test_device_header_name(self):
        negotiator = iar.CoreNegotiator(None, "RL78", "R5F10WMG")
        self.assertEqual(negotiator.device_header(), "ior5f10wmg.h")
        self.assertIsNone(iar.CoreNegotiator(None, "RL78", None).device_header())

    def test_explicit_core_is_not_overridden(self):
        negotiator = iar.CoreNegotiator("cc", "RL78", "R5F10WMG",
                                        base_args=["--core", "rl78_2"])
        negotiator.run(["rl78_0"])
        self.assertEqual(negotiator.status, "overridden")
        self.assertIsNone(negotiator.chosen)


class TestTargetChoice(unittest.TestCase):
    def test_arm_gets_its_real_triple(self):
        triple, undefs = iar.choose_target("ARM", _FakeProbe(RL78_MACROS))
        self.assertEqual(triple, "arm-none-eabi")
        self.assertNotIn("__MSP430__", undefs)

    def test_backendless_arch_gets_a_size_matched_standin(self):
        triple, undefs = iar.choose_target("RL78", _FakeProbe(RL78_MACROS))
        self.assertEqual(triple, "msp430-none-elf")
        # the stand-in's identity must be undone so code testing it is not fooled
        self.assertIn("__MSP430__", undefs)

    def test_32bit_arch_gets_a_32bit_standin(self):
        triple, _ = iar.choose_target(
            "RX", _FakeProbe(["#define __DEF_PTR_SIZE__ 4"]))
        self.assertEqual(triple, "i386-none-elf")

    def test_a_target_is_always_chosen(self):
        # Falling back to the host triple is not an option: on Windows that is
        # an MSVC triple whose predeclared size_t collides with IAR's.
        triple, _ = iar.choose_target("RL78", None)
        self.assertTrue(triple)

    def test_override_wins_and_empty_means_omit(self):
        self.assertEqual(iar.choose_target("RL78", None, "thumbv7m-none-eabi")[0],
                         "thumbv7m-none-eabi")
        self.assertIsNone(iar.choose_target("RL78", None, "")[0])

    def test_clang_identity_always_undone(self):
        for toolchain in ("ARM", "RL78"):
            _, undefs = iar.choose_target(toolchain, _FakeProbe(RL78_MACROS))
            self.assertIn("__GNUC__", undefs)
            self.assertIn("__clang__", undefs)


class TestPredefHeader(unittest.TestCase):
    def test_probed_macros_are_emitted(self):
        text = iar.render_predef_header(_FakeProbe(RL78_MACROS), "RL78", "ICCRL78")
        self.assertIn("#define __ICCRL78__ 1", text)
        self.assertIn("#define __CORE__ 3", text)

    def test_extended_keywords_shimmed(self):
        text = iar.render_predef_header(_FakeProbe(RL78_MACROS), "RL78", "ICCRL78")
        # not macros in the compiler, so they never come from the probe
        for keyword in ("__near", "__saddr", "__interrupt", "__no_init"):
            self.assertIn("#define {0}\n".format(keyword), text)
        self.assertIn("#define __weak __attribute__((weak))", text)

    def test_shim_skipped_when_probe_already_defines_the_name(self):
        probe = _FakeProbe(RL78_MACROS + ["#define __root 1"])
        text = iar.render_predef_header(probe, "RL78", "ICCRL78")
        self.assertNotIn("#ifndef __root", text)

    def test_undefs_emitted_first(self):
        text = iar.render_predef_header(_FakeProbe(RL78_MACROS), "RL78", "ICCRL78",
                                        ["__MSP430__", "__GNUC__"])
        self.assertIn("#undef __MSP430__", text)
        self.assertLess(text.index("#undef __GNUC__"),
                        text.index("#define __ICCRL78__"))

    def test_failed_probe_still_produces_a_usable_header(self):
        text = iar.render_predef_header(_FakeProbe([], ok=False), "RL78", "ICCRL78")
        self.assertIn("__IAR_SYSTEMS_ICC__", text)
        self.assertIn("__ICCRL78__", text)
        self.assertIn("UNAVAILABLE", text)

    def test_header_is_include_guarded(self):
        text = iar.render_predef_header(_FakeProbe(RL78_MACROS), "RL78", "ICCRL78")
        self.assertIn("#ifndef K2C_IAR_PREDEF_H", text)
        self.assertTrue(text.rstrip().endswith("#endif /* K2C_IAR_PREDEF_H */"))


class TestPathResolver(unittest.TestCase):
    def setUp(self):
        # Isolate from this machine: no scanning real install locations, no
        # reading or writing the user's real toolchain config. BOTH paths have
        # to be redirected -- load_config falls back to the predecessor's file,
        # so patching only CONFIG_FILE would let a real ~/.keil2clangd.json
        # leak in and make the outcome depend on whose machine runs the suite.
        self._globs = iar.IAR_ROOT_GLOBS
        self._config = common.CONFIG_FILE
        self._legacy = common.LEGACY_CONFIG_FILE
        self._env = os.environ.pop('IAR_PATH', None)
        self._tmpdir = tempfile.TemporaryDirectory()
        iar.IAR_ROOT_GLOBS = []
        common.CONFIG_FILE = Path(self._tmpdir.name) / "cfg.json"
        common.LEGACY_CONFIG_FILE = Path(self._tmpdir.name) / "legacy.json"

    def tearDown(self):
        iar.IAR_ROOT_GLOBS = self._globs
        common.CONFIG_FILE = self._config
        common.LEGACY_CONFIG_FILE = self._legacy
        if self._env is not None:
            os.environ['IAR_PATH'] = self._env
        self._tmpdir.cleanup()

    def _workbench(self, tmp):
        root = Path(tmp) / "Embedded Workbench 8.0"
        (root / "rl78" / "inc" / "c").mkdir(parents=True)
        (root / "rl78" / "bin").mkdir(parents=True)
        (root / "rl78" / "lib").mkdir(parents=True)
        (root / "rl78" / "bin" / "iccrl78.exe").write_text("", encoding="utf-8")
        return root

    def test_toolkit_dir_from_toolchain_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._workbench(tmp)
            resolver = iar.IarPathResolver(str(root), "RL78", "ICCRL78",
                                           interactive=False)
            self.assertTrue(resolver.found())
            self.assertEqual(resolver.toolkit_dir.name, "rl78")

    def test_compiler_exe_from_settings_node_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._workbench(tmp)
            resolver = iar.IarPathResolver(str(root), "RL78", "ICCRL78",
                                           interactive=False)
            self.assertEqual(resolver.get_compiler_exe().name, "iccrl78.exe")

    def test_lib_dir_only_added_when_it_holds_headers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._workbench(tmp)
            resolver = iar.IarPathResolver(str(root), "RL78", "ICCRL78",
                                           interactive=False)
            self.assertNotIn(root / "rl78" / "lib", resolver.get_system_includes())
            (root / "rl78" / "lib" / "DLib_Config_Normal.h").write_text(
                "", encoding="utf-8")
            self.assertIn(root / "rl78" / "lib", resolver.get_system_includes())

    def test_missing_install_degrades_quietly(self):
        with tempfile.TemporaryDirectory() as tmp:
            resolver = iar.IarPathResolver(str(Path(tmp) / "nope"), "RL78",
                                           "ICCRL78", interactive=False)
            self.assertFalse(resolver.found())
            self.assertEqual(resolver.get_system_includes(), [])
            self.assertIsNone(resolver.get_compiler_exe())


if __name__ == "__main__":
    unittest.main()
