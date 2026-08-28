"""Refuse to ship a repo that carries someone's private strings.

This plugin was extracted from a working setup in a company repo. That
extraction is exactly the kind of job where one forgotten branch name or
absolute path rides along into a public repo, and nothing about the commit
looks wrong. So the constraint is a test that exits non-zero, not a paragraph
in CONTRIBUTING.

Two halves, because they have opposite publication rules:

  * **Structural and local-identity rules, in this file.** Things that are private by *shape*
    rather than by content: a home directory with a real username in it, a
    Keil user-state filename, a SID, a credential, a full git hash, a
    ``[[wiki-link]]`` into someone's private notes, or an email address. The
    current account and device names are also swept without publishing them.

  * **A personal word list, deliberately NOT in this repo.** Branch names,
    employer, colleagues' handles, product codenames. Writing that list into
    the file that checks for it would publish the very strings it exists to
    keep out -- the guard would become the leak. It is read from
    ``~/<tool>/audit-words.txt``, one entry per line, ``#`` for comments.
    Absent on a fresh clone -> that half is skipped and says so.
"""
import getpass
import os
import re
import socket
import subprocess
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
PUBLIC_REF_PREFIXES = ("refs/heads/", "refs/tags/", "refs/remotes/origin/")


def local_identity_words():
    values = {
        getpass.getuser(),
        socket.gethostname(),
        Path.home().name,
        os.environ.get("USERNAME", ""),
        os.environ.get("COMPUTERNAME", ""),
    }
    generic = {"", "user", "username", "developer", "dev", "runner", "localhost"}
    return sorted(value for value in values
                  if len(value) >= 3 and value.lower() not in generic)


LOCAL_IDENTITY_WORDS = local_identity_words()


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


