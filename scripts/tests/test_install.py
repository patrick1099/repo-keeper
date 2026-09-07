"""install-hooks / sync-exempt / commit-msg 与 _passthru 接力的单测（第二阶段第 3 批）。

全部打到临时目录，任何用例都不碰真实 ~/.githooks 与 ~/.repo-keeper。
core.hooksPath 通过 GIT_CONFIG_GLOBAL 注入临时 gitconfig 控制，不让用例依赖
本机真实配置。sync-exempt 的 gh 一律用临时目录里的假脚本冒充，绝不联网。
commit-msg 的三段署名检查用运行时拼接的假名字触发，完整敏感形状不写进源码。

install-hooks 三模式（--check / --stage / --activate）共用同一个 planner 和
writer，本批只跑 --check 与 --stage；--activate 代码在但本批不许执行。
"""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import GateCheck  # noqa: E402
import cli_common as cc  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
HOOKS_SRC = REPO / "hooks"
SCRIPTS_SRC = REPO / "scripts"
COMMIT_MSG = HOOKS_SRC / "commit-msg"
PASSTHRU = HOOKS_SRC / "_passthru"

PY_FILES = ["GateCheck.py", "cli_common.py", "secretscan.py", "toolname.py"]
HOOK_FILES = ["pre-push", "commit-msg", "_passthru"]
#: 4 个 .py + 3 个 hooks/ 分发文件 + 铺满的 _passthru 槽位副本
_EXPECTED_ITEMS = (len(PY_FILES) + len(HOOK_FILES)
                   + len(GateCheck.PASSTHRU_SLOTS))
SAFE_EMAIL = "bob@" + "example.com"
AI_NAME = "cl" + "aude"


