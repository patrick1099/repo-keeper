"""共享敏感词规则：发布闸与 push 闸共用的纯规则模块（从 test_no_secrets.py 抽取）。

本模块只含规则与纯函数：不读工作区、不跑 git、import 无副作用。
词表与替换表只在运行时从 ~/.repo-keeper/ 下读取，绝不写进仓库。
Hit.display 只存掩码后的值，原始命中值不进入 Hit 的 repr / 序列化 / 异常信息。

匹配语义（务必与旧实现保持一致）：
  - 私人词表 words      ：朴素子串匹配（needle in line.lower()）
  - 本机身份词 identity_words：词边界匹配（contains_identity）
历史扫描那条把两者混在一起的历史行为保留在 test_no_secrets.py 里，本模块不管。
"""

from __future__ import annotations

import getpass
import os
import re
import socket
from dataclasses import dataclass
from pathlib import Path

from toolname import GLOBAL_DIR_NAME  # ".repo-keeper"

DEFAULT_WORDS_PATH = Path.home() / GLOBAL_DIR_NAME / "audit-words.txt"
DEFAULT_REPLACEMENTS_PATH = Path.home() / GLOBAL_DIR_NAME / "gate-replacements.txt"

SEVERITY_BLOCK = "block"
SEVERITY_WARN = "warn"


@dataclass(frozen=True)
class Hit:
    origin: str        # "blob:<path>" / "path" / "commit:<sha>:author" / "ref" / "tag:<name>"
    line: int
    column: int
    rule: str
    severity: str      # "block" | "warn"
    display: str       # 掩码后的值。原始命中值绝不进入 repr / JSON / 异常信息
    suggestion: str


def mask(value: str) -> str:
    """长度 <= 6 的整体打星；更长的保留首尾字符、中间打星。"""
    if len(value) <= 6:
        return "*" * len(value)
    return value[0] + "*" * (len(value) - 2) + value[-1]


def local_identity_words() -> list[str]:
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


def identity_match(line: str, word: str):
    """词边界匹配，返回 Match 对象；无命中返回 None。"""
    pattern = r"(?<![A-Za-z0-9]){0}(?![A-Za-z0-9])".format(re.escape(word))
    return re.search(pattern, line, re.IGNORECASE)


def contains_identity(line: str, word: str) -> bool:
    """词边界匹配，忽略大小写。"""
    return identity_match(line, word) is not None


def is_placeholder_secret(value: str) -> bool:
    lowered = value.lower()
    return (any(mark in value for mark in ("<", ">", "${", "{{", "***"))
            or any(word in lowered for word in
                   ("example", "placeholder", "dummy", "changeme", "redacted", "xxx")))


def load_words(path=None) -> list[str] | None:
    """读词表，每行一条，# 为注释。文件不存在返回 None（调用方据此 fail-closed）。"""
    words_file = Path(path) if path is not None else DEFAULT_WORDS_PATH
    if not words_file.is_file():
        return None
    words = []
    for line in words_file.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            words.append(line)
    return words


_REPLACEMENTS_CACHE: dict[str, dict[str, str]] = {}


def clear_replacements_cache() -> None:
    """清空替换表缓存（测试用）。"""
    _REPLACEMENTS_CACHE.clear()


def load_replacements(path=None) -> dict[str, str]:
    """读替换建议表，格式 敏感词 => 建议值。文件不存在返回空 dict。

    缓存是进程生命周期内的快照：同一路径只查一次文件系统，之后直接返回副本。
    push 闸是短命进程，无副作用。

    文件不存在也要入缓存——那才是热路径：替换表默认就不存在，
    而 scan_name 在 push 闸里是逐路径、逐 ref 调的。
    键用未解析的路径串：同一文件的两种写法各占一条缓存，结果都对，
    但省掉 resolve() 那次文件系统往返（本机实测 1.07 ms/次）。
    """
    repl_file = Path(path) if path is not None else DEFAULT_REPLACEMENTS_PATH
    key = str(repl_file)
    if key in _REPLACEMENTS_CACHE:
        return dict(_REPLACEMENTS_CACHE[key])
    if not repl_file.is_file():
        _REPLACEMENTS_CACHE[key] = {}
        return {}
    replacements = {}
    for line in repl_file.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        if "=>" in line:
            k, value = line.split("=>", 1)
            replacements[k.strip()] = value.strip()
    _REPLACEMENTS_CACHE[key] = dict(replacements)
    return replacements


# ---------------------------------------------------------------------------
# 结构规则（自 test_no_secrets.py 原样搬来，含 D8 三处白名单修正与 D9 severity）
# ---------------------------------------------------------------------------

# 家目录带真实账号名：本地盘 / UNC / POSIX 三种形状。
# 捕获组要求至少含一个字母且长度 >= 2，避免退化匹配到 "." / ".." / 纯数字（D8-2）。
HOME_DIR = re.compile(
    r"(?:(?:[A-Za-z]:|\\\\[^\\/\s]+(?:[\\/]+[^\\/\s]+)?)[\\/]+"
    r"(?:Users|Documents and Settings)[\\/]+|/home/|/Users/)"
    r"([A-Za-z0-9._-]*[A-Za-z][A-Za-z0-9._-]+|[A-Za-z0-9._-]+[A-Za-z][A-Za-z0-9._-]*)")
#: 例子里的占位用户，不是泄漏（D8-1 扩容）。
HOME_OK = {"<user>", "you", "youruser", "username", "user", "me", "someone",
           "bob", "alice", "carol", "dave", "example", "xxx", "dev"}

