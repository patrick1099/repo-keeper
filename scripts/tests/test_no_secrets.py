"""Refuse to ship a repo that carries someone's private strings.

This plugin was extracted from a working setup in a company repo. That
extraction is exactly the kind of job where one forgotten branch name or
absolute path rides along into a public repo, and nothing about the commit
looks wrong. So the constraint is a test that exits non-zero, not a paragraph
in CONTRIBUTING.

Two halves, because they have opposite publication rules:

  * **Structural rules, in this file.** Things that are private by *shape*
    rather than by content: a home directory with a real username in it, a
    full git hash, a ``[[wiki-link]]`` into someone's private notes, an email
    address. These are safe to write down here because the pattern gives
    nothing away.

  * **A personal word list, deliberately NOT in this repo.** Branch names,
    employer, colleagues' handles, product codenames. Writing that list into
    the file that checks for it would publish the very strings it exists to
    keep out -- the guard would become the leak. It is read from
    ``~/<tool>/audit-words.txt``, one entry per line, ``#`` for comments.
    Absent on a fresh clone -> that half is skipped and says so.
"""
import os
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from toolname import GLOBAL_DIR_NAME  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
WORDS_FILE = Path.home() / GLOBAL_DIR_NAME / "audit-words.txt"

SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", "dist", "build",
             ".cache", "node_modules"}
SKIP_SUFFIXES = {".exe", ".png", ".jpg", ".gif", ".ico", ".zip", ".pyc"}

#: This file necessarily contains the patterns it looks for.
SELF = Path(__file__).name


def iter_text_files():
    for path in REPO.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.suffix.lower() in SKIP_SUFFIXES:
            continue
        if path.name == SELF:
            continue
        try:
            yield path, path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue        # binary or unreadable -- nothing to leak in text


def rel(path):
    return path.relative_to(REPO).as_posix()


# ---------------------------------------------------------------------------
# structural rules
# ---------------------------------------------------------------------------

# A home directory carries a real account name. Matches Windows and POSIX.
HOME_DIR = re.compile(
    r"(?:[A-Za-z]:[\\/]+Users[\\/]+|/home/|/Users/)([A-Za-z0-9._-]+)")
#: Placeholders that are the point of the example, not a leak.
HOME_OK = {"<user>", "you", "youruser", "username", "user", "me", "someone"}

# A full object id pins the reader to one specific repository's history.
FULL_HASH = re.compile(r"\b[0-9a-f]{40}\b")

# [[note-name]] links resolve only inside the author's private vault; in a
# public repo they are both dead links and a table of contents of private notes.
# Requires a letter inside, so `[[6]]`-style citation markers and array
# indexing do not masquerade as note links.
WIKI_LINK = re.compile(r"\[\[(?=[^\]\n]*[A-Za-z])[^\]\n]+\]\]")

EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
EMAIL_OK = re.compile(r"@(example\.(com|org|net)|invalid|localhost"
                      r"|users\.noreply\.github\.com|anthropic\.com)\b")


class TestStructuralLeaks(unittest.TestCase):
    def _sweep(self, check):
        hits = []
        for path, text in iter_text_files():
            for lineno, line in enumerate(text.splitlines(), 1):
                for bad in check(line):
                    hits.append("{0}:{1}: {2}".format(rel(path), lineno, bad))
        return hits

    def test_no_real_home_directory(self):
        def check(line):
            for m in HOME_DIR.finditer(line):
                if m.group(1).lower() not in HOME_OK:
                    yield m.group(0)
        hits = self._sweep(check)
        self.assertEqual(hits, [], "\n绝对家目录里有真实账号名:\n  "
                                   + "\n  ".join(hits))

    def test_no_full_git_hashes(self):
        hits = self._sweep(lambda ln: FULL_HASH.findall(ln))
        self.assertEqual(hits, [], "\n完整 git hash 指向某一个具体仓库的历史:\n  "
                                   + "\n  ".join(hits))

    def test_no_wiki_links_into_private_notes(self):
        hits = self._sweep(lambda ln: WIKI_LINK.findall(ln))
        self.assertEqual(hits, [], "\n[[...]] 链接只在作者的私人库里解析得开:\n  "
                                   + "\n  ".join(hits))

    def test_no_personal_email_addresses(self):
        def check(line):
            for m in EMAIL.finditer(line):
                if not EMAIL_OK.search(m.group(0)):
                    yield m.group(0)
        hits = self._sweep(check)
        self.assertEqual(hits, [], "\n真实邮箱地址:\n  " + "\n  ".join(hits))


# ---------------------------------------------------------------------------
# personal word list (kept outside this repo on purpose)
# ---------------------------------------------------------------------------

def load_words():
    if not WORDS_FILE.is_file():
        return None
    words = []
    for line in WORDS_FILE.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            words.append(line)
    return words


class TestPersonalWordList(unittest.TestCase):
    def test_no_listed_word_appears_anywhere(self):
        words = load_words()
        if words is None:
            self.skipTest(
                "没有 {0} —— 结构规则照跑,但认不出分支名/雇主/代号这类"
                "只有你知道是敏感的词。发布前请在作者本机跑一遍。".format(WORDS_FILE))
        self.assertTrue(words, "{0} 是空的".format(WORDS_FILE))

        lowered = [(w, w.lower()) for w in words]
        hits = []
        for path, text in iter_text_files():
            for lineno, line in enumerate(text.splitlines(), 1):
                low = line.lower()
                for original, needle in lowered:
                    if needle in low:
                        hits.append("{0}:{1}: {2}".format(
                            rel(path), lineno, original))
        self.assertEqual(hits, [],
                         "\n{0} 里列的词出现在仓库里:\n  ".format(WORDS_FILE.name)
                         + "\n  ".join(hits))


if __name__ == "__main__":
    unittest.main()
