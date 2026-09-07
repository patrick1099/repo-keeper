"""GateCheck 扫描核心的单测（第二阶段第 1 批）。

全部用例走一次性临时 git 仓（tempfile.mkdtemp -> git init），绝不拿任何真仓
当测试对象。仓里提交用 -c/环境变量显式指定身份，不继承本机 git 配置。
词表一律用 --words-file 注入自造假词，绝不读真实词表。夹具运行时拼接，
源码里不出现完整敏感形状（本文件自己会被发布闸 test_no_secrets.py 扫）。

假词约定：words-file 用 "carol"，identity_words 用 "alice"（都属方案允许的
示例值，且不在本机真实词表里，rulecorpus 的既有语料已经证明这一点）。
"""

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import GateCheck  # noqa: E402
import cli_common as cc  # noqa: E402

FAKE_WORDS = ["carol"]
FAKE_IDENTITY = "alice"
ZEROS40 = "0" * 40
ZEROS64 = "0" * 64

PROJX_URL = "https://github.com/" + "bob" + "/" + "projx" + ".git"
SAFE_EMAIL = "bob@" + "example.com"
SENSITIVE_EMAIL = "bob@" + "projx.com"
SENSITIVE_EMAIL_CAROL = "carol@" + "projx.com"


def _git(repo, *args, check=True):
    proc = subprocess.run(["git", "-C", str(repo), *args],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          encoding="utf-8", errors="replace")
    if check and proc.returncode != 0:
        raise AssertionError("git {0} failed: {1}".format(args, proc.stderr.strip()))
    return proc.stdout


