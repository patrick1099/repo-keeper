#!/usr/bin/env python3
"""敏感词发布闸的扫描核心（第二阶段第 1 批，纯 python，不装钩子）。

只做两件事：从 git 协议输入算出"本次要外发"的对象范围，然后对范围内的
blob 内容 / 路径名 / 提交身份与 message / ref 名 / annotated tag 元数据
跑 secretscan 规则。规则模块在 scripts/secretscan.py，本文件只调用它，
不改任何规则。

架构要点（对应 plan-secret-gate.md §D4 / §D5 / §D9 / §9.1）：
  * pre-push 从 stdin 读 git 协议行（<local ref> <local sha> <remote ref> <remote sha>），
    逐行算范围后按对象 OID 去重再扫。
  * 范围用 `git rev-list --objects <local> --not <remote>`（新 ref 省掉 --not 那段），
    绝不用 `git rev-list <local> --not --remotes`——那会把"已在另一个 remote 可达"
    的敏感提交排除掉，造成扫到 0 个对象却放行。
  * 三分法：stdin 没行 -> exit 0；全是删除 -> exit 0；有非删除行但对象集合为空 -> 拒绝。
  * fail-closed：任何"没真正完成检查"的情况（词表缺失/为空/读失败、git 子命令失败、
    输出解析失败、范围算空、--repo 不是 git 仓）都退非零 + E_GATE_UNAVAILABLE。
  * WIKI_LINK / FULL_HASH 是 warn，不拦；其余是 block，有一条就退 1。
    两种非零用信封的 error.code 区分：E_LEAK_FOUND 与 E_GATE_UNAVAILABLE。
  * display 存的是掩码值，原始命中值绝不进信封 / 日志 / 异常信息。

本批不写 hooks/pre-push、不写 install-hooks、不写 arm / sync-exempt——那些是第 2、3 批。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cli_common as cc  # noqa: E402

PROG = "GateCheck"

#: 本闸自己的错误码（cli_common 的 ERROR_CODES 是共享规范表，这里单独命名）。
E_LEAK_FOUND = "E_LEAK_FOUND"
E_GATE_UNAVAILABLE = "E_GATE_UNAVAILABLE"
E_ARG = "E_ARG"

_HEX_SHA = re.compile(r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$")


def _is_valid_sha(sha):
    """合法 sha：40 位或 64 位十六进制。git 协议行给的是完整 sha。"""
    return bool(_HEX_SHA.match(sha))


def _is_zero_sha(sha):
    """全 0 的 40/64 位 sha 表示"该 ref 远端不存在 / 本地不存在"。"""
    return len(sha) in (40, 64) and set(sha) == {"0"}


def _git(repo, args):
    """跑 git 子命令，非零退出即 fail-closed（E_GATE_UNAVAILABLE）。"""
    proc = subprocess.run(["git", "-C", str(repo), *args],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        tail = proc.stderr.decode("utf-8", "replace").strip()
        raise cc.CliError(
            E_GATE_UNAVAILABLE,
            "git 命令失败: git {0}".format(" ".join(args)),
            details={"exit_code": proc.returncode, "stderr_tail": tail[-2000:]})
    return proc.stdout.decode("utf-8", "surrogateescape")


def _git_bytes(repo, args, input_data):
    """带 stdin 的 git 子命令（cat-file --batch 用），非零退出即 fail-closed。"""
    proc = subprocess.run(["git", "-C", str(repo), *args], input=input_data,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        tail = proc.stderr.decode("utf-8", "replace").strip()
        raise cc.CliError(
            E_GATE_UNAVAILABLE,
            "git 命令失败: git {0}".format(" ".join(args)),
            details={"exit_code": proc.returncode, "stderr_tail": tail[-2000:]})
    return proc.stdout


def _check_repo(repo):
    """--repo 必须是 git 仓库，否则 fail-closed。"""
    out = _git(repo, ["rev-parse", "--git-dir"])
    if not out.strip():
        raise cc.CliError(E_GATE_UNAVAILABLE, "--repo 不是 git 仓库: {0}".format(repo),
                          details={"repo": str(repo)})


def _require_secretscan():
    """懒加载规则模块。import 失败即 fail-closed（E_GATE_UNAVAILABLE）。"""
    try:
        import secretscan as sc
    except Exception as exc:
        raise cc.CliError(
            E_GATE_UNAVAILABLE, "无法导入 secretscan 规则模块",
            details={"exc": type(exc).__name__}) from exc
    return sc


def _parse_protocol_lines(lines):
    """把 pre-push 的 stdin 协议行拆成扫描范围。

    返回 (ranges, ref_names, has_delete, has_non_delete)：
      ranges       -> [(local_sha, remote_sha_or_None)]，None 表示新 ref 扫完整历史
      ref_names    -> 所有行的本地/远端 ref 名（删除行也收，反正是外发的名字）
      has_delete   -> 至少有一行是删除（local sha 全 0）
      has_non_delete -> 至少有一行不是删除
    """
    ranges = []
    ref_names = []
    has_delete = False
    has_non_delete = False
    for line in lines:
        line = line.strip()
        if not line:
            continue
        fields = line.split()
        if len(fields) != 4:
            raise cc.CliError(E_GATE_UNAVAILABLE, "pre-push 协议行格式错误")
        local_ref, local_sha, remote_ref, remote_sha = fields
        if not _is_valid_sha(local_sha) or not _is_valid_sha(remote_sha):
            raise cc.CliError(E_GATE_UNAVAILABLE, "pre-push 协议行 sha 非法")
        ref_names.append(local_ref)
        ref_names.append(remote_ref)
        if _is_zero_sha(local_sha):
            has_delete = True
            continue
        has_non_delete = True
        if _is_zero_sha(remote_sha):
            ranges.append((local_sha, None))
        else:
            ranges.append((local_sha, remote_sha))
    return ranges, ref_names, has_delete, has_non_delete


def _collect_objects(repo, ranges):
    """对每个范围跑 rev-list --objects，产出 {oid: set(paths)}。

    新 ref（exclude 为 None）省掉 --not 那段，扫完整可达历史。
    """
    objects = {}
    for local, exclude in ranges:
        args = ["rev-list", "--objects", local]
        if exclude is not None:
            args += ["--not", exclude]
        out = _git(repo, args)
        for line in out.splitlines():
            fields = line.split(" ", 1)
            oid = fields[0]
            if not _is_valid_sha(oid):
                raise cc.CliError(E_GATE_UNAVAILABLE, "rev-list 输出解析失败")
            path = fields[1] if len(fields) > 1 else ""
            objects.setdefault(oid, set()).add(path)
    return objects


def _log_paths(repo, ranges):
    """从 git log --name-only 补路径。

    rev-list --objects 不含 gitlink 的路径，也不含"内容没变只改了路径"的 rename
    新路径——这两类恰恰是路径泄漏的要紧处。git log --name-only 都能给到，
    所以路径扫描集合取两者的并集。--name-only 对增量范围只列本批改动的路径，
    不会把远端已有的路径拉进来造成误报。
    """
    paths = set()
    for local, exclude in ranges:
        args = ["log", "-z"]
        if exclude is not None:
            args.append(exclude + ".." + local)
        else:
            args.append(local)
        args += ["--name-only", "--format="]
        out = _git(repo, args)
        for name in out.split("\x00"):
            if name.strip():
                paths.add(name)
    return paths


def _read_objects(repo, oids):
    """git cat-file --batch 读对象，返回 {oid: (type, bytes)}。

    gitlink 指向 submodule 的 commit oid 在本仓不存在，会返回 "missing"，
    记成 ("missing", b"") 跳过——正好满足"gitlink 不把 OID 当内容扫"。
    输出解析失败一律 fail-closed。
    """
    if not oids:
        return {}
    request = "".join(oid + "\n" for oid in sorted(oids)).encode("ascii")
    payload = _git_bytes(repo, ["cat-file", "--batch"], request)
    results = {}
    offset = 0
    while offset < len(payload):
        header_end = payload.find(b"\n", offset)
        if header_end < 0:
            raise cc.CliError(E_GATE_UNAVAILABLE, "cat-file 输出解析失败")
        header = payload[offset:header_end].split()
        offset = header_end + 1
        if len(header) >= 2 and header[1] == b"missing":
            results[header[0].decode("ascii")] = ("missing", b"")
            continue
        if len(header) < 3:
            raise cc.CliError(E_GATE_UNAVAILABLE, "cat-file 输出解析失败")
        oid = header[0].decode("ascii")
        kind = header[1].decode("ascii")
        try:
            size = int(header[2])
        except ValueError:
            raise cc.CliError(E_GATE_UNAVAILABLE, "cat-file 输出解析失败")
        data = payload[offset:offset + size]
        if len(data) != size:
            raise cc.CliError(E_GATE_UNAVAILABLE, "cat-file 输出不完整")
        offset += size
        if offset < len(payload) and payload[offset:offset + 1] == b"\n":
            offset += 1
        results[oid] = (kind, data)
    return results


def _decode_blob(data):
    """逐 blob 独立解码：UTF-8 -> CP936(GB2312) -> replace 强解。

    绝不允许"解不了就当没有"——那是旧发布闸的 fail-open，本闸是 fail-closed。
    替换字符不会凭空造出要找的形状；二进制被扫只是慢一点。
    """
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        try:
            return data.decode("cp936")
        except UnicodeDecodeError:
            return data.decode("utf-8", errors="replace")


def _parse_identity(rest):
    """把 "Name <email> ts tz" 拆成 (name, email)。"""
    start = rest.rfind("<")
    end = rest.rfind(">")
    if start < 0 or end < start:
        return rest.strip(), ""
    name = rest[:start].strip()
    email = rest[start + 1:end]
    return name, email


def _split_headers_message(text):
    """对象头与 message 以第一个空行分隔。"""
    if "\n\n" in text:
        headers, message = text.split("\n\n", 1)
    else:
        headers, message = text, ""
    return headers, message.rstrip("\n")


def _parse_commit(data):
    """解析 commit 对象：author / committer 的 (name, email) 与 message。"""
    text = data.decode("utf-8", "replace")
    headers, message = _split_headers_message(text)
    author = committer = None
    for line in headers.splitlines():
        if line.startswith("author "):
            author = _parse_identity(line[len("author "):])
        elif line.startswith("committer "):
            committer = _parse_identity(line[len("committer "):])
    return author, committer, message


def _parse_tag(data):
    """解析 annotated tag 对象：tag 名、tagger 的 (name, email) 与 message。"""
    text = data.decode("utf-8", "replace")
    headers, message = _split_headers_message(text)
    tagger = None
    name = ""
    for line in headers.splitlines():
        if line.startswith("tagger "):
            tagger = _parse_identity(line[len("tagger "):])
        elif line.startswith("tag "):
            name = line[len("tag "):].strip()
    return name, tagger, message


def _scan_ref_names(ref_names, words, identity_words, sc):
    """只扫 ref 名。纯删除的 push 没有对象可扫, 但 ref 名照样外发给远端。"""
    hits = []
    for name in dict.fromkeys(ref_names):
        hits.extend(sc.scan_name(name, origin="ref", words=words,
                                 identity_words=identity_words))
    return hits


def _scan_repo(repo, ranges, ref_names, words, identity_words, sc):
    """对给定范围做全量扫描，返回 Hit 列表。

    覆盖：ref 名、路径名（--objects 路径并集 log --name-only 路径）、blob 内容、
    commit author/committer 与 message、annotated tag 的 tagger 与 message。
    """
    objects = _collect_objects(repo, ranges)
    if not objects:
        raise cc.CliError(E_GATE_UNAVAILABLE, "扫描对象集合为空，无法完成检查")
    payload = _read_objects(repo, list(objects))
    log_paths = _log_paths(repo, ranges)

    hits = []

    hits.extend(_scan_ref_names(ref_names, words, identity_words, sc))

    seen_paths = set()
    for oid, paths in objects.items():
        for path in paths:
            if path and path not in seen_paths:
                seen_paths.add(path)
                hits.extend(sc.scan_name(path, origin="path", words=words,
                                         identity_words=identity_words))
    for path in log_paths:
        if path not in seen_paths:
            seen_paths.add(path)
            hits.extend(sc.scan_name(path, origin="path", words=words,
                                     identity_words=identity_words))

    for oid, (kind, data) in payload.items():
        if kind == "blob":
            paths = sorted(objects.get(oid, {""}))
            text = _decode_blob(data)
            origin = "blob:" + (paths[0] if paths[0] else oid)
            hits.extend(sc.scan_text(text, origin=origin, words=words,
                                     identity_words=identity_words))
        elif kind == "commit":
            author, committer, message = _parse_commit(data)
            short = oid[:12]
            if author is not None:
                name, email = author
                hits.extend(sc.scan_text(
                    "{0} <{1}>".format(name, email),
                    origin="commit:{0}:author".format(short), words=words,
                    identity_words=identity_words))
            if committer is not None:
                name, email = committer
                hits.extend(sc.scan_text(
                    "{0} <{1}>".format(name, email),
                    origin="commit:{0}:committer".format(short), words=words,
                    identity_words=identity_words))
            if message:
                hits.extend(sc.scan_text(message,
                                         origin="commit:{0}:message".format(short),
                                         words=words, identity_words=identity_words))
        elif kind == "tag":
            name, tagger, message = _parse_tag(data)
            label = name or oid[:12]
            if tagger is not None:
                tname, temail = tagger
                hits.extend(sc.scan_text(
                    "{0} <{1}>".format(tname, temail),
                    origin="tag:{0}:tagger".format(label), words=words,
                    identity_words=identity_words))
            if message:
                hits.extend(sc.scan_text(message,
                                         origin="tag:{0}:message".format(label),
                                         words=words, identity_words=identity_words))
    return hits


def _hit_dict(h):
    """Hit -> 信封字典。display 已经是掩码值，原始值不进信封。"""
    return {"origin": h.origin, "line": h.line, "column": h.column,
            "rule": h.rule, "severity": h.severity,
            "display": h.display, "suggestion": h.suggestion}


def _hit_payload(hits):
    return {"hits": [_hit_dict(h) for h in hits],
            "block_count": sum(1 for h in hits if h.severity == "block"),
            "warn_count": sum(1 for h in hits if h.severity == "warn")}


def _load_words(sc, words_file):
    """取词表。缺失 / 为空 / 读失败 / 解密失败都 fail-closed。"""
    if words_file is None:
        path = sc.DEFAULT_WORDS_PATH
        try:
            words = sc.load_words()
        except Exception as exc:
            raise cc.CliError(
                E_GATE_UNAVAILABLE, "词表读取失败: {0}".format(path),
                details={"words_file": str(path),
                         "exc": type(exc).__name__}) from exc
    else:
        path = Path(words_file)
        try:
            words = sc.load_words(str(path))
        except Exception as exc:
            raise cc.CliError(
                E_GATE_UNAVAILABLE, "词表读取失败: {0}".format(path),
                details={"words_file": str(path),
                         "exc": type(exc).__name__}) from exc
    if not words:
        raise cc.CliError(E_GATE_UNAVAILABLE, "词表缺失或为空: {0}".format(path),
                          details={"words_file": str(path)})
    return words


def _parse_identity_words(value, sc):
    """--identity-words 覆盖 secretscan.local_identity_words()；不给则用本机真实值。"""
    if not value:
        return sc.local_identity_words()
    return [w.strip() for w in value.split(",") if w.strip()]


def _with_replacements(sc, replacements_file, scan):
    """--replacements-file 只改取数来源；不给时 suggestion 为空但不拦。"""
    if replacements_file is None:
        return scan()
    old = sc.DEFAULT_REPLACEMENTS_PATH
    sc.DEFAULT_REPLACEMENTS_PATH = Path(replacements_file)
    sc.clear_replacements_cache()
    try:
        return scan()
    finally:
        sc.DEFAULT_REPLACEMENTS_PATH = old
        sc.clear_replacements_cache()


def _hit_line(h):
    loc = "{0}:{1}:{2}".format(h.origin, h.line, h.column)
    line = "  {0}  [{1}]  {2}".format(loc, h.rule, h.display)
    if h.suggestion:
        line += "  =>  {0}".format(h.suggestion)
    return line


def _emit_human_report(context, block_hits, warn_hits):
    """human 模式把命中清单打到 stderr；json 模式靠信封，不混文本。"""
    if context.json_mode:
        return
    w = context.sinks.err
    w.write("敏感词发布闸: 检测到 {0} 处 block 命中, {1} 处 warn。\n".format(
        len(block_hits), len(warn_hits)))
    for h in block_hits:
        w.write(_hit_line(h) + "\n")
    for h in warn_hits:
        w.write(_hit_line(h) + "\n")


def _do_scan(context, sc, repo, ranges, ref_names, words, identity_words,
             replacements_file):
    hits = _with_replacements(
        sc, replacements_file,
        lambda: _scan_repo(repo, ranges, ref_names, words, identity_words, sc))
    return _finish_scan(context, hits)


def _do_ref_scan(context, sc, ref_names, words, identity_words,
                 replacements_file):
    """纯删除的 push: 没有对象要扫, 但 ref 名要判。"""
    hits = _with_replacements(
        sc, replacements_file,
        lambda: _scan_ref_names(ref_names, words, identity_words, sc))
    return _finish_scan(context, hits)


def _finish_scan(context, hits):
    block_hits = [h for h in hits if h.severity == "block"]
    warn_hits = [h for h in hits if h.severity == "warn"]
    if block_hits:
        _emit_human_report(context, block_hits, warn_hits)
        return cc.fail(
            E_LEAK_FOUND,
            "发现 {0} 处 block 级敏感命中({1} 处 warn), push 被拦下".format(
                len(block_hits), len(warn_hits)),
            details=_hit_payload(hits))
    if warn_hits:
        _emit_human_report(context, [], warn_hits)
    return cc.ok(_hit_payload(hits))


def _cmd_pre_push(args, context):
    """pre-push: stdin 读协议行, 算范围后扫。不装钩子, 纯 python。"""
    if not args.repo or not args.remote_name or not args.remote_url:
        raise cc.CliError(E_ARG, "pre-push 需要 --repo / --remote-name / --remote-url",
                          exit_code=cc.EXIT_ARG)
    lines = sys.stdin.read().splitlines()
    if not lines:
        return cc.ok(_hit_payload([]))
    ranges, ref_names, _has_delete, has_non_delete = _parse_protocol_lines(lines)
    _check_repo(args.repo)
    sc = _require_secretscan()
    words = _load_words(sc, args.words_file)
    identity_words = _parse_identity_words(args.identity_words, sc)
    if not has_non_delete:
        return _do_ref_scan(context, sc, ref_names, words, identity_words,
                            args.replacements_file)
    return _do_scan(context, sc, args.repo, ranges, ref_names, words,
                    identity_words, args.replacements_file)


def _cmd_scan_repo(args, context):
    """scan-repo: 按给定 refs 全量扫(给校准和验收用)。"""
    if not args.repo:
        raise cc.CliError(E_ARG, "scan-repo 需要 --repo", exit_code=cc.EXIT_ARG)
    if not args.refs:
        raise cc.CliError(E_ARG, "scan-repo 需要至少一个 --refs", exit_code=cc.EXIT_ARG)
    _check_repo(args.repo)
    sc = _require_secretscan()
    words = _load_words(sc, args.words_file)
    identity_words = _parse_identity_words(args.identity_words, sc)
    ranges = [(ref, None) for ref in args.refs]
    return _do_scan(context, sc, args.repo, ranges, list(args.refs), words,
                    identity_words, args.replacements_file)


def build_parser():
    parser = cc.CliFriendlyParser(
        prog=PROG,
        description="敏感词发布闸扫描核心: pre-push 从 stdin 算外发范围并扫, "
                    "scan-repo 按 refs 全量扫。只做扫描, 不装钩子。")
    sub = parser.add_subparsers(dest="subcommand")

    p_pre = sub.add_parser("pre-push", help="读 stdin 协议行, 扫本次要外发的对象")
    p_pre.add_argument("--repo", required=True, help="本地仓库路径")
    p_pre.add_argument("--remote-name", required=True, help="本次 push 的 remote 名")
    p_pre.add_argument("--remote-url", required=True, help="本次 push 的目标 URL")
    _add_scan_args(p_pre)

    p_scan = sub.add_parser("scan-repo", help="按给定 refs 扫完整可达历史")
    p_scan.add_argument("--repo", required=True, help="本地仓库路径")
    p_scan.add_argument("--refs", nargs="+", required=True, help="要扫的 ref")
    _add_scan_args(p_scan)

    parser.add_argument("--json", action="store_true", help="JSON 信封输出")
    parser.add_argument("--format", choices=("json",), default="json",
                        help="输出格式: 仅支持 json(与 --json 等价)")
    parser.add_argument("--ai-help", action="store_true", help="AI 可读的使用说明")
    return parser


def _add_scan_args(p):
    """三个注入参数只影响取数来源, 不影响判定逻辑。"""
    p.add_argument("--words-file", metavar="P",
                   help="覆盖 ~/.repo-keeper/audit-words.txt")
    p.add_argument("--identity-words", metavar="w1,w2",
                   help="覆盖本机身份词(逗号分隔)")
    p.add_argument("--replacements-file", metavar="P",
                   help="覆盖 gate-replacements.txt")
    p.add_argument("--json", action="store_true", help="JSON 信封输出")
    p.add_argument("--format", choices=("json",), default="json",
                   help="输出格式: 仅支持 json(与 --json 等价)")
    p.add_argument("--ai-help", action="store_true", help="AI 可读的使用说明")


def command(argv, context):
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except cc.CliUsageError as exc:
        raise cc.CliError(E_ARG, exc.message, exit_code=cc.EXIT_ARG) from exc
    if args.subcommand == "pre-push":
        return _cmd_pre_push(args, context)
    if args.subcommand == "scan-repo":
        return _cmd_scan_repo(args, context)
    raise cc.CliError(E_ARG, "未指定子命令(pre-push / scan-repo)",
                      exit_code=cc.EXIT_ARG)


AI_HELP = """---
name: GateCheck
description: >
  敏感词发布闸的扫描核心(第二阶段第 1 批)。pre-push 从 stdin 读 git 协议行,
  按"本次要外发的对象"算范围并扫描 blob 内容 / 路径名 / 提交身份与 message /
  ref 名 / annotated tag 元数据; scan-repo 按给定 refs 全量扫。只实现扫描,
  不装钩子。供 hooks/pre-push(第 2 批)调用。fail-closed: 词表不可用、git 失败、
  输出解析失败、范围算空都退 E_GATE_UNAVAILABLE。
