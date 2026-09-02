"""secretscan 共享规则模块的单测。

正例夹具一律运行时拼接，源码里不出现完整敏感形状——否则本文件自己会被发布闸
test_no_secrets.py 扫到。
"""

import os
import sys
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import secretscan  # noqa: E402
from rulecorpus import (  # noqa: E402
    WORDS, IDENTITY_WORDS, corpus_lines, EXPECTED_HITS, NEGATIVE_LINE_INDICES,
)


class TestMask(unittest.TestCase):
    def test_short_values_fully_masked(self):
        self.assertEqual(secretscan.mask(""), "")
        self.assertEqual(secretscan.mask("a"), "*")
        self.assertEqual(secretscan.mask("ab"), "**")
        self.assertEqual(secretscan.mask("abc"), "***")
        self.assertEqual(secretscan.mask("bob"), "***")
        self.assertEqual(secretscan.mask("123456"), "******")

    def test_keeps_first_and_last(self):
        self.assertEqual(secretscan.mask("1234567"), "1*****7")
        self.assertEqual(secretscan.mask("abcd1234"), "a******4")
        cred = "password=" + "real-secret-value"
        self.assertEqual(secretscan.mask(cred), "p" + "*" * 24 + "e")

    def test_threshold_boundary_six_and_seven(self):
        # 修复项 E：长度 6 整体打星，长度 7 保留首尾
        self.assertEqual(secretscan.mask("123456"), "******")
        self.assertEqual(secretscan.mask("1234567"), "1*****7")

    def test_no_original_value_in_display(self):
        value = "C:\\Users\\" + "mallory" + "\\repo"
        masked = secretscan.mask(value)
        self.assertNotIn("mallory", masked)


class TestHit(unittest.TestCase):
    def _hit(self, **kw):
        base = dict(origin="blob:x", line=1, column=0, rule="HOME_DIR",
                    severity="block", display="m**d", suggestion="")
        base.update(kw)
        return secretscan.Hit(**base)

    def test_frozen(self):
        hit = self._hit()
        with self.assertRaises(FrozenInstanceError):
            hit.display = "tampered"

    def test_display_is_masked(self):
        hit = self._hit(display="p************************e")
        self.assertNotIn("real", hit.display)


class TestContainsIdentity(unittest.TestCase):
    def test_word_boundary_semantics(self):
        self.assertTrue(secretscan.contains_identity("bob", "bob"))
        self.assertTrue(secretscan.contains_identity("bob,", "bob"))
        self.assertTrue(secretscan.contains_identity("BOB", "bob"))
        self.assertTrue(secretscan.contains_identity("x_bob_x", "bob"))
        self.assertFalse(secretscan.contains_identity("bobsled", "bob"))
        self.assertFalse(secretscan.contains_identity("mobob", "bob"))
        self.assertFalse(secretscan.contains_identity("bob2", "bob"))
        self.assertFalse(secretscan.contains_identity("2bob", "bob"))

    def test_identity_match_reports_boundary_column(self):
        # 修复项 D：列号取词边界 match.start()，不再指向不构成命中的 find 位置
        m = secretscan.identity_match("mybob bob", "bob")
        self.assertIsNotNone(m)
        self.assertEqual(m.start(), 6)
        self.assertIsNone(secretscan.identity_match("bobsled", "bob"))
        hits = secretscan.scan_text("mybob bob", origin="t",
                                    words=[], identity_words=["bob"])
        self.assertEqual([h.rule for h in hits], ["IDENTITY"])
        self.assertEqual([h.column for h in hits], [6])


class TestIsPlaceholderSecret(unittest.TestCase):
    def test_marker_entry_points(self):
        for value in ("<password>", "${secret}", "{{ secret }}", "***"):
            self.assertTrue(secretscan.is_placeholder_secret(value), value)
        for value in ("example-key", "placeholder-x", "dummy123",
                      "changeme1", "redacted", "xxx"):
            self.assertTrue(secretscan.is_placeholder_secret(value), value)
        self.assertFalse(secretscan.is_placeholder_secret("real-secret-value"))
        self.assertFalse(secretscan.is_placeholder_secret("K7x9mQ2vLp4nR8sT6uW3yA1zB"))