def _git(repo, *args):
    proc = subprocess.run(["git", "-C", str(repo), *args],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise AssertionError("git {0} failed: {1}".format(args, proc.stderr.strip()))
    return proc.stdout


def _common_dir(repo):
    """仓库本地钩子目录（绝对路径）。--path-format=absolute 是必须的。"""
    return Path(_git(repo, "rev-parse", "--path-format=absolute",
                     "--git-common-dir").strip())


def _make_targets(root):
    hooks = root / "hooks"
    home = root / "home"
    hooks.mkdir()
    home.mkdir()
    return hooks, home


@contextlib.contextmanager
def _git_cfg(hooks_path):
    """注入临时 gitconfig 控制 core.hooksPath；hooks_path=None 表示不设。"""
    old = {k: os.environ.get(k)
           for k in ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM")}
    os.environ["GIT_CONFIG_NOSYSTEM"] = "1"
    if hooks_path is None:
        os.environ.pop("GIT_CONFIG_GLOBAL", None)
    else:
        cfg = Path(tempfile.mkdtemp()) / "gitconfig"
        cfg.write_text("[core]\n\thooksPath = {0}\n".format(
            str(hooks_path).replace("\\", "/")), encoding="utf-8")
        os.environ["GIT_CONFIG_GLOBAL"] = str(cfg)
    try:
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _run(*argv, hooks_dir=None, home=None, gitcfg_hooks=None,
         gh_command=None, dry_run=False):
    """in-process 跑 GateCheck.main，env 通过 _git_cfg 控制。"""
    argv = list(argv)
    if hooks_dir is not None:
        argv += ["--hooks-dir", str(hooks_dir)]
    if home is not None:
        argv += ["--home", str(home)]
    if gh_command is not None:
        argv += ["--gh-command", gh_command]
    if dry_run:
        argv += ["--dry-run"]
    out, err = io.StringIO(), io.StringIO()
    with _git_cfg(gitcfg_hooks):
        code = GateCheck.main(argv, cc.Sinks(out=out, err=err), reconfigure=False)
    return code, out.getvalue(), err.getvalue()


def _head8_nonwhitelisted(path):
    """非白名单进程读头 8 字节（Esafenet 密文态验证，本机 PowerShell）。"""
    script = ("$b=[System.IO.File]::ReadAllBytes('{0}'); "
              "($b[0..7] | ForEach-Object {{ $_.ToString('x2') }}) -join ''").format(
                  str(path).replace("'", "''"))
    proc = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        raise AssertionError("powershell failed: " +
                             proc.stderr.decode("utf-8", "replace"))
    return proc.stdout.decode("utf-8", "replace").strip()


def _write_fake_gh(root, repos, exitcode=0, dump_host=False):
    body = "import json, sys\n"
    if dump_host:
        body += ("import os\n"
                 "open({0!r}, 'w').write(os.environ.get('GH_HOST', '<unset>'))\n"
                 ).format(str(root / "gh-host.txt"))
    body += "print(json.dumps({0!r}))\n".format(repos)
    body += "sys.exit({0})\n".format(exitcode)
    p = root / "fake_gh.py"
    p.write_text(body, encoding="utf-8")
    return p


def _make_repo(root, name="bob"):
    repo = root / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", name)
    _git(repo, "config", "user.email", SAFE_EMAIL)
    return repo


def _run_commit_msg(repo, msg):
    msgfile = repo / "msg.txt"
    msgfile.write_text(msg, encoding="utf-8")
    proc = subprocess.run(["sh", str(COMMIT_MSG), str(msgfile)], cwd=str(repo),
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          timeout=60)
    return proc.returncode, proc.stdout, proc.stderr


class TestInstallHooksCheck(unittest.TestCase):
    def test_check_fresh_dir_reports_missing_and_creates_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hooks, home = _make_targets(root)
            code, out, err = _run("install-hooks", "--check",
                                  hooks_dir=hooks, home=home,
                                  gitcfg_hooks=str(root / "elsewhere"))
            self.assertEqual(code, 0, err)
            for name in PY_FILES + HOOK_FILES:
                self.assertIn("missing", err, name)
            self.assertEqual(list(hooks.iterdir()), [])
            self.assertEqual(list(home.iterdir()), [])

    def test_check_installed_reports_ok_and_mtime_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hooks, home = _make_targets(root)
            code, _, err = _run("install-hooks", "--stage", str(root),
                                gitcfg_hooks=str(hooks))
            self.assertEqual(code, 0, err)
            before = {p: p.stat().st_mtime_ns for p in list(hooks.iterdir())
                      + list(home.iterdir())}
            code, out, err = _run("install-hooks", "--check",
                                  hooks_dir=hooks, home=home,
                                  gitcfg_hooks=str(hooks))
            self.assertEqual(code, 0, err)
            for name in PY_FILES + HOOK_FILES:
                self.assertIn("ok", err, name)
            after = {p: p.stat().st_mtime_ns for p in list(hooks.iterdir())
                     + list(home.iterdir())}
            self.assertEqual(before, after)

    def test_check_and_stage_plans_identical(self):
        # 同一个目录先 --check 拿到计划, 再 --stage, 断言实际做的事与计划一致
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hooks, home = _make_targets(root)
            code, out, err = _run("install-hooks", "--check",
                                  hooks_dir=hooks, home=home,
                                  gitcfg_hooks=str(hooks))
            self.assertEqual(code, 0, err)
            for name in PY_FILES + HOOK_FILES:
                self.assertIn("missing", err, name)
            code, _, err2 = _run("install-hooks", "--stage", str(root),
                                 gitcfg_hooks=str(hooks))
            self.assertEqual(code, 0, err2)
            for name in PY_FILES + HOOK_FILES:
                target = home / name if name in PY_FILES else hooks / name
                src = SCRIPTS_SRC / name if name in PY_FILES else HOOKS_SRC / name
                self.assertTrue(target.exists(), name)
                self.assertEqual(target.read_bytes(), src.read_bytes(), name)

    def test_installed_but_inactive_when_hookspath_elsewhere(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hooks, home = _make_targets(root)
            code, _, err = _run("install-hooks", "--stage", str(root),
                                gitcfg_hooks=str(root / "elsewhere"))
            self.assertEqual(code, 0, err)
            code, out, err = _run("install-hooks", "--check",
                                  hooks_dir=hooks, home=home,
                                  gitcfg_hooks=str(root / "elsewhere"))
            self.assertEqual(code, 0, err)
            self.assertIn("installed-but-inactive", err)
            self.assertIn("core.hooksPath", err)
            self.assertIn("ok", err)  # python 闭包不参与"在岗"判定


class TestInstallHooksStage(unittest.TestCase):
    def test_stage_installs_closure_and_hooks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hooks, home = _make_targets(root)
            code, out, err = _run("install-hooks", "--stage", str(root),
                                  gitcfg_hooks=str(hooks))
            self.assertEqual(code, 0, err)
            for name in PY_FILES:
                self.assertEqual((home / name).read_bytes(),
                                 (SCRIPTS_SRC / name).read_bytes(), name)
            for name in HOOK_FILES:
                data = (hooks / name).read_bytes()
                self.assertEqual(data, (HOOKS_SRC / name).read_bytes(), name)
                self.assertNotIn(b"\r", data, name + " 含 CR")
            manifest = json.loads((home / "installed-manifest.json")
                                  .read_text(encoding="utf-8"))
            self.assertEqual(len(manifest["files"]), _EXPECTED_ITEMS)
            for entry in manifest["files"]:
                target = Path(entry["target"])
                self.assertTrue(target.exists(), entry["target"])
                self.assertEqual(entry["sha256"], GateCheck._sha256_bytes(
                    target.read_bytes()))

    def test_target_differs_creates_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hooks, home = _make_targets(root)
            old = b"#!/bin/sh\necho old\n"
            (hooks / "pre-push").write_bytes(old)
            code, _, err = _run("install-hooks", "--stage", str(root),
                                gitcfg_hooks=str(hooks))
            self.assertEqual(code, 0, err)
            baks = list(hooks.glob("pre-push.bak-*"))
            self.assertEqual(len(baks), 1)
            self.assertEqual(baks[0].read_bytes(), old)
            self.assertEqual((hooks / "pre-push").read_bytes(),
                             (HOOKS_SRC / "pre-push").read_bytes())

    def test_mode_wrong_reported_when_exec_bit_missing(self):
        # Windows/NTFS 没有 exec 语义: 先用默认(不查 exec 位)stage 装好,
        # 再翻转 _HAS_EXEC_SEMANTICS 强制启用该维度, 断言报 mode-wrong。
        old = GateCheck._HAS_EXEC_SEMANTICS
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                hooks, home = _make_targets(root)
                code, _, err = _run("install-hooks", "--stage", str(root),
                                    gitcfg_hooks=str(hooks))
                self.assertEqual(code, 0, err)
                GateCheck._HAS_EXEC_SEMANTICS = True
                code, out, err = _run("install-hooks", "--check",
                                      hooks_dir=hooks, home=home,
                                      gitcfg_hooks=str(hooks))
                self.assertEqual(code, 0, err)
                self.assertIn("mode-wrong", err)
                for name in HOOK_FILES:
                    self.assertIn(name, err)
        finally:
            GateCheck._HAS_EXEC_SEMANTICS = old

    def test_crlf_reported_when_target_has_cr(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hooks, home = _make_targets(root)
            src = (HOOKS_SRC / "pre-push").read_bytes()
            (hooks / "pre-push").write_bytes(src.replace(b"\n", b"\r\n"))
            code, out, err = _run("install-hooks", "--check",
                                  hooks_dir=hooks, home=home,
                                  gitcfg_hooks=str(hooks))
            self.assertEqual(code, 0, err)
            self.assertIn("crlf", err)

    def test_compare_and_swap_aborts_on_modified_target(self):
        # plan 之后、write 之前目标被动过 -> 整体中止, 目标保持被改后的样子
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hooks, home = _make_targets(root)
            (home / "GateCheck.py").write_bytes(b"old content\n")
            orig_write = GateCheck._BatchWriter.write
            calls = {"n": 0}

            def tampering_write(writer, target, data, prev_sha, *, exec_mode=False):
                calls["n"] += 1
                if calls["n"] == 1:
                    target.write_bytes(b"tampered by test\n")
                return orig_write(writer, target, data, prev_sha,
                                  exec_mode=exec_mode)

            with mock.patch.object(GateCheck._BatchWriter, "write",
                                   tampering_write):
                code, out, err = _run("install-hooks", "--stage", str(root),
                                      gitcfg_hooks=str(hooks))
            self.assertNotEqual(code, 0)
            self.assertEqual((home / "GateCheck.py").read_bytes(),
                             b"tampered by test\n")

    def test_verify_failure_rolls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hooks, home = _make_targets(root)
            old = b"#!/bin/sh\necho old\n"
            (hooks / "pre-push").write_bytes(old)
            real_replace = os.replace

            def corrupting_replace(src, dst):
                real_replace(src, dst)
                with open(dst, "wb") as fh:
                    fh.write(b"corrupted")

            with mock.patch.object(GateCheck.os, "replace", corrupting_replace):
                code, out, err = _run("install-hooks", "--stage", str(root),
                                      gitcfg_hooks=str(hooks))
            self.assertNotEqual(code, 0)
            self.assertEqual((hooks / "pre-push").read_bytes(), old)

    def test_second_file_failure_rolls_back_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hooks, home = _make_targets(root)
            orig_write = GateCheck._BatchWriter.write
            calls = {"n": 0}

            def failing_write(writer, target, data, prev_sha, *, exec_mode=False):
                calls["n"] += 1
                if calls["n"] == 2:
                    raise GateCheck._BatchInstallError("second file failed")
                return orig_write(writer, target, data, prev_sha,
                                  exec_mode=exec_mode)

            with mock.patch.object(GateCheck._BatchWriter, "write",
                                   failing_write):
                code, out, err = _run("install-hooks", "--stage", str(root),
                                      gitcfg_hooks=str(hooks))
            self.assertNotEqual(code, 0)
            self.assertEqual(list(home.iterdir()), [])

    def test_temp_file_keeps_py_suffix(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hooks, home = _make_targets(root)
            temps = []
            real_replace = os.replace

            def record_replace(src, dst):
                temps.append(str(src))
                real_replace(src, dst)

            with mock.patch.object(GateCheck.os, "replace", record_replace):
                code, out, err = _run("install-hooks", "--stage", str(root),
                                      gitcfg_hooks=str(hooks))
            self.assertEqual(code, 0, err)
            self.assertTrue(any(".GateCheck.tmp-" in t and t.endswith(".py")
                                for t in temps), temps)

    @unittest.skipUnless(os.name == "nt",
                         "Esafenet 密文态验证只在本机 Windows 上有意义")
    def test_staged_py_keeps_esafenet_encryption(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hooks, home = _make_targets(root)
            code, _, err = _run("install-hooks", "--stage", str(root),
                                gitcfg_hooks=str(hooks))
            self.assertEqual(code, 0, err)
            for name in PY_FILES:
                src_head = _head8_nonwhitelisted(SCRIPTS_SRC / name)
                dst_head = _head8_nonwhitelisted(home / name)
                self.assertEqual(src_head, dst_head, name)
                self.assertTrue(src_head.startswith("e0a891e7d8f205ac"), name)

    def test_no_mode_exits_nonzero(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            code, out, err = _run("install-hooks")
            self.assertNotEqual(code, 0)


class TestSyncExempt(unittest.TestCase):
    def test_private_only_written_others_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            home.mkdir()
            repos = [
                {"nameWithOwner": "bob/private-one", "visibility": "PRIVATE",
                 "isArchived": False},
                {"nameWithOwner": "bob/private-two", "visibility": "PRIVATE",
                 "isArchived": False},
                {"nameWithOwner": "bob/public-one", "visibility": "PUBLIC",
                 "isArchived": False},
                {"nameWithOwner": "bob/no-visibility", "isArchived": False},
            ]
            fake = _write_fake_gh(root, repos)
            code, out, err = _run("sync-exempt", home=home,
                                  gh_command="py -3 " + str(fake))
            self.assertEqual(code, 0, err)
            lines = (home / "gate-exempt.txt").read_text(
                encoding="utf-8").splitlines()
            keys = [ln.split()[0] for ln in lines]
            self.assertEqual(keys, ["github.com/bob/private-one",
                                    "github.com/bob/private-two"])
            self.assertIn("bob/public-one", err)
            self.assertIn("bob/no-visibility", err)

    def test_gh_failure_leaves_file_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            home.mkdir()
            target = home / "gate-exempt.txt"
            original = b"github.com/bob/existing 2026-01-01T00:00:00\n"
            target.write_bytes(original)
            fake = _write_fake_gh(root, [], exitcode=3)
            code, out, err = _run("sync-exempt", home=home,
                                  gh_command="py -3 " + str(fake))
            self.assertNotEqual(code, 0)
            self.assertEqual(target.read_bytes(), original)

    def test_non_json_output_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            home.mkdir()
            target = home / "gate-exempt.txt"
            original = b"github.com/bob/existing 2026-01-01T00:00:00\n"
            target.write_bytes(original)
            fake = root / "fake_gh.py"
            fake.write_text("print('not json at all')\n", encoding="utf-8")
            code, out, err = _run("sync-exempt", home=home,
                                  gh_command="py -3 " + str(fake))
            self.assertNotEqual(code, 0)
            self.assertEqual(target.read_bytes(), original)

    def test_limit_reached_means_truncation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            home.mkdir()
            target = home / "gate-exempt.txt"
            target.write_bytes(b"github.com/bob/existing 2026-01-01T00:00:00\n")
            repos = [{} for _ in range(GateCheck.SYNC_EXEMPT_LIMIT)]
            fake = _write_fake_gh(root, repos)
            code, out, err = _run("sync-exempt", home=home,
                                  gh_command="py -3 " + str(fake))
            self.assertNotEqual(code, 0)
            self.assertEqual(target.read_bytes(),
                             b"github.com/bob/existing 2026-01-01T00:00:00\n")

    def test_gh_host_pinned_to_github_com(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            home.mkdir()
            repos = [{"nameWithOwner": "bob/private-one", "visibility": "PRIVATE",
                      "isArchived": False}]
            fake = _write_fake_gh(root, repos, dump_host=True)
            code, out, err = _run("sync-exempt", home=home,
                                  gh_command="py -3 " + str(fake))
            self.assertEqual(code, 0, err)
            host = (root / "gh-host.txt").read_text(encoding="utf-8").strip()
            self.assertEqual(host, "github.com")

    def test_written_keys_found_by_arm(self):
        # 端到端: sync-exempt 写完后, 同一个 --home 下 arm 能直接查到(防两套规范化漂移)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            home.mkdir()
            repos = [{"nameWithOwner": "bob/private-one", "visibility": "PRIVATE",
                      "isArchived": False}]
            fake = _write_fake_gh(root, repos)
            code, _, err = _run("sync-exempt", home=home,
                                gh_command="py -3 " + str(fake))
            self.assertEqual(code, 0, err)
            url = "https://github.com/" + "bob" + "/" + "private-one" + ".git"
            code, out, err2 = _run("arm", "--url", url, "--home", str(home),
                                   "--json")
            self.assertEqual(code, 0, err2)
            obj = json.loads(out)
            self.assertTrue(obj["ok"], out)
            self.assertFalse(obj["data"]["armed"])
            self.assertEqual(obj["data"]["exempt_state"], "fresh")

    def test_dry_run_does_not_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            home.mkdir()
            repos = [{"nameWithOwner": "bob/private-one", "visibility": "PRIVATE",
                      "isArchived": False}]
            fake = _write_fake_gh(root, repos)
            code, out, err = _run("sync-exempt", home=home,
                                  gh_command="py -3 " + str(fake), dry_run=True)
            self.assertEqual(code, 0, err)
            self.assertFalse((home / "gate-exempt.txt").exists())
            self.assertIn("github.com/bob/private-one", err)


class TestCommitMsgRelay(unittest.TestCase):
    def test_ai_signature_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(Path(tmp))
            msg = ("Add feature\n\nCo-Authored-By: " + AI_NAME + " <" +
                   "bob@example.com" + ">\n")
            rc, out, err = _run_commit_msg(repo, msg)
            self.assertEqual(rc, 1)
            text = err.decode("utf-8", "replace")
            self.assertIn("提交被拒绝", text)
            self.assertIn("Co-Authored-By", text)

    def test_clean_message_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(Path(tmp))
            rc, out, err = _run_commit_msg(repo, "Add feature\n")
            self.assertEqual(rc, 0)
            self.assertEqual(err, b"")

    def test_relay_sentinel_args_and_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _make_repo(root)
            common = _common_dir(repo)
            hooks = common / "hooks"
            hooks.mkdir(parents=True, exist_ok=True)
            dump = root / "args.txt"
            (hooks / "commit-msg").write_text(
                '#!/bin/sh\nprintf "%s\\n" "$@" > "{a}"\nexit 7\n'.format(
                    a=str(dump).replace("\\", "/")), encoding="utf-8")
            rc, out, err = _run_commit_msg(repo, "clean\n")
            self.assertEqual(rc, 7)
            got = dump.read_text(encoding="utf-8")
            self.assertIn("msg.txt", got)

    def test_self_relay_guard(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _make_repo(root)
            common = _common_dir(repo)
            hooks = common / "hooks"
            hooks.mkdir(parents=True, exist_ok=True)
            (hooks / "commit-msg").write_bytes(COMMIT_MSG.read_bytes())
            rc, out, err = _run_commit_msg(repo, "clean\n")
            self.assertEqual(rc, 0)
            self.assertEqual(err, b"")

    def test_fail_closed_outside_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plain = root / "not-a-repo"
            plain.mkdir()
            msgfile = plain / "msg.txt"
            msgfile.write_text("clean\n", encoding="utf-8")
            proc = subprocess.run(["sh", str(COMMIT_MSG), str(msgfile)],
                                  cwd=str(plain), stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, timeout=60)
            self.assertEqual(proc.returncode, 1)


class TestPassthruRelay(unittest.TestCase):
    def _slot(self, root, name="post-commit"):
        d = root / "hooks-dir"
        d.mkdir(exist_ok=True)
        dst = d / name
        dst.write_bytes(PASSTHRU.read_bytes())
        return dst

    def test_relay_sentinel_args_and_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _make_repo(root)
            common = _common_dir(repo)
            hooks = common / "hooks"
            hooks.mkdir(parents=True, exist_ok=True)
            dump = root / "args.txt"
            (hooks / "post-commit").write_text(
                '#!/bin/sh\nprintf "%s\\n" "$@" > "{a}"\nexit 9\n'.format(
                    a=str(dump).replace("\\", "/")), encoding="utf-8")
            dst = self._slot(root)
            proc = subprocess.run(["sh", str(dst), "alpha", "beta"],
                                  cwd=str(repo), stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, timeout=60)
            self.assertEqual(proc.returncode, 9)
            got = dump.read_text(encoding="utf-8")
            self.assertIn("alpha", got)
            self.assertIn("beta", got)

    def test_self_relay_guard(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _make_repo(root)
            common = _common_dir(repo)
            hooks = common / "hooks"
            hooks.mkdir(parents=True, exist_ok=True)
            (hooks / "post-commit").write_bytes(PASSTHRU.read_bytes())
            dst = self._slot(root)
            proc = subprocess.run(["sh", str(dst), "x"], cwd=str(repo),
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  timeout=60)
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(proc.stderr, b"")

    def test_fail_closed_outside_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dst = self._slot(root)
            proc = subprocess.run(["sh", str(dst), "x"], cwd=str(root),
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  timeout=60)
            self.assertEqual(proc.returncode, 1)


def _bare_source_repo(root):
    """造一个只含分发闭包 7 个文件的 git 仓，用来单测 _source_closure_state。

    本地 core.hooksPath 指向一个空目录：不让用例依赖本机全局钩子，
    也不让它去跑真实的署名闸。
    """
    src = root / "src-repo"
    (src / "scripts").mkdir(parents=True)
    (src / "hooks").mkdir(parents=True)
    nohooks = root / "nohooks"
    nohooks.mkdir()
    for name in PY_FILES:
        (src / "scripts" / name).write_text("x = 1\n", encoding="utf-8")
    for name in HOOK_FILES:
        (src / "hooks" / name).write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    _git(src, "init", "-q")
    _git(src, "config", "core.hooksPath", str(nohooks))
    _git(src, "add", "-A")
    _git(src, "-c", "user.email=t@example.com", "-c", "user.name=t",
         "commit", "-q", "-m", "init")
    return src


class TestStageCreatesOnlyItsOwnSubdirs(unittest.TestCase):
    """甲：只有 --stage 建目录，且只建两个固定子目录、DIR 本身仍要求先存在。"""

    def test_stage_creates_the_two_subdirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)   # 全新目录，不预先 mkdir hooks/ 与 home/
            code, out, err = _run("install-hooks", "--stage", str(root),
                                  gitcfg_hooks=str(root / "hooks"))
            self.assertEqual(code, 0, err)
            self.assertTrue((root / "hooks").is_dir())
            self.assertTrue((root / "home").is_dir())

    def test_stage_refuses_when_root_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "nope"
            code, out, err = _run("install-hooks", "--stage", str(root))
            self.assertNotEqual(code, 0)
            self.assertFalse(root.exists())

    def test_check_creates_no_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hooks, home = root / "h", root / "m"
            code, out, err = _run("install-hooks", "--check",
                                  hooks_dir=hooks, home=home,
                                  gitcfg_hooks=str(hooks))
            self.assertNotEqual(code, 0)
            self.assertFalse(hooks.exists())
            self.assertFalse(home.exists())

    def test_activate_creates_no_directories(self):
        # --activate 打错路径时若自建目录，会造出一套"字节全对但 git 根本不看"
        # 的目录还报成功——正是要防的那种静默成功。
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hooks, home = root / "h", root / "m"
            code, out, err = _run("install-hooks", "--activate",
                                  hooks_dir=hooks, home=home,
                                  gitcfg_hooks=str(hooks))
            self.assertNotEqual(code, 0)
            self.assertFalse(hooks.exists())
            self.assertFalse(home.exists())


class TestPassthruSlots(unittest.TestCase):
    """丙：core.hooksPath 是替换不是叠加，缺一个槽位就静默屏蔽一个本地钩子。"""

    def test_all_slots_installed_byte_identical(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hooks, home = _make_targets(root)
            code, out, err = _run("install-hooks", "--stage", str(root),
                                  gitcfg_hooks=str(hooks))
            self.assertEqual(code, 0, err)
            # 先钉死列表本身：空列表会让下面的循环空转通过
            self.assertEqual(
                len(GateCheck.PASSTHRU_SLOTS),
                len(GateCheck.GIT_HOOK_NAMES) - 2
                - len(GateCheck.PASSTHRU_SKIP_SLOTS))
            self.assertGreater(len(GateCheck.PASSTHRU_SLOTS), 15)
            installed = {p.name for p in hooks.iterdir() if p.is_file()}
            self.assertTrue(set(GateCheck.PASSTHRU_SLOTS) <= installed,
                            sorted(set(GateCheck.PASSTHRU_SLOTS) - installed))
            want = PASSTHRU.read_bytes()
            for name in GateCheck.PASSTHRU_SLOTS:
                self.assertTrue((hooks / name).is_file(), name)
                self.assertEqual((hooks / name).read_bytes(), want, name)

    def test_skip_slots_left_empty(self):
        # 这三处的"退 0"不是放行而是"我已代劳/我已答复"，铺个只会退 0 的壳
        # 反倒制造静默错误；它们缺席是 fail-loud 的，留空才是安全态。
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hooks, home = _make_targets(root)
            code, out, err = _run("install-hooks", "--stage", str(root),
                                  gitcfg_hooks=str(hooks))
            self.assertEqual(code, 0, err)
            for name in GateCheck.PASSTHRU_SKIP_SLOTS:
                self.assertFalse((hooks / name).exists(), name)

    def test_dedicated_gates_are_not_passthru_copies(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hooks, home = _make_targets(root)
            code, out, err = _run("install-hooks", "--stage", str(root),
                                  gitcfg_hooks=str(hooks))
            self.assertEqual(code, 0, err)
            passthru = PASSTHRU.read_bytes()
            for name in ("pre-push", "commit-msg"):
                self.assertNotEqual((hooks / name).read_bytes(), passthru, name)
            self.assertNotIn("pre-push", GateCheck.PASSTHRU_SLOTS)
            self.assertNotIn("commit-msg", GateCheck.PASSTHRU_SLOTS)

    def test_slot_copy_relays_under_its_own_slot_name(self):
        # 装进去的槽位副本必须真的能接力——修好的 _passthru 模板自己不叫任何
        # git 钩子名，git 永远不会调它，只有铺成槽位名之后才起作用。
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stage = root / "stage"
            stage.mkdir()
            hooks, home = _make_targets(stage)
            code, out, err = _run("install-hooks", "--stage", str(stage),
                                  gitcfg_hooks=str(hooks))
            self.assertEqual(code, 0, err)
            repo = _make_repo(root)
            local = _common_dir(repo) / "hooks"
            local.mkdir(parents=True, exist_ok=True)
            mark = root / "ran.txt"
            (local / "pre-commit").write_text(
                '#!/bin/sh\necho ran > "{0}"\nexit 7\n'.format(
                    str(mark).replace("\\", "/")), encoding="utf-8")
            proc = subprocess.run(["sh", str(hooks / "pre-commit")],
                                  cwd=str(repo), stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, timeout=60)
            self.assertEqual(proc.returncode, 7, proc.stderr)
            self.assertTrue(mark.exists())


class TestInstallIdempotency(unittest.TestCase):
    """幂等：manifest 的 prev_sha 硬编码 None 会让第二次安装必定失败。"""

    def test_stage_twice_both_succeed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hooks, home = _make_targets(root)
            code, _, err = _run("install-hooks", "--stage", str(root),
                                gitcfg_hooks=str(hooks))
            self.assertEqual(code, 0, err)
            first = json.loads((home / "installed-manifest.json")
                               .read_text(encoding="utf-8"))
            code, _, err = _run("install-hooks", "--stage", str(root),
                                gitcfg_hooks=str(hooks))
            self.assertEqual(code, 0, err)
            second = json.loads((home / "installed-manifest.json")
                                .read_text(encoding="utf-8"))
            self.assertEqual(len(second["files"]), len(first["files"]))

    def test_stage_three_times_still_succeeds(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hooks, home = _make_targets(root)
            for i in range(3):
                code, _, err = _run("install-hooks", "--stage", str(root),
                                    gitcfg_hooks=str(hooks))
                self.assertEqual(code, 0, "第 {0} 次: {1}".format(i + 1, err))

    def test_tampered_manifest_aborts_batch(self):
        # manifest 走 compare-and-swap：plan 之后被别的进程动过要整体中止，
        # 而不是闷头覆盖。
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hooks, home = _make_targets(root)
            code, _, err = _run("install-hooks", "--stage", str(root),
                                gitcfg_hooks=str(hooks))
            self.assertEqual(code, 0, err)
            orig_write = GateCheck._BatchWriter.write

            def tampering_write(writer, target, data, prev_sha, *,
                                exec_mode=False):
                if target.name == GateCheck.MANIFEST_NAME:
                    target.write_bytes(b'{"tampered": true}\n')
                return orig_write(writer, target, data, prev_sha,
                                  exec_mode=exec_mode)

            with mock.patch.object(GateCheck._BatchWriter, "write",
                                   tampering_write):
                code, _, err = _run("install-hooks", "--stage", str(root),
                                    gitcfg_hooks=str(hooks))
            self.assertNotEqual(code, 0)

    def test_check_reads_manifest_back(self):
        # 清单不能只写不读——§D5 要它回答"装的是哪一版"。
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hooks, home = _make_targets(root)
            code, _, err = _run("install-hooks", "--stage", str(root),
                                gitcfg_hooks=str(hooks))
            self.assertEqual(code, 0, err)
            code, out, err = _run("install-hooks", "--check", "--format", "json",
                                  hooks_dir=hooks, home=home,
                                  gitcfg_hooks=str(hooks))
            self.assertEqual(code, 0, err)
            data = json.loads(out)["data"]
            self.assertIsNotNone(data["installed_manifest"])
            self.assertEqual(data["installed_manifest"]["files"],
                             _EXPECTED_ITEMS)
            self.assertFalse(data["installed_manifest"]["parse_error"])

    def test_check_survives_corrupt_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hooks, home = _make_targets(root)
            (home / GateCheck.MANIFEST_NAME).write_bytes(b"not json{{{")
            code, out, err = _run("install-hooks", "--check", "--format", "json",
                                  hooks_dir=hooks, home=home,
                                  gitcfg_hooks=str(hooks))
            self.assertEqual(code, 0, err)
            data = json.loads(out)["data"]
            self.assertTrue(data["installed_manifest"]["parse_error"])


class TestOsErrorRollsBack(unittest.TestCase):
    """只接 _BatchInstallError 的话，OSError 会越过回滚留下装了一半的闭包。"""

    def test_oserror_mid_batch_rolls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hooks, home = _make_targets(root)
            real_replace = os.replace
            calls = {"n": 0}

            def failing_replace(src, dst):
                calls["n"] += 1
                if calls["n"] == 2:
                    raise OSError(28, "No space left on device")
                return real_replace(src, dst)

            with mock.patch.object(GateCheck.os, "replace", failing_replace):
                code, out, err = _run("install-hooks", "--stage", str(root),
                                      gitcfg_hooks=str(hooks))
            self.assertNotEqual(code, 0)
            self.assertEqual(list(home.iterdir()), [])
            self.assertEqual(list(hooks.iterdir()), [])

    def test_oserror_on_backup_rolls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hooks, home = _make_targets(root)
            (hooks / "pre-push").write_bytes(b"#!/bin/sh\necho old\n")

            def failing_copy2(src, dst):
                raise OSError(13, "Permission denied")

            with mock.patch.object(GateCheck.shutil, "copy2", failing_copy2):
                code, out, err = _run("install-hooks", "--stage", str(root),
                                      gitcfg_hooks=str(hooks))
            self.assertNotEqual(code, 0)
            self.assertEqual((hooks / "pre-push").read_bytes(),
                             b"#!/bin/sh\necho old\n")
            self.assertEqual(list(home.iterdir()), [])


class TestSourceClosureState(unittest.TestCase):
    """乙：source_commit 要答得了"装的是哪一版"，前提是分发闭包干净。"""

    def test_clean_repo_reports_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = _bare_source_repo(Path(tmp))
            dirty, reason = GateCheck._source_closure_state(src)
            self.assertIsNone(reason)
            self.assertEqual(dirty, [])

    def test_modified_closure_file_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = _bare_source_repo(Path(tmp))
            (src / "scripts" / "GateCheck.py").write_text("x = 2\n",
                                                          encoding="utf-8")
            dirty, reason = GateCheck._source_closure_state(src)
            self.assertIsNone(reason)
            self.assertEqual(dirty, ["scripts/GateCheck.py"])

    def test_untracked_closure_file_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = _bare_source_repo(Path(tmp))
            _git(src, "rm", "-q", "--cached", "hooks/pre-push")
            dirty, reason = GateCheck._source_closure_state(src)
            self.assertIn("hooks/pre-push", dirty)

    def test_dirt_outside_the_closure_is_ignored(self):
        # 只看闭包不看整仓：堵死整仓会把日常迭代堵死。
        with tempfile.TemporaryDirectory() as tmp:
            src = _bare_source_repo(Path(tmp))
            (src / "README.md").write_text("noise\n", encoding="utf-8")
            (src / "scripts" / "other.py").write_text("y = 1\n",
                                                      encoding="utf-8")
            dirty, reason = GateCheck._source_closure_state(src)
            self.assertIsNone(reason)
            self.assertEqual(dirty, [])

    def test_non_repo_is_undecidable_and_treated_as_dirty(self):
        with tempfile.TemporaryDirectory() as tmp:
            dirty, reason = GateCheck._source_closure_state(Path(tmp))
            self.assertIsNotNone(reason)

    def test_manifest_carries_closure_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hooks, home = _make_targets(root)
            with mock.patch.object(GateCheck, "_source_closure_state",
                                   lambda r: (["hooks/pre-push"], None)):
                code, _, err = _run("install-hooks", "--stage", str(root),
                                    gitcfg_hooks=str(hooks))
            self.assertEqual(code, 0, err)
            manifest = json.loads((home / GateCheck.MANIFEST_NAME)
                                  .read_text(encoding="utf-8"))
            self.assertTrue(manifest["source_dirty"])
            self.assertEqual(manifest["source_dirty_files"], ["hooks/pre-push"])

    def test_clean_closure_marked_not_dirty(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hooks, home = _make_targets(root)
            with mock.patch.object(GateCheck, "_source_closure_state",
                                   lambda r: ([], None)):
                code, _, err = _run("install-hooks", "--stage", str(root),
                                    gitcfg_hooks=str(hooks))
            self.assertEqual(code, 0, err)
            manifest = json.loads((home / GateCheck.MANIFEST_NAME)
                                  .read_text(encoding="utf-8"))
            self.assertFalse(manifest["source_dirty"])


class TestActivatePreconditions(unittest.TestCase):
    """--activate 的两个前置条件。用例只走拒绝路径，一次都不真装。

    require_clean 那条写入路径单独用 _install_execute 直接驱动（见最后一个用例），
    这样既盖到了代码，又不必真的执行 --activate 这个子命令。
    """

    def test_refused_when_hookspath_unset(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hooks, home = _make_targets(root)
            code, out, err = _run("install-hooks", "--activate",
                                  hooks_dir=hooks, home=home,
                                  gitcfg_hooks=None)
            self.assertNotEqual(code, 0)
            self.assertIn("core.hooksPath", out + err)
            self.assertEqual(list(home.iterdir()), [])

    def test_refused_when_hookspath_points_elsewhere(self):
        # 装成功但没生效：字节全对、manifest 全对，而 git 根本不看那个目录。
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hooks, home = _make_targets(root)
            code, out, err = _run("install-hooks", "--activate",
                                  hooks_dir=hooks, home=home,
                                  gitcfg_hooks=str(root / "elsewhere"))
            self.assertNotEqual(code, 0)
            self.assertIn("core.hooksPath", out + err)
            self.assertEqual(list(home.iterdir()), [])

    def test_refused_when_closure_dirty(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hooks, home = _make_targets(root)
            with mock.patch.object(GateCheck, "_source_closure_state",
                                   lambda r: (["scripts/GateCheck.py"], None)):
                code, out, err = _run("install-hooks", "--activate",
                                      hooks_dir=hooks, home=home,
                                      gitcfg_hooks=str(hooks))
            self.assertNotEqual(code, 0)
            self.assertIn("闭包", out + err)
            self.assertEqual(list(home.iterdir()), [])

    def test_refused_when_closure_undecidable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hooks, home = _make_targets(root)
            with mock.patch.object(GateCheck, "_source_closure_state",
                                   lambda r: ([], "git status 跑不起来")):
                code, out, err = _run("install-hooks", "--activate",
                                      hooks_dir=hooks, home=home,
                                      gitcfg_hooks=str(hooks))
            self.assertNotEqual(code, 0)
            self.assertEqual(list(home.iterdir()), [])

    def test_stage_allows_dirty_closure(self):
        # --stage 允许脏，只在 manifest 里记 source_dirty——不堵日常迭代。
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hooks, home = _make_targets(root)
            with mock.patch.object(GateCheck, "_source_closure_state",
                                   lambda r: (["scripts/GateCheck.py"], None)):
                code, _, err = _run("install-hooks", "--stage", str(root),
                                    gitcfg_hooks=str(hooks))
            self.assertEqual(code, 0, err)

    def test_require_clean_rechecks_closure_at_write_time(self):
        # plan 读字节与真正落盘之间源仓可能被改动过，所以贴着写入要再验一次。
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hooks, home = _make_targets(root)
            source_root = GateCheck._find_source_root()
            items = GateCheck._install_plan(source_root, hooks, home)
            with mock.patch.object(GateCheck, "_source_closure_state",
                                   lambda r: (["hooks/_passthru"], None)):
                with self.assertRaises(cc.CliError):
                    GateCheck._install_execute(items, hooks, home, source_root,
                                               require_clean=True)
            self.assertEqual(list(home.iterdir()), [])
            self.assertEqual(list(hooks.iterdir()), [])

    def test_require_clean_passes_when_closure_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hooks, home = _make_targets(root)
            source_root = GateCheck._find_source_root()
            items = GateCheck._install_plan(source_root, hooks, home)
            with mock.patch.object(GateCheck, "_source_closure_state",
                                   lambda r: ([], None)):
                manifest = GateCheck._install_execute(
                    items, hooks, home, source_root, require_clean=True)
            self.assertEqual(len(manifest["files"]), _EXPECTED_ITEMS)
            self.assertFalse(manifest["source_dirty"])


if __name__ == "__main__":
    unittest.main()