ai_help_version: 0.1.0
---

# GateCheck AI Help Guide

## Quick Reference

- **Push gate (hook side):** `GateCheck.py pre-push --repo <path> --remote-name <n> --remote-url <url> --json`
  (protocol lines come from stdin; the hook pipes them)
- **Full history scan:** `GateCheck.py scan-repo --repo <path> --refs <ref>... --json`
- **Help:** `GateCheck.py --ai-help`

## When to Use

Use this tool to scan git objects that are about to be pushed (or a whole ref's
reachable history) for sensitive words and structural leaks. It is the python
scan core of the pre-push gate; batch 2 wires it into the sh hook.

Do NOT use it to install hooks (`install-hooks` / `arm` / `sync-exempt` are later
batches), and do not expect it to rewrite anything — this tool never writes to the repo.

## Command Reference

- `pre-push --repo <path> --remote-name <n> --remote-url <url> [--words-file P] [--identity-words w1,w2] [--replacements-file P]`
  reads one protocol line per stdin line: `<local ref> <local sha> <remote ref> <remote sha>`.
  A 40/64 all-zero sha means "does not exist". Per line: remote exists -> scan
  `<remote>..<local>`; new ref (remote sha all-zero) -> scan full history of
  `<local>`; delete (local sha all-zero) -> skip.