class TestLoaders(unittest.TestCase):
    def test_load_words_parsing(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "words.txt"
            f.write_text("alpha\n# comment\nbeta \n  gamma  \n", encoding="utf-8")
            self.assertEqual(secretscan.load_words(str(f)),
                             ["alpha", "beta", "gamma"])

    def test_load_words_missing_is_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(secretscan.load_words(str(Path(tmp) / "nope.txt")))

    def test_load_replacements_parsing(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "repl.txt"
            f.write_text("bob => alice\n# note\ncarol => dave\n\n", encoding="utf-8")
            self.assertEqual(secretscan.load_replacements(str(f)),
                             {"bob": "alice", "carol": "dave"})

    def test_load_replacements_missing_is_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(secretscan.load_replacements(str(Path(tmp) / "nope.txt")), {})


class TestLocalIdentityWords(unittest.TestCase):
    def test_returns_nonempty_strings(self):
        words = secretscan.local_identity_words()
        self.assertTrue(words)
        self.assertTrue(all(isinstance(w, str) and w for w in words))


class TestScanText(unittest.TestCase):
    def _scan(self, line, words=(), identity=()):
        return secretscan.scan_text(line, origin="t", words=list(words),
                                    identity_words=list(identity))

    def test_home_dir_hit_and_whitelisted(self):
        hits = self._scan("C:\\Users\\" + "mallory" + "\\repo")
        self.assertEqual([h.rule for h in hits], ["HOME_DIR"])
        self.assertEqual(hits[0].line, 1)
        self.assertEqual(hits[0].severity, "block")
        self.assertNotIn("mallory", hits[0].display)
        self.assertEqual(self._scan("C:\\Users\\" + "example" + "\\repo"), [])

    def test_degenerate_home_dir_ignored(self):
        self.assertEqual(self._scan(r"C:\Users\.\repo"), [])
        self.assertEqual(self._scan(r"C:\Users\..\repo"), [])
        self.assertEqual(self._scan(r"C:\Users\1234\repo"), [])

    def test_email_ok_expanded(self):
        self.assertEqual(self._scan("test@" + "example.invalid"), [])
        self.assertEqual(self._scan("git@" + "github.com"), [])
        self.assertEqual(self._scan("noreply@" + "github.com"), [])
        hits = self._scan("mallory@" + "projx.com")
        self.assertEqual([h.rule for h in hits], ["EMAIL"])

    def test_email_whitelist_is_exact_domain(self):
        # 修复项 A：整域白名单改完整域匹配，后缀绕过与超出授权的域都被拦
        self.assertEqual([h.rule for h in self._scan("support@" + "github.com")], ["EMAIL"])
        self.assertEqual([h.rule for h in self._scan("mallory@" + "github.com.evil.com")], ["EMAIL"])
        self.assertEqual([h.rule for h in self._scan("person@" + "example.com.evil.com")], ["EMAIL"])
        self.assertEqual([h.rule for h in self._scan("x@" + "myexample.com")], ["EMAIL"])

    def test_email_whitelist_addresses_still_allowed(self):
        self.assertEqual(self._scan("git@" + "github.com"), [])
        self.assertEqual(self._scan("noreply@" + "github.com"), [])
        self.assertEqual(self._scan("dev@" + "users.noreply.github.com"), [])

    def test_token_placeholder_filtered(self):
        self.assertEqual(self._scan("sk-" + "example" + "x" * 22), [])
        hits = self._scan("ghp_" + "K7x9mQ2vLp4nR8sT6uW3yA1zB")
        self.assertEqual([h.rule for h in hits], ["TOKEN"])

    def test_alphabet_token_still_hit(self):
        # D8-4 只过滤占位 token；字母表顺序假 token 仍命中（已知限制，见报告）
        hits = self._scan("ghp_" + "abcdefghijklmnopqrstuvwxyz0123")
        self.assertEqual([h.rule for h in hits], ["TOKEN"])

    def test_wiki_posix_class_not_a_link(self):
        # 修复项 F：POSIX 字符类形状不再被当私人笔记双链
        self.assertEqual(self._scan("[" + "[:space:]" + "]"), [])
        self.assertEqual(self._scan("[" + "[:alpha:]" + "]"), [])
        self.assertEqual(self._scan("[" + "[:digit:]" + "]"), [])
        # 内部含冒号但不是字符类形状，仍然命中
        hits = self._scan("[" + "[a:b]" + "]")
        self.assertEqual([h.rule for h in hits], ["WIKI_LINK"])
        hits = self._scan("[" + "[:not a class:]" + "]")
        self.assertEqual([h.rule for h in hits], ["WIKI_LINK"])

    def test_warn_severity_for_wiki_and_hash(self):
        wiki_line = "[" + "[" + "note" + "]" + "]"
        hits = self._scan(wiki_line)
        self.assertEqual([h.rule for h in hits], ["WIKI_LINK"])
        self.assertEqual(hits[0].severity, "warn")
        hex40 = ("0123456789abcdef" * 3)[:40]
        hits = self._scan(hex40)
        self.assertEqual([h.rule for h in hits], ["FULL_HASH"])
        self.assertEqual(hits[0].severity, "warn")

    def test_word_substring_vs_identity_boundary(self):
        text = "\n".join(["the " + "zzqx" + " thing", "bobsled", "bob,"])
        hits = self._scan(text, words=("zzqx",), identity=("bob",))
        rules_by_line = {}
        for h in hits:
            rules_by_line.setdefault(h.line, []).append(h.rule)
        self.assertEqual(rules_by_line, {1: ["WORD"], 3: ["IDENTITY"]})

    def test_origin_propagated(self):
        hits = self._scan("bob", identity=("bob",))
        self.assertEqual(hits[0].origin, "t")

    def test_empty_text(self):
        self.assertEqual(self._scan(""), [])

    def test_multiple_hits_one_line(self):
        line = ("moved " + "/home/" + "victor" + " to "
                + "C:\\Users\\" + "mallory" + "\\repo")
        hits = self._scan(line)
        self.assertEqual([h.rule for h in hits], ["HOME_DIR", "HOME_DIR"])


class TestScanName(unittest.TestCase):
    def test_line_is_zero(self):
        hits = secretscan.scan_name("refs/heads/" + "bob" + "-feature",
                                    origin="ref", words=[],
                                    identity_words=["bob"])
        self.assertTrue(hits)
        self.assertTrue(all(h.line == 0 for h in hits))

    def test_same_hits_as_scan_text(self):
        name = "C:\\Users\\" + "mallory" + "\\repo"
        from_name = secretscan.scan_name(name, origin="ref", words=[], identity_words=[])
        from_text = secretscan.scan_text(name, origin="ref", words=[], identity_words=[])
        self.assertEqual(from_name[0].display, from_text[0].display)
        self.assertEqual(from_name[0].line, 0)
        self.assertEqual(from_text[0].line, 1)


class TestCredentialHits(unittest.TestCase):
    def test_d8_4_applies_to_tokens(self):
        self.assertTrue(list(secretscan.credential_hits("ghp_" + "K7x9mQ2vLp4nR8sT6uW3yA1zB")))
        self.assertEqual(list(secretscan.credential_hits("sk-" + "example" + "x" * 22)), [])

    def test_placeholder_assignment_allowed(self):
        self.assertEqual(list(secretscan.credential_hits("password=<password>")), [])
        self.assertEqual(list(secretscan.credential_hits("api_key=example-key")), [])

    def test_credential_matches_single_implementation(self):
        # 修复项 G：credential_matches 是唯一实现，credential_hits 只取其值
        line = "ghp_" + "K7x9mQ2vLp4nR8sT6uW3yA1zB"
        matches = list(secretscan.credential_matches(line))
        self.assertEqual([m[0] for m in matches], ["TOKEN"])
        self.assertEqual(matches[0][1], line.find("ghp_"))
        self.assertEqual([v for _, _, v in matches],
                         list(secretscan.credential_hits(line)))


class TestSuggestion(unittest.TestCase):
    def test_suggestion_from_replacements(self):
        with tempfile.TemporaryDirectory() as tmp:
            repl = Path(tmp) / "gate-replacements.txt"
            repl.write_text("bob => alice\n", encoding="utf-8")
            old = secretscan.DEFAULT_REPLACEMENTS_PATH
            secretscan.DEFAULT_REPLACEMENTS_PATH = repl
            try:
                hits = secretscan.scan_text("bob", origin="t", words=[],
                                            identity_words=["bob"])
                self.assertEqual(hits[0].suggestion, "alice")
            finally:
                secretscan.DEFAULT_REPLACEMENTS_PATH = old
                secretscan.clear_replacements_cache()

    def test_suggestion_empty_without_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = secretscan.DEFAULT_REPLACEMENTS_PATH
            secretscan.DEFAULT_REPLACEMENTS_PATH = Path(tmp) / "none.txt"
            try:
                hits = secretscan.scan_text("bob", origin="t", words=[],
                                            identity_words=["bob"])
                self.assertEqual(hits[0].suggestion, "")
            finally:
                secretscan.DEFAULT_REPLACEMENTS_PATH = old
                secretscan.clear_replacements_cache()

    def test_replacements_cached_until_cleared(self):
        # 修复项 C：进程内快照缓存，clear_replacements_cache 之后重读
        with tempfile.TemporaryDirectory() as tmp:
            repl = Path(tmp) / "repl.txt"
            repl.write_text("bob => alice\n", encoding="utf-8")
            old = secretscan.DEFAULT_REPLACEMENTS_PATH
            secretscan.DEFAULT_REPLACEMENTS_PATH = repl
            try:
                self.assertEqual(secretscan.load_replacements(), {"bob": "alice"})
                repl.write_text("bob => carol\n", encoding="utf-8")
                self.assertEqual(secretscan.load_replacements(), {"bob": "alice"})
                secretscan.clear_replacements_cache()
                self.assertEqual(secretscan.load_replacements(), {"bob": "carol"})
            finally:
                secretscan.DEFAULT_REPLACEMENTS_PATH = old
                secretscan.clear_replacements_cache()


class TestCorpusIntegration(unittest.TestCase):
    def test_corpus_hit_signature(self):
        # 全量签名等值比较（修复项 H）：命中消失 + 误报新增都会被立刻抓到
        lines = corpus_lines()
        hits = secretscan.scan_text("\n".join(lines), origin="corpus",
                                    words=WORDS, identity_words=IDENTITY_WORDS)
        actual = sorted((h.line, h.rule, h.column, len(h.display)) for h in hits)
        self.assertEqual(actual, list(EXPECTED_HITS))

    def test_corpus_negative_lines_clean(self):
        lines = corpus_lines()
        for idx in NEGATIVE_LINE_INDICES:
            hits = secretscan.scan_text(lines[idx - 1], origin="corpus",
                                        words=WORDS, identity_words=IDENTITY_WORDS)
            self.assertEqual(hits, [], "line {0}: {1}".format(idx, lines[idx - 1]))


if __name__ == "__main__":
    unittest.main()
