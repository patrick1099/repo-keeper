import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import local_config as lc  # noqa: E402
from toolname import PROJECT_CONFIG_NAME  # noqa: E402


def write_toml(path, text):
    """Write a TOML fixture.

    encode('utf-8') explicitly rather than write_text: on this machine the
    locale encoding is cp936, and tomllib requires UTF-8 -- a fixture with
    Chinese in it would fail to parse for a reason that has nothing to do
    with what the test is checking.
    """
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_bytes(text.encode("utf-8"))


def _git(args, cwd):
    subprocess.run(["git"] + args, cwd=str(cwd), check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class TestMerge(unittest.TestCase):
    """Merge semantics -- the part whose mistakes are invisible at runtime."""

    def merge(self, g, p):
        return lc.merge_layers([(lc.GLOBAL, g), (lc.PROJECT, p)])

    def test_project_scalar_beats_global(self):
        merged, origins = self.merge({"scan": {"depth": 60}},
                                     {"scan": {"depth": 200}})
        self.assertEqual(merged["scan"]["depth"], 200)
        self.assertEqual(origins["scan.depth"], lc.PROJECT)

    def test_table_deep_merges_without_clearing_the_rest(self):
        # The whole reason tables merge instead of replacing: adding one
        # deliberate-drift entry in a project must not silently drop the
        # global ones.
        merged, origins = self.merge(
            {"expected_drift": {"a.c": "global reason", "b.c": "global reason"}},
            {"expected_drift": {"b.c": "project reason", "c.c": "new"}})
        self.assertEqual(merged["expected_drift"], {
            "a.c": "global reason",
            "b.c": "project reason",
            "c.c": "new",
        })
        self.assertEqual(origins["expected_drift.a.c"], lc.GLOBAL)
        self.assertEqual(origins["expected_drift.b.c"], lc.PROJECT)
        self.assertEqual(origins["expected_drift.c.c"], lc.PROJECT)

    def test_array_replaces_wholesale_and_is_not_concatenated(self):
        # Concatenating would make an inherited list impossible to shrink.
        merged, _ = self.merge({"paths": {"code": ["App", "Boot", "drv"]}},
                               {"paths": {"code": ["src"]}})
        self.assertEqual(merged["paths"]["code"], ["src"])

    def test_inline_tables_deep_merge_too(self):
        # identity.clean lives in the global layer, identity.work may be
        # overridden per project; keeping only one of them would be wrong.
        merged, origins = self.merge(
            {"identity": {"clean": {"name": "a"}, "work": {"name": "b"}}},
            {"identity": {"work": {"name": "c"}}})
        self.assertEqual(merged["identity"]["clean"]["name"], "a")
        self.assertEqual(merged["identity"]["work"]["name"], "c")
        self.assertEqual(origins["identity.clean.name"], lc.GLOBAL)
        self.assertEqual(origins["identity.work.name"], lc.PROJECT)

    def test_project_scalar_shadows_a_global_table_entirely(self):
        merged, origins = self.merge({"filters": {"strip_script": "x"}},
                                     {"filters": "disabled"})
        self.assertEqual(merged["filters"], "disabled")
        self.assertEqual(origins["filters"], lc.PROJECT)
        # The shadowed sub-keys are gone, so they must not claim an origin.
        self.assertNotIn("filters.strip_script", origins)

    def test_global_only_keys_survive(self):
        merged, origins = self.merge({"identity": {"clean": {"name": "a"}}},
                                     {"branches": {"clean": "c"}})
        self.assertEqual(merged["identity"]["clean"]["name"], "a")
        self.assertEqual(origins["identity.clean.name"], lc.GLOBAL)
        self.assertEqual(origins["branches.clean"], lc.PROJECT)


class TestLoad(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.gp = self.root / "defaults.toml"
        self.pp = self.root / PROJECT_CONFIG_NAME

    def test_both_layers(self):
        write_toml(self.gp, '[scan]\ndepth = 60\n')
        write_toml(self.pp, '[branches]\nclean = "customer/x"\n')
        cfg = lc.load(global_path=self.gp, project_path=self.pp)
        self.assertEqual(cfg.get("scan.depth"), 60)
        self.assertEqual(cfg.get("branches.clean"), "customer/x")
        self.assertEqual(cfg.origin("scan.depth"), lc.GLOBAL)
        self.assertEqual(cfg.origin("branches.clean"), lc.PROJECT)

    def test_missing_global_layer_is_not_an_error(self):
        # The project layer has to be able to stand alone -- a fresh machine
        # has no global file yet.
        write_toml(self.pp, '[branches]\nclean = "c"\nwork = "w"\n')
        cfg = lc.load(global_path=self.root / "nope.toml", project_path=self.pp)
        self.assertEqual(cfg.get("branches.clean"), "c")
        self.assertIsNone(cfg.sources[lc.GLOBAL])
        self.assertEqual(cfg.sources[lc.PROJECT], str(self.pp))

    def test_missing_project_layer_is_not_an_error(self):
        write_toml(self.gp, '[scan]\ndepth = 7\n')
        cfg = lc.load(global_path=self.gp, project_path=self.root / "nope.toml")
        self.assertEqual(cfg.get("scan.depth"), 7)
        self.assertIsNone(cfg.sources[lc.PROJECT])

    def test_both_missing_gives_an_empty_config_not_a_crash(self):
        cfg = lc.load(global_path=self.root / "a", project_path=self.root / "b")
        self.assertIsNone(cfg.get("branches.clean"))
        self.assertEqual(cfg.explain().count("(不存在"), 2)

    def test_malformed_toml_names_the_file(self):
        write_toml(self.pp, "this is not = = toml\n")
        with self.assertRaises(lc.ConfigError) as ctx:
            lc.load(global_path=self.root / "nope", project_path=self.pp)
        self.assertIn(PROJECT_CONFIG_NAME, str(ctx.exception))

    def test_get_default_for_absent_key(self):
        cfg = lc.load(global_path=self.root / "a", project_path=self.root / "b")
        self.assertEqual(cfg.get("branches.clean", "fallback"), "fallback")
        self.assertFalse(cfg.has("branches.clean"))


class TestRequire(unittest.TestCase):
    def cfg(self, data):
        merged, origins = lc.merge_layers([(lc.PROJECT, data)])
        return lc.Config(merged, origins, {lc.GLOBAL: None, lc.PROJECT: None})

    def test_present_keys_come_back_in_order(self):
        cfg = self.cfg({"branches": {"clean": "c", "work": "w"}})
        self.assertEqual(cfg.require("branches.work", "branches.clean"),
                         ["w", "c"])

    def test_missing_key_raises_and_names_the_layer_to_fill(self):
        cfg = self.cfg({"branches": {"clean": "c"}})
        with self.assertRaises(lc.ConfigError) as ctx:
            cfg.require("branches.clean", "branches.work", "paths.code")
        msg = str(ctx.exception)
        self.assertIn("branches.work", msg)
        self.assertIn("paths.code", msg)
        self.assertNotIn("branches.clean\n", msg)   # the present one

    def test_all_missing_keys_reported_at_once(self):
        # One at a time would mean fix, re-run, get told about the next.
        cfg = self.cfg({})
        with self.assertRaises(lc.ConfigError) as ctx:
            cfg.require("branches.clean", "branches.work", "paths.code")
        self.assertIn("3 个必填项", str(ctx.exception))

    def test_message_points_at_a_real_path(self):
        cfg = self.cfg({})
        with self.assertRaises(lc.ConfigError) as ctx:
            cfg.require("identity.clean")
        # identity is a global-layer key, so the hint must point at the global
        # file, not at the repo.
        self.assertIn(str(lc.global_config_path()), str(ctx.exception))

    def test_no_default_is_ever_invented(self):
        cfg = self.cfg({})
        self.assertIsNone(cfg.get("branches.main"))


class TestExplain(unittest.TestCase):
    def test_marks_the_layer_of_every_leaf(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        gp, pp = root / "defaults.toml", root / PROJECT_CONFIG_NAME
        write_toml(gp, '[identity]\nclean = { name = "a" }\n[scan]\ndepth = 60\n')
        write_toml(pp, '[scan]\ndepth = 5\n[branches]\nclean = "c"\n')
        text = lc.load(global_path=gp, project_path=pp).explain()

        self.assertIn(str(gp), text)
        self.assertIn(str(pp), text)
        for line in text.splitlines():
            if "identity.clean.name" in line:
                self.assertIn(lc.GLOBAL, line)
            if "scan.depth" in line:
                # overridden -> must be attributed to project, not global
                self.assertIn(lc.PROJECT, line)
            if "branches.clean" in line:
                self.assertIn(lc.PROJECT, line)


class TestDiscovery(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "main"
        self.root.mkdir()
        _git(["init", "-q", "-b", "main"], self.root)
        _git(["config", "user.email", "t@t"], self.root)
        _git(["config", "user.name", "t"], self.root)
        (self.root / "f.txt").write_bytes(b"x\n")
        _git(["add", "-A"], self.root)
        _git(["commit", "-qm", "init"], self.root)

    def test_found_at_the_worktree_root_from_a_subdirectory(self):
        write_toml(self.root / PROJECT_CONFIG_NAME, '[branches]\nclean = "c"\n')
        sub = self.root / "a" / "b"
        sub.mkdir(parents=True)
        self.assertEqual(lc.project_config_path(sub),
                         self.root / PROJECT_CONFIG_NAME)

    def test_absent_gives_none_not_an_exception(self):
        self.assertIsNone(lc.project_config_path(self.root))

    def test_linked_worktree_falls_back_to_the_main_checkout(self):
        # One config at the top of the main checkout has to serve every linked
        # worktree -- clean and work branches each get their own, and copying
        # the file into both is how they drift apart.
        write_toml(self.root / PROJECT_CONFIG_NAME, '[branches]\nclean = "c"\n')
        linked = Path(self.tmp.name) / "wt"
        _git(["worktree", "add", "-q", "-b", "side", str(linked)], self.root)
        self.assertFalse((linked / PROJECT_CONFIG_NAME).exists())
        self.assertEqual(lc.project_config_path(linked),
                         self.root / PROJECT_CONFIG_NAME)

    def test_worktree_own_copy_wins_over_the_main_one(self):
        write_toml(self.root / PROJECT_CONFIG_NAME, '[branches]\nclean = "main"\n')
        linked = Path(self.tmp.name) / "wt2"
        _git(["worktree", "add", "-q", "-b", "side2", str(linked)], self.root)
        write_toml(linked / PROJECT_CONFIG_NAME, '[branches]\nclean = "own"\n')
        self.assertEqual(lc.project_config_path(linked),
                         linked / PROJECT_CONFIG_NAME)

    def test_outside_any_repo_gives_none(self):
        outside = Path(self.tmp.name) / "not-a-repo"
        outside.mkdir()
        self.assertIsNone(lc.project_config_path(outside))


if __name__ == "__main__":
    unittest.main()