- `scan-repo --repo <path> --refs <ref>...` scans each ref's full reachable history.
- `--words-file P` / `--identity-words w1,w2` / `--replacements-file P` override the
  data sources (defaults: `~/.repo-keeper/audit-words.txt`, machine identity words,
  `~/.repo-keeper/gate-replacements.txt`). They change the source only, never the logic.
- `--json` / `--format json`: machine envelope output.

## Input / Output

- `--json` success: `{ok:true, data:{hits:[...], block_count, warn_count}, error:null, meta:{log}}` on stdout.
  A hit is `{origin, line, column, rule, severity, display, suggestion}`. `display` is masked.
- `--json` failure: envelope on stderr, stdout empty; `error.code` distinguishes:
  `E_LEAK_FOUND` (block hit found, gate refuses) vs `E_GATE_UNAVAILABLE` (gate could not run).
  Hits are in `error.details.hits` for `E_LEAK_FOUND`.
- human mode (no `--json`): clean -> silent; hits -> report on stderr; errors -> message on stderr.

## Severity & Exit Codes

| case | exit | error.code |
|---|---|---|
| clean (no block hit) | 0 | null |
| only warn hits (`WIKI_LINK` / `FULL_HASH`) | 0 | null (warns listed in `data.hits`) |
| at least one block hit | 1 | `E_LEAK_FOUND` |
| gate could not complete the check | 1 | `E_GATE_UNAVAILABLE` |
| argument / usage error | 2 | `E_ARG` |

Note: the release gate `test_no_secrets.py` treats `WIKI_LINK` / `FULL_HASH` as
failures — this gate intentionally downgrades them to warn (plan D9). Do not "unify" them.

## Side Effects & Safety

- Read-only: never writes to the repo, never installs or modifies hooks.
- fail-closed: missing/empty/unreadable wordlist, git subcommand failure, output
  parse failure, or an empty object set for a non-delete push all refuse with
  `E_GATE_UNAVAILABLE`. There is no "could not decode -> treat as clean" path.
- The object scan goes through `git cat-file --batch` (object store), never the
  working tree.

## Errors & Recovery

| code | meaning | recovery |
|---|---|---|
| `E_LEAK_FOUND` | block-level sensitive hit found | fix the hit (see `suggestion`) and re-push |
| `E_GATE_UNAVAILABLE` | the gate could not run (wordlist/git/parse/range) | check error.details, fix the cause, re-run |
| `E_ARG` | bad arguments / usage | fix the arguments per the message |
"""


def main(argv=None, sinks=None, reconfigure=True):
    return cc.main(argv, sinks, command=command, parser_factory=build_parser,
                   ai_help=AI_HELP, prog=PROG, reconfigure=reconfigure)


if __name__ == "__main__":
    sys.exit(main())