def git_text(*args):
    result = subprocess.run(
        ["git", *args], cwd=str(REPO), stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        return None
    return result.stdout


def git_bytes(*args, input_data=None):
    result = subprocess.run(
        ["git", *args], cwd=str(REPO), input=input_data,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode != 0:
        return None
    return result.stdout


def contains_identity(line, word):
    pattern = r"(?<![A-Za-z0-9]){0}(?![A-Za-z0-9])".format(re.escape(word))
    return re.search(pattern, line, re.IGNORECASE) is not None


def is_placeholder_secret(value):
    lowered = value.lower()
    return (any(mark in value for mark in ("<", ">", "${", "{{", "***"))
            or any(word in lowered for word in
                   ("example", "placeholder", "dummy", "changeme", "redacted", "xxx")))


def rel(path):
    return path.relative_to(REPO).as_posix()


# ---------------------------------------------------------------------------
# structural rules
# ---------------------------------------------------------------------------

# A home directory carries a real account name. Matches local and UNC Windows
# profiles plus POSIX homes.
HOME_DIR = re.compile(
    r"(?:(?:[A-Za-z]:|\\\\[^\\/\s]+(?:[\\/]+[^\\/\s]+)?)[\\/]+"
    r"(?:Users|Documents and Settings)[\\/]+|/home/|/Users/)"
    r"([A-Za-z0-9._-]+)")
#: Placeholders that are the point of the example, not a leak.
HOME_OK = {"<user>", "you", "youruser", "username", "user", "me", "someone"}

# A full object id pins the reader to one specific repository's history.
FULL_HASH = re.compile(r"\b(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})\b")

# [[note-name]] links resolve only inside the author's private vault; in a
# public repo they are both dead links and a table of contents of private notes.
# Requires a letter inside, so `[[6]]`-style citation markers and array
# indexing do not masquerade as note links.
WIKI_LINK = re.compile(r"\[\[(?=[^\]\n]*[A-Za-z])[^\]\n]+\]\]")

EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
EMAIL_OK = re.compile(r"@(example\.(com|org|net)|invalid|localhost"
                      r"|users\.noreply\.github\.com|anthropic\.com)\b")

KEIL_USER_FILE = re.compile(r"\.uvguix\.([A-Za-z0-9._-]+)\b", re.IGNORECASE)
KEIL_USER_OK = {"dev", "developer", "user", "username", "test", "sample"}

SID = re.compile(r"\bS-1-5-(?:21-)?\d+(?:-\d+){2,}\b", re.IGNORECASE)
PRIVATE_KEY = re.compile(
    r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----")
CREDENTIAL_URL = re.compile(
    r"\b[a-z][a-z0-9+.-]*://[^\s/@:]+:[^\s/@]+@", re.IGNORECASE)
TOKEN = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
    r"(?:AKIA|ASIA)[A-Z0-9]{16}|AIza[0-9A-Za-z_-]{30,}|"
    r"xox[baprs]-[A-Za-z0-9-]{20,}|sk-[A-Za-z0-9_-]{20,})\b")
SECRET_ASSIGNMENT = re.compile(
    r"\b(api[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret|"
    r"password|passwd|pwd)\b\s*[:=]\s*[\"']?([^\s\"';,#]{8,})",
    re.IGNORECASE)


def credential_hits(line):
    for pattern in (PRIVATE_KEY, CREDENTIAL_URL, TOKEN):
        for match in pattern.finditer(line):
            yield match.group(0)
    for match in SECRET_ASSIGNMENT.finditer(line):
        if not is_placeholder_secret(match.group(2)):
            yield match.group(0)


def is_skipped_history_path(path):
    return path.replace("\\", "/").rsplit("/", 1)[-1] == SELF


def history_blob_records(refs):
    if not refs:
        return []
    listing = git_text(
        "-c", "core.quotePath=false", "rev-list", "--objects", *refs)
    if listing is None:
        return None

    objects = []
    for line in listing.splitlines():
        fields = line.split(" ", 1)
        if len(fields) == 1:
            objects.append((fields[0], ""))
        else:
            objects.append((fields[0], fields[1]))
    if not objects:
        return []

    request = ("".join(oid + "\n" for oid, _ in objects)).encode("ascii")
    payload = git_bytes("cat-file", "--batch", input_data=request)
    if payload is None:
        return None

    records = []
    offset = 0
    for oid, path in objects:
        header_end = payload.find(b"\n", offset)
        if header_end < 0:
            break
        header = payload[offset:header_end].split()
        offset = header_end + 1
        if len(header) < 3:
            break
        kind = header[1]
        try:
            size = int(header[2])
        except ValueError:
            break
        data = payload[offset:offset + size]
        if len(data) != size:
            break
        offset += size
        if offset < len(payload) and payload[offset:offset + 1] == b"\n":
            offset += 1
        if kind == b"blob" and not is_skipped_history_path(path):
            records.append((path or oid, data.decode("utf-8", errors="replace")))
    return records


def history_path_names(refs):
    if not refs:
        return []
    listing = git_text(
        "-c", "core.quotePath=false", "log", *refs, "--name-only", "--format=")
    if listing is None:
        return None
    return [line for line in listing.splitlines()
            if line and not is_skipped_history_path(line)]


def history_line_hits(line, words):
    hits = []
    for match in HOME_DIR.finditer(line):
        if match.group(1).lower() not in HOME_OK:
            hits.append(match.group(0))
    hits.extend(FULL_HASH.findall(line))
    hits.extend(WIKI_LINK.findall(line))
    for match in EMAIL.finditer(line):
        if not EMAIL_OK.search(match.group(0)):
            hits.append(match.group(0))
    for match in KEIL_USER_FILE.finditer(line):
        if match.group(1).lower() not in KEIL_USER_OK:
            hits.append(match.group(0))
    hits.extend(SID.findall(line))
    hits.extend(credential_hits(line))
    for word in words:
        if contains_identity(line, word) or word.lower() in line.lower():
            hits.append(word)
    return hits


def history_leak_hits():
    words = list(LOCAL_IDENTITY_WORDS)
    personal = load_words()
    if personal:
        words.extend(personal)

    ref_text = git_text(
        "for-each-ref", "--format=%(refname)", *PUBLIC_REF_PREFIXES)
    if ref_text is None:
        return None
    refs = [line for line in ref_text.splitlines() if line]
    if not refs:
        return []
    log = git_text(
        "log", *refs,
        "--format=%H%x1f%an%x1f%ae%x1f%cn%x1f%ce%x1f%B%x1e")
    paths = history_path_names(refs)
    blobs = history_blob_records(refs)
    if log is None or paths is None or blobs is None:
        return None

    hits = []
    for ref in refs:
        for bad in history_line_hits(ref, words):
            hits.append("ref:{0}: {1}".format(ref, bad))

    for path in paths:
        for lineno, line in enumerate(path.splitlines(), 1):
            for bad in history_line_hits(line, words):
                hits.append("path:{0}:{1}: {2}".format(path, lineno, bad))

    for path, text in blobs:
        for lineno, line in enumerate(text.splitlines(), 1):
            for bad in history_line_hits(line, words):
                hits.append("blob:{0}:{1}: {2}".format(path, lineno, bad))

    for record in log.split("\x1e"):
        fields = record.strip().split("\x1f")
        if len(fields) < 6:
            continue
        commit = fields[0][:12]
        for lineno, line in enumerate(" ".join(fields[1:]).splitlines(), 1):
            for bad in history_line_hits(line, words):
                hits.append("commit:{0}:{1}: {2}".format(commit, lineno, bad))
    return hits


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

    def test_no_keil_user_state_identifiers(self):
        def check(line):
            for match in KEIL_USER_FILE.finditer(line):
                if match.group(1).lower() not in KEIL_USER_OK:
                    yield match.group(0)
        hits = self._sweep(check)
        self.assertEqual(hits, [], "\nKeil 用户状态文件带有真实账号名:\n  "
                                   + "\n  ".join(hits))

    def test_no_local_account_or_device_names(self):
        def check(line):
            for word in LOCAL_IDENTITY_WORDS:
                if contains_identity(line, word):
                    yield word
        hits = self._sweep(check)
        self.assertEqual(hits, [], "\n本机账号名或设备名出现在仓库里:\n  "
                                   + "\n  ".join(hits))

    def test_no_windows_sids(self):
        hits = self._sweep(lambda line: SID.findall(line))
        self.assertEqual(hits, [], "\nWindows SID:\n  " + "\n  ".join(hits))

    def test_no_credentials(self):
        hits = self._sweep(credential_hits)
        self.assertEqual(hits, [], "\n疑似凭据或私钥:\n  " + "\n  ".join(hits))

    def test_git_refs_and_commit_metadata(self):
        hits = history_leak_hits()
        if hits is None:
            self.skipTest("当前目录没有可扫描的 Git 元数据")
        self.assertEqual(hits, [], "\nGit refs 或提交元数据含私人信息:\n  "
                                   + "\n  ".join(hits))


class TestPatternCoverage(unittest.TestCase):
    def test_home_directory_variants(self):
        samples = (r"C:\Users\alice\repo", r"\\host\C$\Users\alice\repo",
                   r"\\host\profiles\Users\alice\repo", "/home/alice/repo",
                   "/Users/alice/repo")
        for sample in samples:
            self.assertIsNotNone(HOME_DIR.search(sample), sample)

    def test_credential_shapes(self):
        samples = ("-----BEGIN PRIVATE KEY-----",
                   "https://alice:real-password@example.com/repo",
                   "password=real-secret-value")
        for sample in samples:
            self.assertTrue(list(credential_hits(sample)), sample)

    def test_placeholder_credentials_are_allowed(self):
        self.assertEqual(list(credential_hits("password=<password>")), [])
        self.assertEqual(list(credential_hits("api_key=example-key")), [])


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
