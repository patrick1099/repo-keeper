import io
import json
import os
import subprocess
import sys
import tempfile
import tomllib
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import Keeper  # noqa: E402
import local_config  # noqa: E402
from toolname import PROJECT_CONFIG_NAME, REANCHOR_EXE  # noqa: E402


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


class TestPushableBranch(KeeperTest):
    """能推到远端 = 禁地。名单是人维护的,远端不是 —— 闸架在远端那一侧。"""

    def setUp(self):
        super().setUp()
        # A bare repo standing in for the company remote. `git remote add` is
        # not enough: the gate asks the remote what it has.
        self.remote = self.base / "origin.git"
        _git(["init", "-q", "--bare", str(self.remote)], self.base)
        _git(["remote", "add", "origin", str(self.remote)], self.root)
        _git(["push", "-q", "origin", "main"], self.root)

    def test_a_branch_that_exists_on_the_remote_is_off_limits(self):
        # Not in `protected`, no upstream configured -- nothing local says it
        # is special. Only the remote does, and that is the whole point.
        _git(["push", "-q", "origin", "main:通用版"], self.root)
        _git(["checkout", "-q", "-b", "通用版"], self.root)
        rc, out, _ = self.run_keeper("init", "-p", str(self.root))
        self.assertEqual(rc, 1)
        self.assertIn("能直接推到远端", out)
        self.assertFalse(self.project_cfg.exists())

    def test_an_upstream_makes_a_branch_off_limits(self):
        _git(["checkout", "-q", "-b", "feat/x"], self.root)
        _git(["push", "-q", "-u", "origin", "feat/x"], self.root)
        rc, out, _ = self.run_keeper("init", "-p", str(self.root))
        self.assertEqual(rc, 1)
        self.assertIn("upstream", out)

    def test_a_local_only_branch_still_proceeds(self):
        _git(["checkout", "-q", "-b", "task/local"], self.root)
        rc, out, _ = self.run_keeper("init", "-p", str(self.root))
        self.assertEqual(rc, 0, out)
        self.assertTrue(self.project_cfg.exists())

    def test_a_no_push_branch_is_not_treated_as_pushable(self):
        # clean-branch's deliberate never-push hatch: pushRemote naming
        # something that is not a remote. Honouring it keeps the tool/debug
        # branch usable even though the remote has the same name.
        _git(["push", "-q", "origin", "main:debug"], self.root)
        _git(["checkout", "-q", "-b", "debug"], self.root)
        _git(["config", "branch.debug.pushRemote", "no_push"], self.root)
        rc, out, _ = self.run_keeper("init", "-p", str(self.root))
        self.assertEqual(rc, 0, out)

    def test_the_new_worktree_branch_is_vetted_too(self):
        # Escaping to a worktree is pointless if the branch you land on is
        # itself pushable.
        _git(["push", "-q", "origin", "main:通用版"], self.root)
        rc, out, _ = self.run_keeper("init", "-p", str(self.root),
                                     "--worktree", str(self.base / "wt"),
                                     "--branch", "通用版")
        self.assertEqual(rc, 1)
        self.assertIn("换个只在本机存在的名字", out)
        self.assertFalse((self.base / "wt").exists())

    def test_an_unreachable_remote_falls_back_and_says_so(self):
        _git(["push", "-q", "origin", "main:通用版"], self.root)
        _git(["fetch", "-q", "origin"], self.root)
        _git(["checkout", "-q", "-b", "通用版"], self.root)
        _git(["remote", "set-url", "origin", str(self.base / "gone.git")], self.root)
        rc, out, _ = self.run_keeper("init", "-p", str(self.root))
        self.assertEqual(rc, 1)
        self.assertIn("上次 fetch 的快照", out)