# 完整的对象 id 把读者钉死在某个仓库的历史上。
FULL_HASH = re.compile(r"\b(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})\b")

# 笔记双链只在作者私人库里能解析；公开仓里既是死链又是私人笔记目录。
# 内部必须有字母，所以引用标记 / 数组下标不会冒充笔记链接。
# 正则串拆成三段拼，避免本文件源码自身被 WIKI_LINK 规则命中。
WIKI_LINK = re.compile(
    r"\[\[" + r"(?!:[A-Za-z]+:\]\])" + r"(?=[^\]\n]*[A-Za-z])[^\]\n]+" + r"\]\]")

EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
# 整地址判定：域名必须完整相等，不是后缀包含（修复项 A 取代 D8-3 的整域白名单）。
EMAIL_OK_DOMAIN = re.compile(
    r"(?:example\.(?:com|org|net|invalid)|invalid|localhost"
    r"|users\.noreply\.github\.com|anthropic\.com)\Z", re.IGNORECASE)
EMAIL_OK_ADDRESS = frozenset({"git@github.com", "noreply@github.com"})


def email_is_ok(value: str) -> bool:
    """整地址判定：域名必须完整相等，不是后缀包含。"""
    lowered = value.lower()
    if lowered in EMAIL_OK_ADDRESS:
        return True
    if "@" not in lowered:
        return False
    domain = lowered.rsplit("@", 1)[1]
    return EMAIL_OK_DOMAIN.fullmatch(domain) is not None

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


def credential_matches(line):
    """凭据规则逐条扫描，产出 (rule, start, value)。唯一实现。"""
    for pattern in (PRIVATE_KEY, CREDENTIAL_URL):
        for match in pattern.finditer(line):
            rule = "PRIVATE_KEY" if pattern is PRIVATE_KEY else "CREDENTIAL_URL"
            yield rule, match.start(), match.group(0)
    # D8-4：TOKEN 也过 is_placeholder_secret，占位 token 不再当真。
    for match in TOKEN.finditer(line):
        if not is_placeholder_secret(match.group(0)):
            yield "TOKEN", match.start(), match.group(0)
    for match in SECRET_ASSIGNMENT.finditer(line):
        if not is_placeholder_secret(match.group(2)):
            yield "SECRET_ASSIGNMENT", match.start(), match.group(0)


def credential_hits(line):
    """只要命中值的薄封装（发布闸的 per-rule 判定用）。"""
    for _rule, _start, value in credential_matches(line):
        yield value


def _hit(origin, line, column, rule, severity, value, replacements):
    return Hit(origin=origin, line=line, column=column, rule=rule,
               severity=severity, display=mask(value),
               suggestion=replacements.get(value, ""))


def _scan_line(line, *, origin, line_no, words, identity_words, replacements):
    found = []
    for match in HOME_DIR.finditer(line):
        if match.group(1).lower() not in HOME_OK:
            found.append(_hit(origin, line_no, match.start(), "HOME_DIR",
                              SEVERITY_BLOCK, match.group(0), replacements))
    for match in FULL_HASH.finditer(line):
        found.append(_hit(origin, line_no, match.start(), "FULL_HASH",
                          SEVERITY_WARN, match.group(0), replacements))
    for match in WIKI_LINK.finditer(line):
        found.append(_hit(origin, line_no, match.start(), "WIKI_LINK",
                          SEVERITY_WARN, match.group(0), replacements))
    for match in EMAIL.finditer(line):
        if not email_is_ok(match.group(0)):
            found.append(_hit(origin, line_no, match.start(), "EMAIL",
                              SEVERITY_BLOCK, match.group(0), replacements))
    for match in KEIL_USER_FILE.finditer(line):
        if match.group(1).lower() not in KEIL_USER_OK:
            found.append(_hit(origin, line_no, match.start(), "KEIL_USER_FILE",
                              SEVERITY_BLOCK, match.group(0), replacements))
    for match in SID.finditer(line):
        found.append(_hit(origin, line_no, match.start(), "SID",
                          SEVERITY_BLOCK, match.group(0), replacements))
    for rule, start, value in credential_matches(line):
        found.append(_hit(origin, line_no, start, rule,
                          SEVERITY_BLOCK, value, replacements))
    for word in words:
        if word.lower() in line.lower():
            found.append(_hit(origin, line_no, line.lower().find(word.lower()), "WORD",
                              SEVERITY_BLOCK, word, replacements))
    for word in identity_words:
        match = identity_match(line, word)
        if match is not None:
            found.append(_hit(origin, line_no, match.start(), "IDENTITY",
                              SEVERITY_BLOCK, word, replacements))
    return found


def scan_text(text, *, origin, words, identity_words) -> list[Hit]:
    """按行扫描一段文本，返回全部命中。"""
    replacements = load_replacements()
    hits = []
    for lineno, line in enumerate(text.splitlines(), 1):
        hits.extend(_scan_line(line, origin=origin, line_no=lineno,
                               words=words or [], identity_words=identity_words or [],
                               replacements=replacements))
    return hits


def scan_name(name, *, origin, words, identity_words) -> list[Hit]:
    """扫描 ref 名 / 路径名，无行号语义，line 固定为 0。"""
    replacements = load_replacements()
    return _scan_line(name, origin=origin, line_no=0,
                      words=words or [], identity_words=identity_words or [],
                      replacements=replacements)