def _git_env(repo, args, env):
    full = dict(os.environ)
    full.update(env)
    proc = subprocess.run(["git", "-C", str(repo), *args], env=full,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise AssertionError("git {0} failed: {1}".format(args, proc.stderr.strip()))
    return proc.stdout


def _make_repo(root, name="bob", email=None):
    repo = root / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    empty_hooks = repo / ".empty-hooks"
    empty_hooks.mkdir()
    _git(repo, "config", "core.hooksPath", str(empty_hooks))
    _git(repo, "config", "user.name", name)
    _git(repo, "config", "user.email", email or SAFE_EMAIL)
    return repo


def _commit(repo, files, message, env=None):
    for rel, content in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            p.write_bytes(content)
        else:
            p.write_text(content, encoding="utf-8")
    _git(repo, "add", "-A")
    _git_env(repo, ["commit", "-q", "-m", message], env or {})
    return _git(repo, "rev-parse", "HEAD").strip()


def _sha(repo, ref="HEAD"):
    return _git(repo, "rev-parse", ref).strip()


def _words_file(root, words=FAKE_WORDS):
    p = root / "words.txt"
    p.write_text("\n".join(words) + "\n", encoding="utf-8")
    return p


def _run_gatecheck(*argv, stdin_text=""):
    out, err = io.StringIO(), io.StringIO()
    sinks = cc.Sinks(out=out, err=err)
    old = sys.stdin
    sys.stdin = io.StringIO(stdin_text)
    try:
        code = GateCheck.main(list(argv), sinks, reconfigure=False)
    finally:
        sys.stdin = old
    return code, out.getvalue(), err.getvalue()


def _load_json(text):
    return json.loads(text)


def _common_args(repo, words, extra=()):
    return ["--repo", str(repo), "--remote-name", "origin",
            "--remote-url", PROJX_URL, "--words-file", str(words),
            "--identity-words", FAKE_IDENTITY, "--json"] + list(extra)


def _new_ref_stdin(sha):
    return "refs/heads/main {0} refs/heads/main {1}\n".format(sha, ZEROS40)


class TestRangeCalculation(unittest.TestCase):
    def _repo_with_sensitive_history(self, tmp):
        repo = _make_repo(tmp)
        sha1 = _commit(repo, {"a.txt": "hello carol"}, "first")
        sha2 = _commit(repo, {"b.txt": "clean"}, "second")
        return repo, sha1, sha2

    def test_remote_existing_ref_scans_increment_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, sha1, sha2 = self._repo_with_sensitive_history(Path(tmp))
            words = _words_file(Path(tmp))
            code, out, err = _run_gatecheck(
                "pre-push", *_common_args(repo, words),
                stdin_text="refs/heads/main {0} refs/heads/main {1}\n".format(sha2, sha1))
            self.assertEqual(code, 0, err)
            obj = _load_json(out)
            self.assertTrue(obj["ok"])
            self.assertEqual(obj["data"]["hits"], [])

    def test_new_ref_scans_full_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, _sha1, sha2 = self._repo_with_sensitive_history(Path(tmp))
            words = _words_file(Path(tmp))
            code, out, err = _run_gatecheck(
                "pre-push", *_common_args(repo, words),
                stdin_text="refs/heads/main {0} refs/heads/main {1}\n".format(sha2, ZEROS40))
            self.assertEqual(code, 1)
            obj = _load_json(err)
            self.assertEqual(obj["error"]["code"], "E_LEAK_FOUND")
            self.assertTrue(any(h["rule"] == "WORD" for h in obj["error"]["details"]["hits"]))

    def test_delete_ref_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, sha1, _ = self._repo_with_sensitive_history(Path(tmp))
            words = _words_file(Path(tmp))
            code, out, err = _run_gatecheck(
                "pre-push", *_common_args(repo, words),
                stdin_text="refs/heads/main {0} refs/heads/main {1}\n".format(ZEROS40, sha1))
            self.assertEqual(code, 0, err)
            self.assertTrue(_load_json(out)["ok"])

    def test_only_deletes_exit_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, sha1, sha2 = self._repo_with_sensitive_history(Path(tmp))
            words = _words_file(Path(tmp))
            stdin_text = ("refs/heads/a {0} refs/heads/a {1}\n"
                          "refs/heads/b {0} refs/heads/b {1}\n").format(ZEROS40, sha1)
            code, out, err = _run_gatecheck(
                "pre-push", *_common_args(repo, words), stdin_text=stdin_text)
            self.assertEqual(code, 0, err)
            self.assertTrue(_load_json(out)["ok"])

    def test_only_deletes_still_scan_ref_names(self):
        # 纯删除没有对象可扫, 但被删的 ref 名仍会发给远端, 所以照扫。
        with tempfile.TemporaryDirectory() as tmp:
            repo, sha1, _ = self._repo_with_sensitive_history(Path(tmp))
            words = _words_file(Path(tmp))
            stdin_text = "refs/heads/{0} {1} refs/heads/{0} {2}\n".format(
                FAKE_WORDS[0], ZEROS40, sha1)
            code, out, err = _run_gatecheck(
                "pre-push", *_common_args(repo, words), stdin_text=stdin_text)
            self.assertEqual(code, 1)
            obj = _load_json(err)
            self.assertEqual(obj["error"]["code"], "E_LEAK_FOUND")
            hits = obj["error"]["details"]["hits"]
            self.assertTrue(all(h["origin"] == "ref" for h in hits), hits)
            self.assertTrue(any(h["rule"] == "WORD" for h in hits), hits)
            self.assertNotIn(FAKE_WORDS[0], out + err)

    def test_only_deletes_fail_closed_without_words(self):
        # 纯删除现在也要判, 判不了就得拒——不能因为"反正是删除"而放行。
        with tempfile.TemporaryDirectory() as tmp:
            repo, sha1, _ = self._repo_with_sensitive_history(Path(tmp))
            missing = Path(tmp) / "no-such-words.txt"
            code, out, err = _run_gatecheck(
                "pre-push", *_common_args(repo, missing),
                stdin_text="refs/heads/a {0} refs/heads/a {1}\n".format(ZEROS40, sha1))
            self.assertEqual(code, 1)
            self.assertEqual(_load_json(err)["error"]["code"], "E_GATE_UNAVAILABLE")

    def test_empty_stdin_exit_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, _sha1, _ = self._repo_with_sensitive_history(Path(tmp))
            words = _words_file(Path(tmp))
            code, out, err = _run_gatecheck(
                "pre-push", *_common_args(repo, words), stdin_text="")
            self.assertEqual(code, 0, err)
            self.assertTrue(_load_json(out)["ok"])

    def test_non_delete_empty_objects_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, sha1, _ = self._repo_with_sensitive_history(Path(tmp))
            words = _words_file(Path(tmp))
            code, out, err = _run_gatecheck(
                "pre-push", *_common_args(repo, words),
                stdin_text="refs/heads/main {0} refs/heads/main {0}\n".format(sha1))
            self.assertEqual(code, 1)
            self.assertEqual(_load_json(err)["error"]["code"], "E_GATE_UNAVAILABLE")

    def test_zero_sha_40_and_64_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, _sha1, sha2 = self._repo_with_sensitive_history(Path(tmp))
            words = _words_file(Path(tmp))
            # 40 位全 0 作为远端 sha：新 ref，扫完整历史 -> 命中
            code, _out, err = _run_gatecheck(
                "pre-push", *_common_args(repo, words),
                stdin_text="refs/heads/main {0} refs/heads/main {1}\n".format(sha2, ZEROS40))
            self.assertEqual(_load_json(err)["error"]["code"], "E_LEAK_FOUND")
            # 64 位全 0 作为远端 sha：同样按新 ref 处理
            code, _out, err = _run_gatecheck(
                "pre-push", *_common_args(repo, words),
                stdin_text="refs/heads/main {0} refs/heads/main {1}\n".format(sha2, ZEROS64))
            self.assertEqual(_load_json(err)["error"]["code"], "E_LEAK_FOUND")
            # 64 位全 0 作为本地 sha：删除，跳过
            code, out, _err = _run_gatecheck(
                "pre-push", *_common_args(repo, words),
                stdin_text="refs/heads/main {0} refs/heads/main {1}\n".format(ZEROS64, sha2))
            self.assertEqual(code, 0, _err)
            self.assertTrue(_load_json(out)["ok"])

    def test_not_remotes_trap_regression(self):
        # 敏感提交已被另一个 remote（refs/remotes/private/*）可达，但目标远端没有。
        # 若用 `rev-list <local> --not --remotes`，这个提交会被排除，扫到 0 个对象。
        # 本实现按目标远端算范围，新 ref 扫完整历史，必须扫到。
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(Path(tmp))
            sha1 = _commit(repo, {"secret.txt": "carol"}, "secret")
            _git(repo, "update-ref", "refs/remotes/private/main", sha1)
            words = _words_file(Path(tmp))
            code, out, err = _run_gatecheck(
                "pre-push", *_common_args(repo, words),
                stdin_text="refs/heads/main {0} refs/heads/main {1}\n".format(sha1, ZEROS40))
            self.assertEqual(code, 1, "buggy --not --remotes would miss this")
            self.assertEqual(_load_json(err)["error"]["code"], "E_LEAK_FOUND")

    def test_multi_ref_objects_deduped(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(Path(tmp))
            sha1 = _commit(repo, {"a.txt": "carol"}, "first")
            _git(repo, "branch", "feat")
            words = _words_file(Path(tmp))
            stdin_text = ("refs/heads/main {0} refs/heads/main {1}\n"
                          "refs/heads/feat {0} refs/heads/feat {1}\n").format(sha1, ZEROS40)
            code, _out, err = _run_gatecheck(
                "pre-push", *_common_args(repo, words), stdin_text=stdin_text)
            self.assertEqual(code, 1)
            obj = _load_json(err)
            self.assertEqual(obj["error"]["code"], "E_LEAK_FOUND")
            blob_hits = [h for h in obj["error"]["details"]["hits"]
                         if h["origin"] == "blob:a.txt"]
            self.assertEqual(len(blob_hits), 1)


class TestScanContent(unittest.TestCase):
    def test_blob_content_hit(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(Path(tmp))
            sha = _commit(repo, {"data.txt": "hello carol"}, "first")
            words = _words_file(Path(tmp))
            code, _out, err = _run_gatecheck(
                "pre-push", *_common_args(repo, words), stdin_text=_new_ref_stdin(sha))
            self.assertEqual(code, 1)
            hits = _load_json(err)["error"]["details"]["hits"]
            self.assertTrue(any(h["origin"].startswith("blob:") for h in hits), hits)

    def test_path_name_hit(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(Path(tmp))
            sha = _commit(repo, {"carol-notes.txt": "clean"}, "first")
            words = _words_file(Path(tmp))
            code, _out, err = _run_gatecheck(
                "pre-push", *_common_args(repo, words), stdin_text=_new_ref_stdin(sha))
            self.assertEqual(code, 1)
            hits = _load_json(err)["error"]["details"]["hits"]
            self.assertTrue(any(h["origin"] == "path" for h in hits), hits)

    def test_renamed_new_path_hit(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(Path(tmp))
            sha1 = _commit(repo, {"old.txt": "same content"}, "base")
            _git(repo, "mv", "old.txt", "carol.txt")
            sha2 = _commit(repo, {}, "rename")
            words = _words_file(Path(tmp))
            code, _out, err = _run_gatecheck(
                "pre-push", *_common_args(repo, words),
                stdin_text="refs/heads/main {0} refs/heads/main {1}\n".format(sha2, ZEROS40))
            self.assertEqual(code, 1, err)
            hits = _load_json(err)["error"]["details"]["hits"]
            self.assertTrue(any(h["origin"] == "path" for h in hits), hits)

    def test_commit_author_email_hit(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(Path(tmp))
            sha = _commit(repo, {"a.txt": "clean"}, "first", env={
                "GIT_AUTHOR_NAME": "bob",
                "GIT_AUTHOR_EMAIL": SENSITIVE_EMAIL,
                "GIT_COMMITTER_NAME": "bob",
                "GIT_COMMITTER_EMAIL": SAFE_EMAIL,
            })
            words = _words_file(Path(tmp))
            code, _out, err = _run_gatecheck(
                "pre-push", *_common_args(repo, words), stdin_text=_new_ref_stdin(sha))
            self.assertEqual(code, 1, err)
            hits = _load_json(err)["error"]["details"]["hits"]
            self.assertTrue(any(h["origin"].endswith(":author") and h["rule"] == "EMAIL"
                                for h in hits), hits)

    def test_committer_email_hit(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(Path(tmp))
            sha = _commit(repo, {"a.txt": "clean"}, "first", env={
                "GIT_AUTHOR_NAME": "bob",
                "GIT_AUTHOR_EMAIL": SAFE_EMAIL,
                "GIT_COMMITTER_NAME": "bob",
                "GIT_COMMITTER_EMAIL": SENSITIVE_EMAIL,
            })
            words = _words_file(Path(tmp))
            code, _out, err = _run_gatecheck(
                "pre-push", *_common_args(repo, words), stdin_text=_new_ref_stdin(sha))
            self.assertEqual(code, 1, err)
            hits = _load_json(err)["error"]["details"]["hits"]
            self.assertTrue(any(h["origin"].endswith(":committer") and h["rule"] == "EMAIL"
                                for h in hits), hits)

    def test_commit_message_hit(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(Path(tmp))
            sha = _commit(repo, {"a.txt": "clean"}, "release notes for carol")
            words = _words_file(Path(tmp))
            code, _out, err = _run_gatecheck(
                "pre-push", *_common_args(repo, words), stdin_text=_new_ref_stdin(sha))
            self.assertEqual(code, 1, err)
            hits = _load_json(err)["error"]["details"]["hits"]
            self.assertTrue(any(h["origin"].endswith(":message") for h in hits), hits)

    def test_local_ref_name_hit(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(Path(tmp))
            sha = _commit(repo, {"a.txt": "clean"}, "first")
            _git(repo, "branch", "carol-feature")
            words = _words_file(Path(tmp))
            code, _out, err = _run_gatecheck(
                "pre-push", *_common_args(repo, words),
                stdin_text="refs/heads/carol-feature {0} refs/heads/carol-feature {1}\n"
                           .format(sha, ZEROS40))
            self.assertEqual(code, 1, err)
            hits = _load_json(err)["error"]["details"]["hits"]
            self.assertTrue(any(h["origin"] == "ref" for h in hits), hits)

    def test_remote_ref_name_hit(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(Path(tmp))
            sha1 = _commit(repo, {"a.txt": "clean"}, "first")
            sha2 = _commit(repo, {"b.txt": "clean"}, "second")
            words = _words_file(Path(tmp))
            code, _out, err = _run_gatecheck(
                "pre-push", *_common_args(repo, words),
                stdin_text="refs/heads/main {0} refs/heads/carol-dest {1}\n"
                           .format(sha2, sha1))
            self.assertEqual(code, 1, err)
            hits = _load_json(err)["error"]["details"]["hits"]
            self.assertTrue(any(h["origin"] == "ref" for h in hits), hits)

    def test_annotated_tag_tagger_and_message_hit(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(Path(tmp))
            _commit(repo, {"a.txt": "clean"}, "first")
            _git_env(repo, ["tag", "-a", "v1", "-m", "tag message for carol"],
                     {"GIT_COMMITTER_NAME": "carol",
                      "GIT_COMMITTER_EMAIL": SENSITIVE_EMAIL_CAROL})
            words = _words_file(Path(tmp))
            code, _out, err = _run_gatecheck(
                "pre-push", *_common_args(repo, words),
                stdin_text="refs/tags/v1 {0} refs/tags/v1 {1}\n".format(
                    _sha(repo, "v1"), ZEROS40))
            self.assertEqual(code, 1, err)
            hits = _load_json(err)["error"]["details"]["hits"]
            self.assertTrue(any(h["origin"] == "tag:v1:tagger" for h in hits), hits)
            self.assertTrue(any(h["origin"] == "tag:v1:message" for h in hits), hits)

    def test_gitlink_no_full_hash_but_path_scanned(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _make_repo(root)
            sub = root / "sub"
            sub.mkdir()
            _git(sub, "init", "-q")
            empty_hooks = sub / ".empty-hooks"
            empty_hooks.mkdir()
            _git(sub, "config", "core.hooksPath", str(empty_hooks))
            _git(sub, "config", "user.name", "bob")
            _git(sub, "config", "user.email", SAFE_EMAIL)
            subsha = _commit(sub, {"s.txt": "sub content"}, "sub")
            _commit(repo, {"keep.txt": "clean"}, "base")
            _git(repo, "update-index", "--add", "--cacheinfo",
                 "160000," + subsha + ",carol-sub")
            _git(repo, "commit", "-q", "-m", "add submodule")
            sha = _sha(repo)
            words = _words_file(root)
            code, _out, err = _run_gatecheck(
                "pre-push", *_common_args(repo, words), stdin_text=_new_ref_stdin(sha))
            self.assertEqual(code, 1, err)
            hits = _load_json(err)["error"]["details"]["hits"]
            self.assertTrue(any(h["origin"] == "path" for h in hits), hits)
            self.assertFalse(any(h["rule"] == "FULL_HASH" for h in hits), hits)


class TestDecoding(unittest.TestCase):
    def test_utf8_blob_hit(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(Path(tmp))
            sha = _commit(repo, {"u.txt": "carol \u8bf4\u660e"}, "first")
            words = _words_file(Path(tmp))
            code, _out, err = _run_gatecheck(
                "pre-push", *_common_args(repo, words), stdin_text=_new_ref_stdin(sha))
            self.assertEqual(code, 1, err)
            hits = _load_json(err)["error"]["details"]["hits"]
            self.assertTrue(any(h["rule"] == "WORD" for h in hits), hits)

    def test_cp936_blob_hit(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(Path(tmp))
            gbk_bytes = ("carol\u4e2d").encode("gbk")
            self.assertRaises(UnicodeDecodeError, gbk_bytes.decode, "utf-8")
            sha = _commit(repo, {"g.txt": gbk_bytes}, "first")
            words = _words_file(Path(tmp))
            code, _out, err = _run_gatecheck(
                "pre-push", *_common_args(repo, words), stdin_text=_new_ref_stdin(sha))
            self.assertEqual(code, 1, err)
            hits = _load_json(err)["error"]["details"]["hits"]
            self.assertTrue(any(h["rule"] == "WORD" for h in hits), hits)

    def test_binary_blob_no_crash_no_skip(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(Path(tmp))
            binary = b"carol " + bytes(range(128, 256))
            sha = _commit(repo, {"bin.dat": binary}, "first")
            words = _words_file(Path(tmp))
            code, _out, err = _run_gatecheck(
                "pre-push", *_common_args(repo, words), stdin_text=_new_ref_stdin(sha))
            self.assertEqual(code, 1, err)
            hits = _load_json(err)["error"]["details"]["hits"]
            self.assertTrue(any(h["rule"] == "WORD" for h in hits),
                            "binary blob must still be scanned, not skipped")


class TestSeverity(unittest.TestCase):
    def _hex(self, n):
        return ("0123456789abcdef" * ((n + 15) // 16))[:n]

    def test_only_warn_exits_zero_and_listed(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(Path(tmp))
            wiki = "[" + "[note]" + "]"
            hex40 = self._hex(40)
            sha = _commit(repo, {"w.txt": wiki + " " + hex40}, "first")
            words = _words_file(Path(tmp))
            code, out, err = _run_gatecheck(
                "pre-push", *_common_args(repo, words), stdin_text=_new_ref_stdin(sha))
            self.assertEqual(code, 0, err)
            obj = _load_json(out)
            self.assertTrue(obj["ok"])
            rules = {h["rule"]: h["severity"] for h in obj["data"]["hits"]}
            self.assertIn("WIKI_LINK", rules)
            self.assertIn("FULL_HASH", rules)
            self.assertEqual(rules["WIKI_LINK"], "warn")
            self.assertEqual(rules["FULL_HASH"], "warn")

    def test_block_hit_exits_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(Path(tmp))
            sha = _commit(repo, {"a.txt": "carol"}, "first")
            words = _words_file(Path(tmp))
            code, _out, err = _run_gatecheck(
                "pre-push", *_common_args(repo, words), stdin_text=_new_ref_stdin(sha))
            self.assertEqual(code, 1)
            self.assertEqual(_load_json(err)["error"]["code"], "E_LEAK_FOUND")


class TestFailClosed(unittest.TestCase):
    def _reject(self, code, err):
        self.assertEqual(code, 1, err)
        obj = _load_json(err)
        self.assertEqual(obj["error"]["code"], "E_GATE_UNAVAILABLE", err)
        return obj

    def test_secretscan_import_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(Path(tmp))
            sha = _commit(repo, {"a.txt": "carol"}, "first")
            words = _words_file(Path(tmp))
            with mock.patch.dict(sys.modules, {"secretscan": None}):
                code, _out, err = _run_gatecheck(
                    "pre-push", *_common_args(repo, words),
                    stdin_text=_new_ref_stdin(sha))
            self._reject(code, err)

    def test_words_file_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(Path(tmp))
            sha = _commit(repo, {"a.txt": "carol"}, "first")
            missing = Path(tmp) / "nope.txt"
            code, _out, err = _run_gatecheck(
                "pre-push", *_common_args(repo, missing), stdin_text=_new_ref_stdin(sha))
            self._reject(code, err)

    def test_words_file_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(Path(tmp))
            sha = _commit(repo, {"a.txt": "carol"}, "first")
            empty = Path(tmp) / "empty.txt"
            empty.write_text("", encoding="utf-8")
            code, _out, err = _run_gatecheck(
                "pre-push", *_common_args(repo, empty), stdin_text=_new_ref_stdin(sha))
            self._reject(code, err)

    def test_words_file_read_or_decrypt_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(Path(tmp))
            sha = _commit(repo, {"a.txt": "carol"}, "first")
            bad = Path(tmp) / "bad.txt"
            bad.write_bytes(b"\xff\xfe\x80bad")
            code, _out, err = _run_gatecheck(
                "pre-push", *_common_args(repo, bad), stdin_text=_new_ref_stdin(sha))
            self._reject(code, err)

    def test_git_command_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(Path(tmp))
            _commit(repo, {"a.txt": "carol"}, "first")
            words = _words_file(Path(tmp))
            missing_sha = "a" * 40
            code, _out, err = _run_gatecheck(
                "pre-push", *_common_args(repo, words),
                stdin_text=_new_ref_stdin(missing_sha))
            self._reject(code, err)

    def test_git_output_parse_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(Path(tmp))
            sha = _commit(repo, {"a.txt": "carol"}, "first")
            words = _words_file(Path(tmp))
            real_git = GateCheck._git

            def fake_git(repo_arg, args):
                if args[0] == "rev-list":
                    return "not-an-oid somepath\n"
                return real_git(repo_arg, args)

            with mock.patch.object(GateCheck, "_git", side_effect=fake_git):
                code, _out, err = _run_gatecheck(
                    "pre-push", *_common_args(repo, words),
                    stdin_text=_new_ref_stdin(sha))
            self._reject(code, err)

    def test_repo_not_git_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "not-a-repo"
            repo.mkdir()
            words = _words_file(Path(tmp))
            code, _out, err = _run_gatecheck(
                "pre-push", *_common_args(repo, words),
                stdin_text=_new_ref_stdin("a" * 40))
            self._reject(code, err)

    def test_replacements_missing_still_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(Path(tmp))
            sha = _commit(repo, {"a.txt": "carol"}, "first")
            words = _words_file(Path(tmp))
            code, _out, err = _run_gatecheck(
                "pre-push", *_common_args(repo, words), stdin_text=_new_ref_stdin(sha))
            self.assertEqual(code, 1)
            obj = _load_json(err)
            self.assertEqual(obj["error"]["code"], "E_LEAK_FOUND")
            self.assertEqual(obj["error"]["details"]["hits"][0]["suggestion"], "")

    def test_replacements_suggestion_applied(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(Path(tmp))
            sha = _commit(repo, {"a.txt": "carol"}, "first")
            words = _words_file(Path(tmp))
            repl = Path(tmp) / "repl.txt"
            repl.write_text("carol => dave\n", encoding="utf-8")
            code, _out, err = _run_gatecheck(
                "pre-push", *_common_args(repo, words),
                "--replacements-file", str(repl),
                stdin_text=_new_ref_stdin(sha))
            self.assertEqual(code, 1)
            obj = _load_json(err)
            self.assertEqual(obj["error"]["details"]["hits"][0]["suggestion"], "dave")


class TestScanRepo(unittest.TestCase):
    def test_full_history_hit(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(Path(tmp))
            sha = _commit(repo, {"a.txt": "carol"}, "first")
            words = _words_file(Path(tmp))
            code, _out, err = _run_gatecheck(
                "scan-repo", "--repo", str(repo), "--refs", "HEAD",
                "--words-file", str(words), "--identity-words", FAKE_IDENTITY,
                "--json")
            self.assertEqual(code, 1, err)
            obj = _load_json(err)
            self.assertEqual(obj["error"]["code"], "E_LEAK_FOUND")
            self.assertTrue(any(h["rule"] == "WORD" for h in obj["error"]["details"]["hits"]))

    def test_clean_exit_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(Path(tmp))
            _commit(repo, {"a.txt": "clean"}, "first")
            words = _words_file(Path(tmp))
            code, out, err = _run_gatecheck(
                "scan-repo", "--repo", str(repo), "--refs", "HEAD",
                "--words-file", str(words), "--identity-words", FAKE_IDENTITY,
                "--json")
            self.assertEqual(code, 0, err)
            obj = _load_json(out)
            self.assertTrue(obj["ok"])


class TestEnvelope(unittest.TestCase):
    def test_clean_exit_zero_ok_true(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(Path(tmp))
            sha = _commit(repo, {"a.txt": "clean"}, "first")
            words = _words_file(Path(tmp))
            code, out, err = _run_gatecheck(
                "pre-push", *_common_args(repo, words),
                stdin_text="refs/heads/main {0} refs/heads/main {1}\n".format(sha, ZEROS40))
            self.assertEqual(code, 0, err)
            obj = _load_json(out)
            self.assertTrue(obj["ok"])
            self.assertIsNone(obj["error"])
            self.assertEqual(obj["data"]["hits"], [])

    def test_hit_display_masked_not_in_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(Path(tmp))
            sha = _commit(repo, {"a.txt": "hello carol"}, "first")
            words = _words_file(Path(tmp))
            code, out, err = _run_gatecheck(
                "pre-push", *_common_args(repo, words), stdin_text=_new_ref_stdin(sha))
            self.assertEqual(code, 1)
            self.assertNotIn("carol", out)
            self.assertNotIn("carol", err)
            obj = _load_json(err)
            hit = obj["error"]["details"]["hits"][0]
            self.assertEqual(hit["display"], "*****")

    def test_ai_help_exit_zero_parseable(self):
        out, err = io.StringIO(), io.StringIO()
        sinks = cc.Sinks(out=out, err=err)
        code = GateCheck.main(["--ai-help"], sinks, reconfigure=False)
        self.assertEqual(code, 0)
        text = out.getvalue()
        self.assertTrue(text.startswith("---"))
        self.assertIn("name: GateCheck", text)
        self.assertIn("description:", text)
        self.assertIn("## Severity & Exit Codes", text)
        self.assertIn("## Errors & Recovery", text)


class TestArm(unittest.TestCase):
    """arm 只回答该不该武装：URL 规范化 + 豁免表查询，不扫描、不读词表、不碰 git。"""

    def _arm(self, url, home=None):
        argv = ["arm", "--url", url, "--json"]
        if home is not None:
            argv += ["--home", str(home)]
        code, out, err = _run_gatecheck(*argv)
        return code, out, err

    def _data(self, url, home=None):
        code, out, err = self._arm(url, home)
        self.assertEqual(code, 0, err)
        obj = _load_json(out)
        self.assertTrue(obj["ok"], out)
        return obj["data"]

    def _empty_home(self, tmp):
        home = Path(tmp) / "cfg"
        home.mkdir()
        return home

    def test_url_shapes_normalize_to_same_key(self):
        # §3.1：各种形状归一到同一个 github.com/owner/repo
        owner_repo = "bob" + "/" + "projx"
        shapes = [
            "https://github.com/" + owner_repo,
            "https://github.com/" + owner_repo + ".git",
            "https://github.com/" + owner_repo + "/",
            "git@github.com:" + owner_repo + ".git",
            "ssh://git@github.com/" + owner_repo,
            "HTTPS://GitHub.COM/" + "Bob" + "/" + "ProjX" + ".git",
        ]
        with tempfile.TemporaryDirectory() as tmp:
            home = self._empty_home(tmp)
            for url in shapes:
                data = self._data(url, home)
                self.assertTrue(data["is_github"], url)
                self.assertEqual(data["normalized"], "github.com/" + owner_repo, url)
                self.assertTrue(data["armed"], url)
                self.assertEqual(data["reason"], "no_exempt", url)

    def test_credentials_dropped_and_never_echoed(self):
        # 凭据部分必须丢弃，且绝不出现在输出里
        secret = "dave"
        url = "https://bob:" + secret + "@github.com/bob/projx.git"
        with tempfile.TemporaryDirectory() as tmp:
            home = self._empty_home(tmp)
            code, out, err = self._arm(url, home)
            self.assertEqual(code, 0, err)
            self.assertEqual(_load_json(out)["data"]["normalized"],
                             "github.com/bob/projx")
            self.assertNotIn(secret, out)
            self.assertNotIn(secret, err)

    def test_non_github_not_armed(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = self._empty_home(tmp)
            data = self._data("https://gerrit.company.com/projx", home)
            self.assertFalse(data["is_github"])
            self.assertFalse(data["armed"])
            self.assertEqual(data["reason"], "not_github")
            self.assertIsNone(data["normalized"])

    def test_github_in_path_but_other_host_not_armed(self):
        # sh 侧过近似会把它放进第二段，python 这一段才是权威判定
        with tempfile.TemporaryDirectory() as tmp:
            home = self._empty_home(tmp)
            data = self._data("https://gitlab.example.com/github/x", home)
            self.assertFalse(data["is_github"])
            self.assertFalse(data["armed"])

    def test_unrecognized_path_errs_toward_arming(self):
        # 多余路径段判为不可识别 -> 往"武装"倒，绝不往"放行"倒
        with tempfile.TemporaryDirectory() as tmp:
            home = self._empty_home(tmp)
            for url in ("https://github.com/a/b/c", "https://github.com/onlyowner"):
                data = self._data(url, home)
                self.assertTrue(data["is_github"], url)
                self.assertIsNone(data["normalized"], url)
                self.assertTrue(data["armed"], url)
                self.assertEqual(data["reason"], "unrecognized_path", url)

    def test_exempt_file_missing_means_armed(self):
        # 豁免表不存在 -> 无豁免 -> 武装（保守方向，不是 fail-closed 报错）
        with tempfile.TemporaryDirectory() as tmp:
            data = self._data(PROJX_URL, Path(tmp) / "no-such-home")
            self.assertTrue(data["armed"])
            self.assertEqual(data["exempt_state"], "none")

    def test_exempt_fresh_not_armed(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = self._empty_home(tmp)
            (home / "gate-exempt.txt").write_text(
                "github.com/bob/projx {0}\n".format(datetime.now().isoformat()),
                encoding="utf-8")
            data = self._data(PROJX_URL, home)
            self.assertFalse(data["armed"])
            self.assertEqual(data["exempt_state"], "fresh")
            self.assertEqual(data["reason"], "exempt_fresh")

    def test_exempt_expired_armed(self):
        # §3.3 本批的有意偏离：过期视为未豁免，照常武装并扫描（不联网复核）
        with tempfile.TemporaryDirectory() as tmp:
            home = self._empty_home(tmp)
            stale = (datetime.now() - timedelta(days=TTL_OVER_DAYS)).isoformat()
            (home / "gate-exempt.txt").write_text(
                "github.com/bob/projx {0}\n".format(stale), encoding="utf-8")
            data = self._data(PROJX_URL, home)
            self.assertTrue(data["armed"])
            self.assertEqual(data["exempt_state"], "expired")
            self.assertEqual(data["reason"], "exempt_expired")

    def test_exempt_match_is_exact_not_substring(self):
        # 前缀相同的另一个仓不得把本仓豁免掉
        with tempfile.TemporaryDirectory() as tmp:
            home = self._empty_home(tmp)
            (home / "gate-exempt.txt").write_text(
                "github.com/bob/projx-extra {0}\n".format(datetime.now().isoformat()),
                encoding="utf-8")
            data = self._data(PROJX_URL, home)
            self.assertTrue(data["armed"])
            self.assertEqual(data["exempt_state"], "none")

    def test_bad_timestamp_entry_is_dropped(self):
        # checked_at 解析不了的条目直接丢弃 -> 宁可武装
        with tempfile.TemporaryDirectory() as tmp:
            home = self._empty_home(tmp)
            (home / "gate-exempt.txt").write_text(
                "github.com/bob/projx not-a-timestamp\n", encoding="utf-8")
            data = self._data(PROJX_URL, home)
            self.assertTrue(data["armed"])

    def test_missing_url_exits_two(self):
        code, out, err = _run_gatecheck("arm", "--json")
        self.assertEqual(code, 2, out + err)


TTL_OVER_DAYS = GateCheck.TTL_DAYS + 1


if __name__ == "__main__":
    unittest.main()