class TestWorktree(KeeperTest):
    def _add_reusable_keil_config(self, missing_source=False):
        proj = self.root / "Proj"
        code = self.root / "Code"
        proj.mkdir()
        code.mkdir()
        (proj / "Demo.uvprojx").write_bytes(b"<Project/>\n")
        (code / "main.c").write_bytes(b"int app(void){return 0;}\n")
        _git(["add", "Proj/Demo.uvprojx", "Code/main.c"], self.root)
        _git(["commit", "-qm", "add project"], self.root)
        source = "../Code/missing.c" if missing_source else "../Code/main.c"
        entries = [{
            "directory": str(proj).replace("\\", "/"),
            "file": source,
            "arguments": ["clang", "-c", source],
        }]
        (proj / "compile_commands.json").write_text(
            json.dumps(entries), encoding="utf-8")
        (proj / ".clangd").write_bytes(b"CompileFlags:\n  Add: [-DTEST]\n")
        (self.root / REANCHOR_EXE).write_bytes(b"test exe")
        info_exclude = self.root / ".git" / "info" / "exclude"
        with open(info_exclude, "a", encoding="utf-8") as fh:
            fh.write("\nlocal-cache.bin\n")
        (self.root / "local-cache.bin").write_bytes(b"cache")
        return proj

    def test_creates_the_worktree_and_continues_in_it(self):
        wt = self.base / "wt"
        rc, out, err = self.run_keeper("init", "-p", str(self.root),
                                       "--worktree", str(wt),
                                       "--branch", "task/demo")
        self.assertEqual(rc, 0, out + err)
        self.assertTrue((wt / "main.c").is_file())
        self.assertIn("以下步骤在新 worktree 里进行", out)

    def test_copies_ignored_files_and_reanchors_instead_of_regenerating(self):
        proj = self._add_reusable_keil_config()
        wt = self.base / "wt"
        with mock.patch.object(Keeper, "step_clangd",
                               side_effect=AssertionError("must not regenerate")):
            rc, out, err = self.run_keeper(
                "init", "-p", str(self.root), "--worktree", str(wt),
                "--branch", "task/reuse")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual((wt / "local-cache.bin").read_bytes(), b"cache")
        self.assertTrue((wt / ".git").is_file())
        self.assertFalse((wt / PROJECT_CONFIG_NAME).exists())
        entries = json.loads((wt / proj.relative_to(self.root) /
                              "compile_commands.json").read_text(encoding="utf-8"))
        self.assertEqual(
            entries[0]["directory"],
            str(wt / proj.relative_to(self.root)).replace("\\", "/"))
        self.assertIn("完成路径重锚定", out)

    def test_does_not_copy_over_a_different_branch_baseline(self):
        (self.root / "second.c").write_bytes(b"second\n")
        _git(["add", "second.c"], self.root)
        _git(["commit", "-qm", "second"], self.root)
        first = subprocess.run(
            ["git", "-C", str(self.root), "rev-parse", "HEAD~1"],
            check=True, capture_output=True, encoding="utf-8").stdout.strip()
        _git(["branch", "wt", first], self.root)
        (self.root / "source-only.bin").write_bytes(b"source")
        wt = self.base / "wt"
        rc, out, err = self.run_keeper(
            "init", "-p", str(self.root), "--worktree", str(wt))
        self.assertEqual(rc, 0, out + err)
        self.assertFalse((wt / "source-only.bin").exists())
        self.assertIn("基线提交与源工作区不同", out)

    def test_refuses_to_copy_visible_tracked_changes(self):
        (self.root / "main.c").write_bytes(b"changed\n")
        wt = self.base / "wt"
        rc, out, _ = self.run_keeper(
            "init", "-p", str(self.root), "--worktree", str(wt),
            "--branch", "task/dirty")
        self.assertEqual(rc, 1)
        self.assertIn("源工作区有已跟踪改动", out)
        self.assertFalse(wt.exists())

    def test_existing_worktree_is_not_overwritten_from_source(self):
        wt = self.base / "wt"
        _git(["worktree", "add", "-q", "-b", "task/existing", str(wt)], self.root)
        (self.root / "source-only.bin").write_bytes(b"source")
        rc, out, err = self.run_keeper(
            "init", "-p", str(self.root), "--worktree", str(wt),
            "--branch", "task/existing")
        self.assertEqual(rc, 0, out + err)
        self.assertFalse((wt / "source-only.bin").exists())

    def test_reanchor_failure_falls_back_to_generation(self):
        self._add_reusable_keil_config(missing_source=True)
        wt = self.base / "wt"
        with mock.patch.object(Keeper, "step_clangd", return_value=0) as generate:
            rc, out, err = self.run_keeper(
                "init", "-p", str(self.root), "--worktree", str(wt),
                "--branch", "task/fallback")
        self.assertEqual(rc, 0, out + err)
        generate.assert_called_once()
        self.assertIn("回退到 clangd-config", out)

    def test_nested_target_is_not_recursively_copied(self):
        wt = self.root / "linked" / "wt"
        rc, out, err = self.run_keeper(
            "init", "-p", str(self.root), "--worktree", str(wt),
            "--branch", "task/nested")
        self.assertEqual(rc, 0, out + err)
        self.assertTrue((wt / "main.c").is_file())
        self.assertFalse((wt / "linked" / "wt").exists())

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

    def test_worktree_dry_run_reports_copy_without_creating_target(self):
        wt = self.base / "wt"
        rc, out, err = self.run_keeper(
            "init", "-p", str(self.root), "--worktree", str(wt),
            "--branch", "task/dry", "--dry-run")
        self.assertEqual(rc, 0, out + err)
        self.assertIn("会从源工作区复制", out)
        self.assertFalse(wt.exists())

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


class TestJsonChannel(KeeperTest):
    """Keeper 的 --json 信封通道:成功走 stdout 单信封,失败走 stderr E_VALIDATION。"""

    def test_json_init_success_envelope(self):
        _git(["checkout", "-q", "-b", "task/x"], self.root)
        rc, out, err = self.run_keeper("init", "-p", str(self.root), "--json")
        self.assertEqual(rc, 0)
        self.assertEqual(err, "")
        obj = json.loads(out)
        self.assertTrue(obj["ok"])
        self.assertIsNone(obj["error"])
        self.assertIn("log", obj["meta"])
        self.assertEqual(
            {"root", "done", "todo", "pending"}, set(obj["data"].keys()))

    def test_json_init_non_repo_validation(self):
        outside = self.base / "nope"
        outside.mkdir()
        rc, out, err = self.run_keeper("init", "-p", str(outside), "--json")
        self.assertEqual(rc, 2)
        self.assertEqual(out, "")
        obj = json.loads(err)
        self.assertFalse(obj["ok"])
        self.assertEqual(obj["error"]["code"], "E_VALIDATION")


if __name__ == "__main__":
    unittest.main()
