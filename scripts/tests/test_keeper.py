import io
import os
import subprocess
import sys
import tempfile
import tomllib
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import Keeper  # noqa: E402
import local_config  # noqa: E402
from toolname import PROJECT_CONFIG_NAME  # noqa: E402


def _git(args, cwd):
    subprocess.run(["git"] + args, cwd=str(cwd), check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class KeeperTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / "repo"
        self.root.mkdir()
        _git(["init", "-q", "-b", "main"], self.root)
        _git(["config", "user.email", "t@t"], self.root)
        _git(["config", "user.name", "t"], self.root)
        (self.root / "main.c").write_bytes(b"int main(void){return 0;}\n")
        _git(["add", "-A"], self.root)
        _git(["commit", "-qm", "init"], self.root)

        # Never touch the real ~/<tool>/defaults.toml: a test that writes into
        # the developer's home directory changes the next run's behaviour.
        self.fake_global = self.base / "home" / "defaults.toml"
        self._orig = local_config.global_config_path
        local_config.global_config_path = lambda: self.fake_global

    def tearDown(self):
        local_config.global_config_path = self._orig

    def run_keeper(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = Keeper.main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    @property
    def project_cfg(self):
        return self.root / PROJECT_CONFIG_NAME


class TestProtectedBranch(KeeperTest):
    def test_refuses_to_set_up_on_a_protected_branch(self):
        # The premise of the whole plugin: you do not work on the shared
        # branch. Setting the repo up there would do every later step in the
        # checkout we just said not to use.
        rc, out, _ = self.run_keeper("init", "-p", str(self.root))
        self.assertEqual(rc, 1)
        self.assertIn("受保护分支", out)
        self.assertFalse(self.project_cfg.exists())
        self.assertFalse(self.fake_global.exists())

    def test_does_not_invent_a_worktree_location(self):
        _, out, _ = self.run_keeper("init", "-p", str(self.root))
        self.assertIn("放哪儿我不猜", out)
        self.assertEqual(list(self.base.glob("wt*")), [])

    def test_proceeds_on_an_unprotected_branch(self):
        _git(["checkout", "-q", "-b", "task/x"], self.root)
        rc, out, _ = self.run_keeper("init", "-p", str(self.root))
        self.assertEqual(rc, 0, out)
        self.assertTrue(self.project_cfg.exists())

    def test_protected_list_comes_from_config(self):
        self.fake_global.parent.mkdir(parents=True, exist_ok=True)
        self.fake_global.write_bytes(b'[branches]\nprotected = ["task/x"]\n')
        _git(["checkout", "-q", "-b", "task/x"], self.root)
        rc, out, _ = self.run_keeper("init", "-p", str(self.root))
        self.assertEqual(rc, 1)
        self.assertIn("受保护分支 task/x", out)


class TestWorktree(KeeperTest):
    def test_creates_the_worktree_and_continues_in_it(self):
        wt = self.base / "wt"
        rc, out, err = self.run_keeper("init", "-p", str(self.root),
                                       "--worktree", str(wt),
                                       "--branch", "task/demo")
        self.assertEqual(rc, 0, out + err)
        self.assertTrue((wt / "main.c").is_file())
        self.assertIn("以下步骤在新 worktree 里进行", out)

    def test_a_resolved_branch_warning_is_not_left_pending(self):
        # It used to still be listed as "your call" after being handled, which
        # reads as unfinished work that is in fact done.
        wt = self.base / "wt"
        rc, out, _ = self.run_keeper("init", "-p", str(self.root),
                                     "--worktree", str(wt), "--branch", "task/d")
        self.assertEqual(rc, 0)
        self.assertIn("需要你决定 0 件", out)

    def test_refuses_a_non_empty_destination(self):
        wt = self.base / "wt"
        wt.mkdir()
        (wt / "something").write_bytes(b"x")
        rc, out, _ = self.run_keeper("init", "-p", str(self.root),
                                     "--worktree", str(wt), "--branch", "task/d")
        self.assertEqual(rc, 1)
        self.assertIn("已存在且非空", out)

    def test_project_config_lands_in_the_main_checkout(self):
        # One file at the top of the main checkout serves every linked
        # worktree; a copy per worktree is how the two drift apart.
        wt = self.base / "wt"
        self.run_keeper("init", "-p", str(self.root), "--worktree", str(wt),
                        "--branch", "task/demo")
        self.assertTrue(self.project_cfg.is_file())
        self.assertFalse((wt / PROJECT_CONFIG_NAME).exists())


class TestConfigTemplates(KeeperTest):
    def setUp(self):
        super().setUp()
        _git(["checkout", "-q", "-b", "task/x"], self.root)

    def test_both_templates_are_valid_toml(self):
        self.run_keeper("init", "-p", str(self.root))
        for path in (self.fake_global, self.project_cfg):
            with self.subTest(path=path):
                with open(path, "rb") as fh:
                    tomllib.load(fh)      # raises if the template is malformed

    def test_templates_are_utf8_on_disk(self):
        # write_text would use the locale encoding (cp936 here) and tomllib
        # requires UTF-8 -- Chinese comments would come back unparseable.
        self.run_keeper("init", "-p", str(self.root))
        self.project_cfg.read_bytes().decode("utf-8")
        self.fake_global.read_bytes().decode("utf-8")

    def test_project_template_leaves_the_required_keys_unset(self):
        # A guessed branch ref points the whole run at the wrong branch and
        # still looks like it worked, so the template must not fill one in.
        self.run_keeper("init", "-p", str(self.root))
        cfg = local_config.load(start=self.root)
        for key in ("branches.clean", "branches.work", "paths.code"):
            self.assertFalse(cfg.has(key), key)

    def test_existing_configs_are_never_overwritten(self):
        self.project_cfg.write_bytes(b'[branches]\nclean = "mine"\n')
        self.fake_global.parent.mkdir(parents=True, exist_ok=True)
        self.fake_global.write_bytes(b"[scan]\ndepth = 7\n")
        rc, out, _ = self.run_keeper("init", "-p", str(self.root))
        self.assertEqual(rc, 0, out)
        self.assertIn(b'clean = "mine"', self.project_cfg.read_bytes())
        self.assertIn(b"depth = 7", self.fake_global.read_bytes())

    def test_project_config_is_ignored_by_git(self):
        # It holds branch refs and the reasons behind deliberate divergence --
        # private notes about a shared repo. It must never be offered for commit.
        self.run_keeper("init", "-p", str(self.root))
        status = subprocess.run(
            ["git", "-C", str(self.root), "status", "--porcelain"],
            capture_output=True, encoding="utf-8").stdout
        self.assertNotIn(PROJECT_CONFIG_NAME, status)

    def test_ignore_rule_went_to_info_exclude_not_gitignore(self):
        self.run_keeper("init", "-p", str(self.root))
        self.assertFalse((self.root / ".gitignore").exists())
        exclude = (self.root / ".git" / "info" / "exclude").read_text(
            encoding="utf-8")
        self.assertIn(PROJECT_CONFIG_NAME, exclude)


class TestDryRunAndErrors(KeeperTest):
    def test_dry_run_writes_nothing(self):
        _git(["checkout", "-q", "-b", "task/x"], self.root)
        self.run_keeper("init", "-p", str(self.root), "--dry-run")
        self.assertFalse(self.project_cfg.exists())
        self.assertFalse(self.fake_global.exists())

    def test_outside_a_repo_exits_two(self):
        outside = self.base / "nope"
        outside.mkdir()
        rc, _, err = self.run_keeper("init", "-p", str(outside))
        self.assertEqual(rc, 2)
        self.assertIn("不在 git 仓库", err)

    def test_second_run_is_clean(self):
        _git(["checkout", "-q", "-b", "task/x"], self.root)
        self.run_keeper("init", "-p", str(self.root))
        rc, out, err = self.run_keeper("init", "-p", str(self.root))
        self.assertEqual(rc, 0, out + err)
        self.assertIn("不动它", out)

    def test_explain_reports_both_layers(self):
        _git(["checkout", "-q", "-b", "task/x"], self.root)
        self.run_keeper("init", "-p", str(self.root))
        rc, out, _ = self.run_keeper("explain", "-p", str(self.root))
        self.assertEqual(rc, 0)
        self.assertIn("global", out)
        self.assertIn("project", out)


if __name__ == "__main__":
    unittest.main()
