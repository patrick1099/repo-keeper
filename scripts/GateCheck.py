#!/usr/bin/env python3
"""敏感词发布闸的扫描核心（第二阶段第 1 批，纯 python，不装钩子）。

做三件事：从 git 协议输入算出"本次要外发"的对象范围，然后对范围内的
blob 内容 / 路径名 / 提交身份与 message / ref 名 / annotated tag 元数据
跑 secretscan 规则；另有 arm 子命令只回答"该不该武装"（URL 规范化 +
豁免表查询），给 sh 钩子做权威判定。规则模块在 scripts/secretscan.py，
本文件只调用它，不改任何规则。

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

本批（第 2 批）新增 arm 子命令与 sh 钩子（hooks/pre-push）；第 3 批负责
install-hooks / sync-exempt / 改 ~/.githooks/commit-msg。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
from datetime import datetime
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


def _parse_github_url(url):
    """把各种 remote URL 形状归一到 github.com/owner/repo（小写），返回 (is_github, normalized)。

    覆盖：https / scp 风格 SSH（git@host:path）/ ssh:// / 带凭据 / 结尾斜杠 /
    单个 .git 后缀 / 大小写归一。凭据部分丢弃，绝不回显。路径不是恰好
    owner/repo 两段（多余或缺段）判为不可识别，返回 normalized=None——
    调用方按候选处理（武装），绝不往"放行"倒。
    """
    text = (url or "").strip()
    if not text:
        return False, None
    if "://" in text:
        rest = text.split("://", 1)[1]
        if "@" in rest:
            rest = rest.rsplit("@", 1)[1]
        slash = rest.find("/")
        if slash < 0:
            authority, path = rest, ""
        else:
            authority, path = rest[:slash], rest[slash + 1:]
        if authority.startswith("["):
            end = authority.find("]")
            host = authority[:end + 1] if end >= 0 else authority
        else:
            host = authority.split(":", 1)[0]
    else:
        body = text
        if "@" in body:
            body = body.rsplit("@", 1)[1]
        if ":" not in body:
            return False, None
        host, path = body.split(":", 1)
    if host.lower() != "github.com":
        return False, None
    clean = path
    while clean.endswith("/"):
        clean = clean[:-1]
    if clean.endswith(".git"):
        clean = clean[:-len(".git")]
    segs = [s for s in clean.split("/") if s]
    if len(segs) != 2:
        return True, None
    return True, "github.com/" + "/".join(s.lower() for s in segs)


TTL_DAYS = 7


def _read_exempts(home):
    """读豁免表，返回 {normalized: checked_at(datetime)}。

    文件不存在 / 为空 -> 空 dict（= 无豁免，调用方照常武装，不是 fail-closed 报错）。
    每行一条 <normalized> <checked_at>，# 注释。checked_at 缺失或解析失败
    的条目直接丢弃——宁可武装，不可误放行。
    """
    path = Path(home) / "gate-exempt.txt"
    result = {}
    if not path.is_file():
        return result
    text = path.read_text(encoding="utf-8")
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        fields = line.split()
        if len(fields) < 2:
            continue
        key, stamp = fields[0], fields[1]
        try:
            result[key.lower()] = datetime.fromisoformat(stamp)
        except ValueError:
            continue
    return result


def _is_exempt_fresh(checked_at, now):
    """TTL 内算新鲜。naive/aware 混用不报错，统一按 checked_at 的时区口径比。"""
    if checked_at.tzinfo is None:
        now = now.replace(tzinfo=None) if now.tzinfo else now
    else:
        now = now.astimezone(checked_at.tzinfo)
    return (now - checked_at).total_seconds() <= TTL_DAYS * 86400


def _cmd_arm(args, context):
    """arm：只回答该不该武装。URL 规范化 + 豁免表查询，不启动扫描、不读词表、不碰 git。

    正常回答一律退 0（armed 是查询结果不是错误）；只有缺 --url 才退 2 E_ARG。
    豁免过期按任务书 §3.3 视为未豁免（本批不做联网复核），照常武装并扫描。
    """
    if not args.url:
        raise cc.CliError(E_ARG, "arm 需要 --url", exit_code=cc.EXIT_ARG)
    home = Path(args.home) if args.home else Path.home() / ".repo-keeper"
    is_github, normalized = _parse_github_url(args.url)
    if not is_github:
        return cc.ok({"normalized": None, "is_github": False, "armed": False,
                      "reason": "not_github", "exempt_state": "none"})
    if normalized is None:
        return cc.ok({"normalized": None, "is_github": True, "armed": True,
                      "reason": "unrecognized_path", "exempt_state": "none"})
    checked_at = _read_exempts(home).get(normalized)
    if checked_at is None:
        return cc.ok({"normalized": normalized, "is_github": True, "armed": True,
                      "reason": "no_exempt", "exempt_state": "none"})
    if _is_exempt_fresh(checked_at, datetime.now()):
        return cc.ok({"normalized": normalized, "is_github": True, "armed": False,
                      "reason": "exempt_fresh", "exempt_state": "fresh"})
    return cc.ok({"normalized": normalized, "is_github": True, "armed": True,
                  "reason": "exempt_expired", "exempt_state": "expired"})


# ---------------------------------------------------------------------------
# install-hooks / sync-exempt（第二阶段第 3 批）：安装器与豁免表同步器
# ---------------------------------------------------------------------------
#: git 支持的钩子名参照表（githooks(5)，依据 Git 2.53.0.windows.3）。
#: core.hooksPath 是"替换"默认 $GIT_DIR/hooks 而非叠加，缺的槽位等于把仓库
#: 本地同名钩子整个屏蔽掉，所以 --check 要如实报告当前目录缺哪些。
GIT_HOOK_NAMES = [
    "applypatch-msg", "pre-applypatch", "post-applypatch",
    "pre-commit", "pre-merge-commit", "prepare-commit-msg", "commit-msg",
    "post-commit", "pre-rebase", "post-checkout", "post-merge", "pre-push",
    "pre-receive", "update", "proc-receive", "post-receive", "post-update",
    "push-to-checkout", "pre-auto-gc", "post-rewrite", "sendemail-validate",
    "fsmonitor-watchman", "p4-pre-submit", "post-index-change",
]

#: 运行时闭包 4 个 .py（GateCheck.py -> cli_common.py / secretscan.py -> toolname.py）
INSTALL_PY_FILES = ["GateCheck.py", "cli_common.py", "secretscan.py",
                    "toolname.py"]
#: hooks/ 下 3 个分发文件（§9.4 已定的分发源）
INSTALL_HOOK_FILES = ["pre-push", "commit-msg", "_passthru"]

#: 这三个槽位不铺 _passthru。它们的"退 0"不是"放行"，而是"我已代劳/我已答复"：
#: push-to-checkout 退 0 意味着 git 认定工作区已由钩子更新完毕；proc-receive 要说
#: packet 协议；fsmonitor-watchman 退 0 且无输出意味着"没有文件变化"。铺一个只会
#: 退 0 的壳，在这三处制造的正是本方案要消灭的静默错误。而这三处"槽位缺失"本身是
#: fail-loud 的（git 直接报错，或退回默认拒绝），所以留空才是安全态。
PASSTHRU_SKIP_SLOTS = ("push-to-checkout", "proc-receive", "fsmonitor-watchman")

#: _passthru 要逐字节铺满的槽位。core.hooksPath 是"替换"而非"叠加"：任一槽位缺席，
#: 该仓库本地的同名钩子就再也不会被 git 调起，而 git 照常成功、盘上毫无痕迹。
#: 两个专用闸（pre-push / commit-msg）自带接力，不在此列。
PASSTHRU_SLOTS = [n for n in GIT_HOOK_NAMES
                  if n not in ("pre-push", "commit-msg")
                  and n not in PASSTHRU_SKIP_SLOTS]

#: 分发清单文件名。它既被写也被 --check 读回（方案 §D5：清单要能回答"装的是哪一版"）。
MANIFEST_NAME = "installed-manifest.json"

#: sync-exempt 的 gh --limit。显式给足够大，且"返回条数恰好等于 limit"按截断失败。
SYNC_EXEMPT_LIMIT = 1000

#: Windows(NTFS) 没有 exec 语义, os.chmod 也产生不了 st_mode 的 0111 位,
#: git-for-Windows 也不要求钩子带 exec bit。可执行位维度只在 POSIX 上有意义。
_HAS_EXEC_SEMANTICS = os.name != "nt"


def _norm_path(p):
    """路径规范化（expanduser -> 绝对 -> 大小写不敏感），用于 core.hooksPath 比对。"""
    return os.path.normcase(os.path.abspath(os.path.expanduser(str(p))))


def _sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def _find_source_root():
    """GateCheck.py 自身位置往上推，找同时含 hooks/ 与 scripts/ 的仓根。

    装到 ~/.repo-keeper/ 的副本自身不含 hooks/，所以用副本跑 install-hooks
    会在这里 fail-closed——分发源只认仓内这份。
    """
    script = Path(__file__).resolve()
    for parent in (script.parent, *script.parents):
        if (parent / "hooks").is_dir() and (parent / "scripts").is_dir():
            return parent
    raise cc.CliError(E_GATE_UNAVAILABLE, "找不到分发源目录（hooks/ 与 scripts/）")


def _current_hooks_path():
    """core.hooksPath 的实际值（规范前）。git config 失败或未设置返回 None。

    从家目录跑，避开"当前在某个仓里、被它自己的 local config 覆盖"的情况——
    install-hooks 管理的是全局钩子目录，per-repo 的 local 覆盖不在它的职责范围。
    """
    proc = subprocess.run(["git", "config", "--get", "core.hooksPath"],
                          cwd=str(Path.home()),
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        return None
    value = proc.stdout.decode("utf-8", "replace").strip()
    return value or None


def _plan_one(src, target, group, active_ok, name=None):
    """单个文件的状态判定（§2.3 四维：内容 / 可执行位 / 换行 / 是否在岗）。

    name 显式给出时用它当条目名：_passthru 的槽位副本在源里都叫 _passthru，
    但在计划里必须按各自的槽位名出现，否则报告里 22 行全叫一个名字。
    """
    src_bytes = src.read_bytes()
    src_sha = _sha256_bytes(src_bytes)
    item = {"name": name or src.name, "group": group, "target": target,
            "src_sha": src_sha, "src_bytes": src_bytes,
            "current_sha": None, "status": "missing"}
    if not target.exists():
        return item
    cur = target.read_bytes()
    cur_sha = _sha256_bytes(cur)
    item["current_sha"] = cur_sha
    if cur_sha != src_sha:
        # 内容不同 -> differs；只差 \r（换行问题）归 crlf，不算"装的是旧版"
        if _sha256_bytes(cur.replace(b"\r", b"")) == src_sha:
            item["status"] = "crlf"
        else:
            item["status"] = "differs"
        return item
    if group == "hooks":
        if _HAS_EXEC_SEMANTICS and not (os.stat(target).st_mode & 0o111):
            item["status"] = "mode-wrong"
            return item
        if not active_ok:
            item["status"] = "installed-but-inactive"
            return item
    item["status"] = "ok"
    return item


def _install_plan(source_root, hooks_target, home_target):
    """算分发计划：7 个文件 + 各自的状态。返回条目列表。"""
    active = _current_hooks_path()
    active_ok = active is not None and _norm_path(active) == _norm_path(hooks_target)
    items = []
    scripts_src = source_root / "scripts"
    hooks_src = source_root / "hooks"
    for name in INSTALL_PY_FILES:
        src = scripts_src / name
        if not src.is_file():
            raise cc.CliError(E_GATE_UNAVAILABLE, "分发源缺失: {0}".format(src))
        items.append(_plan_one(src, home_target / name, "python", active_ok))
    for name in INSTALL_HOOK_FILES:
        src = hooks_src / name
        if not src.is_file():
            raise cc.CliError(E_GATE_UNAVAILABLE, "分发源缺失: {0}".format(src))
        items.append(_plan_one(src, hooks_target / name, "hooks", active_ok))
    passthru_src = hooks_src / "_passthru"
    for name in PASSTHRU_SLOTS:
        items.append(_plan_one(passthru_src, hooks_target / name, "hooks",
                               active_ok, name=name))
    return items


class _BatchInstallError(Exception):
    """安装/同步过程中任何一步失败，整体中止并回滚。"""


class _BatchWriter:
    """原子写 + compare-and-swap + 备份 + 整批回滚 的共享写入器（§2.4）。

    write 对每一个目标：目标已存在且内容不同 -> 先复制 .bak-<时间戳>；临时文件
    保住原扩展名（.py 临时名必须以 .py 结尾，否则 Esafenet 把它写成明文，os.replace
    再把这分明文搬到目标上，hash 校验照样通过）；fsync 后 os.replace；紧贴 replace
    再验一次目标当前 sha（compare-and-swap，plan 之后被别的进程动过就整体中止）；
    写后校验 hash/可执行位/换行。任一个文件失败 -> 已写过的全部回滚，
    回滚本身失败要大声报（这是最坏情况：既没装成，也没还原）。
    """

    def __init__(self):
        self.written = []   # (target, backup_or_None, prev_sha_or_None, prev_mode_or_None)

    def _temp_name(self, target):
        suffix = target.suffix
        stem = target.name[:-len(suffix)] if suffix else target.name
        return target.parent / (".{0}.tmp-{1}{2}".format(stem, secrets.token_hex(6), suffix))

    def _backup_name(self, target):
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        return Path(str(target) + ".bak-" + stamp)

    def _abort(self, message):
        raise _BatchInstallError(message)

    def write(self, target, data, prev_sha, *, exec_mode=False):
        """原子写一个文件。prev_sha 是 plan 阶段记下的目标当前 sha（None=不存在）。"""
        backup = None
        if target.exists():
            if prev_sha is None:
                self._abort("目标在 plan 之后才出现, 拒绝覆盖: {0}".format(target))
            if _sha256_bytes(target.read_bytes()) != prev_sha:
                self._abort("compare-and-swap 失败: {0} 在 plan 之后被动过".format(target))
            if _sha256_bytes(data) != prev_sha:
                backup = self._backup_name(target)
                shutil.copy2(str(target), str(backup))
        elif prev_sha is not None:
            self._abort("目标在 plan 之后消失: {0}".format(target))
        temp = self._temp_name(target)
        try:
            with open(str(temp), "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            if target.exists() and _sha256_bytes(target.read_bytes()) != prev_sha:
                self._abort("compare-and-swap 失败(紧贴 replace): {0}".format(target))
            os.replace(str(temp), str(target))
        finally:
            if temp.exists():
                temp.unlink()
        if _sha256_bytes(target.read_bytes()) != _sha256_bytes(data):
            self.written.append((target, backup, prev_sha, None))
            self._abort("写后校验失败: {0}".format(target))
        if exec_mode:
            os.chmod(str(target), 0o755)
            if _HAS_EXEC_SEMANTICS and not (os.stat(target).st_mode & 0o111):
                self.written.append((target, backup, prev_sha, None))
                self._abort("可执行位校验失败: {0}".format(target))
            if b"\r" in target.read_bytes():
                self.written.append((target, backup, prev_sha, None))
                self._abort("换行校验失败(含 CR): {0}".format(target))
        self.written.append((target, backup, prev_sha, None))

    def rollback(self):
        """整批回滚，后写先回。回滚本身失败必须大声报。"""
        errors = []
        for target, backup, prev_sha, prev_mode in reversed(self.written):
            try:
                if backup is not None:
                    shutil.copy2(str(backup), str(target))
                    if prev_sha is not None and \
                            _sha256_bytes(target.read_bytes()) != prev_sha:
                        errors.append("{0} 回滚后 hash 与备份不符".format(target))
                elif prev_sha is None:
                    if target.exists():
                        target.unlink()
                elif prev_mode is not None:
                    os.chmod(str(target), prev_mode)
            except Exception as exc:
                errors.append("{0}: {1}".format(target, exc))
        if errors:
            raise _BatchInstallError("回滚失败: " + "; ".join(errors))


def _source_commit(source_root):
    """分发源仓 HEAD 短 sha；取不到返回 None（写进 manifest 为 null）。"""
    try:
        proc = subprocess.run(["git", "-C", str(source_root), "rev-parse",
                               "--short", "HEAD"],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=10)
        if proc.returncode != 0:
            return None
        out = proc.stdout.decode("utf-8", "replace").strip()
        return out or None
    except Exception:
        return None


def _source_closure_paths():
    """分发闭包的仓内相对路径：4 个 .py + 3 个 hooks/ 文件。"""
    return (["scripts/" + n for n in INSTALL_PY_FILES]
            + ["hooks/" + n for n in INSTALL_HOOK_FILES])


def _source_closure_state(source_root):
    """分发闭包干不干净：这 7 个文件是否都已跟踪、且工作区与暂存区都等于 HEAD。

    只看闭包不看整仓——日常迭代允许仓里别处有未提交改动，堵死那个会把迭代堵死。
    干净是 source_commit 能回答"装的是哪一版"的前提：闭包脏的时候，装进去的字节
    根本不是那个 commit 的内容，字段就是误导性的。

    返回 (dirty_files, reason)。两者都为空/None 才算干净；reason 非 None 表示
    "判定不了"（git 跑不起来等），一律按脏处理——fail-closed。
    """
    paths = _source_closure_paths()
    try:
        proc = subprocess.run(
            ["git", "-C", str(source_root), "status", "--porcelain", "--"] + paths,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
    except Exception as exc:
        return ([], "git status 跑不起来: {0}".format(exc))
    if proc.returncode != 0:
        return ([], "git status 退 {0}".format(proc.returncode))
    dirty = []
    for line in proc.stdout.decode("utf-8", "replace").splitlines():
        entry = line[3:] if len(line) > 3 else ""
        # 重命名报成 "old -> new"，取后者即可
        entry = entry.split(" -> ")[-1].strip().strip('"')
        if entry:
            dirty.append(entry)
    return (sorted(set(dirty)), None)


def _build_manifest(items, home_target, source_root, closure):
    """分发清单（§2.2）：只放路径与 hash，不放任何文件内容。"""
    dirty, reason = closure
    files = []
    for item in items:
        mode = "100755" if item["group"] == "hooks" else "100644"
        files.append({"target": str(item["target"]), "sha256": item["src_sha"],
                      "mode": mode, "group": item["group"]})
    return {"installed_at": datetime.now().isoformat(),
            "source_commit": _source_commit(source_root),
            "source_dirty": bool(dirty) or reason is not None,
            "source_dirty_files": dirty,
            "source_dirty_reason": reason,
            "files": files}


def _read_manifest(home_target):
    """读回分发清单。不存在返回 None；存在但读不动返回带 parse_error 的壳。"""
    path = home_target / MANIFEST_NAME
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"parse_error": True}


def _install_execute(items, hooks_target, home_target, source_root,
                     require_clean=False):
    """--stage / --activate 共用：逐文件安装 + 写 manifest。失败整批回滚。"""
    writer = _BatchWriter()
    manifest_target = home_target / MANIFEST_NAME
    try:
        closure = _source_closure_state(source_root)
        if require_clean:
            # 贴着写入再验一次：plan 读字节与真正落盘之间，源仓可能被改动过。
            dirty, reason = closure
            if dirty or reason:
                writer._abort("分发闭包在 plan 之后变脏: {0}".format(
                    reason or "、".join(dirty)))
        # manifest 也必须走 compare-and-swap：prev_sha 硬编码 None 会让"目标已存在"
        # 被误判成"plan 之后才出现"而整批中止，于是装第二次必定失败——
        # 那等于 --activate 一辈子只能成功一次，之后任何升级、重装、修复都装不进去。
        manifest_prev = (_sha256_bytes(manifest_target.read_bytes())
                         if manifest_target.exists() else None)
        for item in items:
            target = item["target"]
            if item["status"] in ("ok", "installed-but-inactive"):
                continue
            if item["status"] == "mode-wrong":
                if _sha256_bytes(target.read_bytes()) != item["src_sha"]:
                    writer._abort("compare-and-swap 失败: {0} 在 plan 之后被动过".format(target))
                old_mode = os.stat(target).st_mode & 0o777
                os.chmod(str(target), 0o755)
                writer.written.append((target, None, item["current_sha"], old_mode))
                continue
            writer.write(target, item["src_bytes"], item["current_sha"],
                         exec_mode=(item["group"] == "hooks"))
        manifest = _build_manifest(items, home_target, source_root, closure)
        writer.write(manifest_target,
                     (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
                     .encode("utf-8"),
                     manifest_prev)
    # 不能只接 _BatchInstallError：写入路径上的 shutil.copy2 / open / fsync /
    # os.replace / os.chmod 抛的 OSError（磁盘满、权限、文件被占用、路径过长）
    # 会越过回滚直接冒出去，留下装了一半的闭包——那意味着每次 push 都被拒。
    except Exception as exc:
        try:
            writer.rollback()
        except _BatchInstallError as rb:
            raise cc.CliError(
                E_GATE_UNAVAILABLE,
                "安装失败且回滚失败: {0}; 回滚错误: {1}".format(exc, rb),
                details={"stage": "rollback_failed"}) from rb
        raise cc.CliError(
            E_GATE_UNAVAILABLE, "安装失败, 已回滚: {0}".format(exc),
            details={"stage": "rolled_back"}) from exc
    return manifest


def _resolve_target_dir(flag_value, env_value, default):
    if flag_value:
        return flag_value
    if env_value:
        return env_value
    return str(default)


def _report_check(context, items, hooks_target, home_target, closure):
    """--check：一个字节都不写、一个目录都不建，只出计划（§2.5）。"""
    active = _current_hooks_path()
    dirty, dirty_reason = closure
    manifest = _read_manifest(home_target)
    lines = []
    for item in items:
        line = "  {0:<16} {1:<24} {2}".format(
            item["name"], item["status"], item["target"])
        if item["status"] == "installed-but-inactive":
            line += "  (core.hooksPath 当前: {0})".format(active or "<未设置>")
        lines.append(line)
    missing_slots = [n for n in GIT_HOOK_NAMES if not (hooks_target / n).exists()]
    if not context.json_mode:
        w = context.sinks.err
        w.write("install-hooks --check\n")
        w.write("  目标钩子目录: {0}\n".format(hooks_target))
        w.write("  目标配置目录: {0}\n".format(home_target))
        w.write("  core.hooksPath: {0}\n".format(active or "<未设置>"))
        for line in lines:
            w.write(line + "\n")
        w.write("  当前目录缺这些 git 槽位({0} 个): {1}\n".format(
            len(missing_slots), ", ".join(missing_slots)))
        w.write("  分发闭包: {0}\n".format(
            dirty_reason or ("干净" if not dirty else
                             "脏 -> " + "、".join(dirty))))
        if manifest is None:
            w.write("  已装清单: <无>\n")
        else:
            w.write("  已装清单: {0} source_commit={1} dirty={2} files={3}\n".format(
                manifest.get("installed_at"), manifest.get("source_commit"),
                manifest.get("source_dirty"), len(manifest.get("files") or [])))
    return cc.ok({"active_hooks_dir": active,
                  "files": [{"name": i["name"], "status": i["status"],
                             "target": str(i["target"])} for i in items],
                  "missing_slots": missing_slots,
                  "source_closure": {"dirty": dirty, "reason": dirty_reason},
                  "installed_manifest": (None if manifest is None else {
                      "installed_at": manifest.get("installed_at"),
                      "source_commit": manifest.get("source_commit"),
                      "source_dirty": manifest.get("source_dirty"),
                      "files": len(manifest.get("files") or []),
                      "parse_error": bool(manifest.get("parse_error"))})})


def _cmd_install_hooks(args, context):
    """install-hooks：--check / --stage / --activate 三模式互斥且必须显式给一个。

    三者共用同一个 planner（_install_plan）；--stage 与 --activate 再共用同一个
    writer（_BatchWriter），走的是逐字节同一条写路径。--check 不做任何模拟，
    它报的就是 planner 算出来的那份计划，所以不存在"预演与真装漂移"。
    """
    modes = [bool(args.check), bool(args.stage), bool(args.activate)]
    if sum(modes) != 1:
        raise cc.CliError(
            E_ARG, "install-hooks 必须且只能给一个模式: --check / --stage DIR / --activate",
            exit_code=cc.EXIT_ARG)
    if args.stage:
        stage = Path(args.stage).resolve()
        if not stage.is_dir():
            raise cc.CliError(E_GATE_UNAVAILABLE,
                              "--stage 目标目录不存在: {0}".format(stage))
        hooks_target = stage / "hooks"
        home_target = stage / "home"
        # 只有 --stage 建目录，且只建这两个固定子目录、DIR 本身仍要求先存在。
        # --check / --activate 分支里一个 mkdir 都没有：打错路径时造出一套
        # "字节全对但 git 根本不看"的目录还报成功，正是要防的那种静默成功。
        hooks_target.mkdir(exist_ok=True)
        home_target.mkdir(exist_ok=True)
    else:
        hooks_target = Path(_resolve_target_dir(
            args.hooks_dir, os.environ.get("REPO_KEEPER_HOOKS_DIR"),
            Path.home() / ".githooks")).resolve()
        home_target = Path(_resolve_target_dir(
            args.home, os.environ.get("REPO_KEEPER_HOME"),
            Path.home() / ".repo-keeper")).resolve()
    if not hooks_target.is_dir():
        raise cc.CliError(E_GATE_UNAVAILABLE,
                          "目标钩子目录不存在: {0}".format(hooks_target))
    if not home_target.is_dir():
        raise cc.CliError(E_GATE_UNAVAILABLE,
                          "目标配置目录不存在: {0}".format(home_target))
    source_root = _find_source_root()
    closure = _source_closure_state(source_root)
    items = _install_plan(source_root, hooks_target, home_target)
    if args.check:
        return _report_check(context, items, hooks_target, home_target, closure)
    if args.activate:
        # 装成功但没生效，是这个方案要消灭的失效模式本身：字节全对、manifest 全对，
        # 而 git 根本不看那个目录。所以 core.hooksPath 必须先指到安装目标。
        active = _current_hooks_path()
        if active is None or _norm_path(active) != _norm_path(hooks_target):
            raise cc.CliError(
                E_GATE_UNAVAILABLE,
                "core.hooksPath 现在是 {0}，与安装目标 {1} 不一致；"
                "先把它指到目标再装，否则装完 git 不会调用这些钩子。".format(
                    active or "<未设置>", hooks_target))
        dirty, reason = closure
        if dirty or reason:
            raise cc.CliError(
                E_GATE_UNAVAILABLE,
                "分发闭包不干净，拒绝 --activate（先把这 7 个文件提交成一个干净的"
                "本地 commit，manifest 的 source_commit 才答得了\"装的是哪一版\"）: "
                "{0}".format(reason or "、".join(dirty)))
    manifest = _install_execute(items, hooks_target, home_target, source_root,
                                require_clean=bool(args.activate))
    return cc.ok({"mode": "stage" if args.stage else "activate",
                  "installed": len(items),
                  "manifest": manifest,
                  "targets": {"hooks_dir": str(hooks_target),
                              "home": str(home_target)}})


def _gh_command(value):
    """gh 命令解析（--gh-command 是测试注入位）。按空白拆, 不碰反斜杠。"""
    if not value:
        return ["gh"]
    return [part for part in value.split() if part]


def _run_gh(argv, gh_command):
    """跑 gh。显式钉死 GH_HOST=github.com, 防企业版同名私有仓被当成 github.com 写进表。"""
    env = dict(os.environ)
    env["GH_HOST"] = "github.com"
    proc = subprocess.run(gh_command + argv, env=env,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          timeout=60)
    return proc


def _cmd_sync_exempt(args, context):
    """sync-exempt：把 gh 列出的 PRIVATE 仓填进豁免表。只在人工调用时联网。"""
    home = Path(_resolve_target_dir(args.home,
                                    os.environ.get("REPO_KEEPER_HOME"),
                                    Path.home() / ".repo-keeper")).resolve()
    if not home.is_dir():
        raise cc.CliError(E_GATE_UNAVAILABLE,
                          "配置根目录不存在: {0}".format(home))
    proc = _run_gh(["repo", "list", "--json", "nameWithOwner,visibility,isArchived",
                    "--limit", str(SYNC_EXEMPT_LIMIT)], _gh_command(args.gh_command))
    if proc.returncode != 0:
        raise cc.CliError(
            E_GATE_UNAVAILABLE,
            "gh repo list 失败(退出码 {0}), 一个字节都不写".format(proc.returncode),
            details={"stderr_tail": proc.stderr.decode("utf-8", "replace")[-2000:]})
    try:
        repos = json.loads(proc.stdout.decode("utf-8", "replace"))
    except ValueError as exc:
        raise cc.CliError(E_GATE_UNAVAILABLE,
                          "gh 输出不是合法 JSON, 一个字节都不写",
                          details={"exc": type(exc).__name__}) from exc
    if not isinstance(repos, list):
        raise cc.CliError(E_GATE_UNAVAILABLE, "gh 输出不是列表, 一个字节都不写")
    if len(repos) >= SYNC_EXEMPT_LIMIT:
        raise cc.CliError(
            E_GATE_UNAVAILABLE,
            "gh 返回 {0} 条恰好达到 --limit {1}, 疑似被截断, 整体失败不写文件".format(
                len(repos), SYNC_EXEMPT_LIMIT))
    now = datetime.now()
    entries = {}
    skipped = []
    for repo in repos:
        if not isinstance(repo, dict) or "nameWithOwner" not in repo:
            skipped.append("(字段缺失)")
            continue
        raw = repo["nameWithOwner"]
        if not isinstance(raw, str) or not raw:
            skipped.append("(非字符串)")
            continue
        if repo.get("visibility") != "PRIVATE":
            skipped.append(raw)
            continue
        # 复用 _parse_github_url 做规范化: 两套规范化必然漂移, 漂移就是静默失效
        is_gh, normalized = _parse_github_url("https://github.com/" + raw)
        if not is_gh or normalized is None:
            skipped.append(raw)
            continue
        entries[normalized] = now
    content = "".join("{0} {1}\n".format(key, ts.isoformat())
                      for key, ts in sorted(entries.items()))
    if args.dry_run:
        if not context.json_mode:
            w = context.sinks.err
            w.write("sync-exempt --dry-run: 将写入 {0} 条豁免(全部为 PRIVATE):\n".format(
                len(entries)))
            for key in sorted(entries):
                w.write("  {0}\n".format(key))
        return cc.ok({"dry_run": True, "count": len(entries),
                      "entries": sorted(entries), "skipped": skipped})
    target = home / "gate-exempt.txt"
    prev_sha = _sha256_bytes(target.read_bytes()) if target.exists() else None
    writer = _BatchWriter()
    try:
        writer.write(target, content.encode("utf-8"), prev_sha)
    except _BatchInstallError as exc:
        try:
            writer.rollback()
        except _BatchInstallError as rb:
            raise cc.CliError(
                E_GATE_UNAVAILABLE,
                "写豁免表失败且回滚失败: {0}; 回滚错误: {1}".format(exc, rb),
                details={"stage": "rollback_failed"}) from rb
        raise cc.CliError(E_GATE_UNAVAILABLE, "写豁免表失败, 已回滚: {0}".format(exc),
                          details={"stage": "rolled_back"}) from exc
    if not context.json_mode:
        w = context.sinks.err
        w.write("sync-exempt: 已写 {0} 条豁免到 {1}（跳过 {2} 条）\n".format(
            len(entries), target, len(skipped)))
        if skipped:
            w.write("  跳过(非 PRIVATE / 解析不了): {0}\n".format(
                ", ".join(skipped[:20])))
    return cc.ok({"count": len(entries), "skipped": skipped,
                  "target": str(target)})


def _add_json_args(p):
    """给子命令补 --json / --format / --ai-help（与 _add_scan_args 的约定一致）。"""
    p.add_argument("--json", action="store_true", help="JSON 信封输出")
    p.add_argument("--format", choices=("json",), default="json",
                   help="输出格式: 仅支持 json(与 --json 等价)")
    p.add_argument("--ai-help", action="store_true", help="AI 可读的使用说明")


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

    p_arm = sub.add_parser("arm", help="只回答该不该武装（URL 规范化 + 豁免表查询）")
    p_arm.add_argument("--url", required=True, help="push 目标 URL")
    p_arm.add_argument("--home", metavar="DIR", help="配置根目录（默认 ~/.repo-keeper）")
    p_arm.add_argument("--json", action="store_true", help="JSON 信封输出")
    p_arm.add_argument("--format", choices=("json",), default="json",
                       help="输出格式: 仅支持 json(与 --json 等价)")

    p_inst = sub.add_parser("install-hooks", help="安装/校验全局钩子与 python 闭包")
    inst_mode = p_inst.add_mutually_exclusive_group(required=True)
    inst_mode.add_argument("--check", action="store_true", help="只出计划, 不写字节")
    inst_mode.add_argument("--stage", metavar="DIR", help="写 DIR 下的一次性目录(与真装同一写路径)")
    inst_mode.add_argument("--activate", action="store_true", help="写真实 ~/.githooks + ~/.repo-keeper")
    p_inst.add_argument("--hooks-dir", metavar="DIR", help="钩子目标目录(默认 ~/.githooks)")
    p_inst.add_argument("--home", metavar="DIR", help="配置根目录(默认 ~/.repo-keeper)")
    _add_json_args(p_inst)

    p_sync = sub.add_parser("sync-exempt", help="把 gh 上的 PRIVATE 仓填入豁免表(联网, 人工跑)")
    p_sync.add_argument("--home", metavar="DIR", help="配置根目录(默认 ~/.repo-keeper)")
    p_sync.add_argument("--gh-command", metavar="CMD", help="gh 命令(测试注入用)")
    p_sync.add_argument("--dry-run", action="store_true", help="只打印将写入的条目, 不写文件")
    _add_json_args(p_sync)

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
    if args.subcommand == "install-hooks":
        return _cmd_install_hooks(args, context)
    if args.subcommand == "sync-exempt":
        return _cmd_sync_exempt(args, context)
    if args.subcommand == "arm":
        return _cmd_arm(args, context)
    raise cc.CliError(E_ARG, "未指定子命令(pre-push / scan-repo / arm)",
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
- **Arm query (hook side):** `GateCheck.py arm --url <url> --home <dir> --json`
  (answers whether to arm: URL normalization + exempt-table lookup; normal answers exit 0)
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
## install-hooks & sync-exempt (batch 3)

- `install-hooks (--check | --stage DIR | --activate) [--hooks-dir DIR] [--home DIR]`
  installs the 3 hooks into the hooks dir and the 4-file python closure
  (`GateCheck.py` / `cli_common.py` / `secretscan.py` / `toolname.py`) into the
  home dir, plus `installed-manifest.json`. The three modes share one planner and
  one writer; `--check` only reports (writes nothing), `--stage DIR` runs the real
  write path against `DIR/hooks` and `DIR/home`, `--activate` writes the real
  `~/.githooks` and `~/.repo-keeper` (deferred to the human decision gate).
- `sync-exempt [--home DIR] [--gh-command CMD] [--dry-run]` queries `gh repo list`
  (network, manual runs only), writes PRIVATE repos into `<home>/gate-exempt.txt`
  with a `checked_at` timestamp. The gate itself never goes online.
"""


def main(argv=None, sinks=None, reconfigure=True):
    return cc.main(argv, sinks, command=command, parser_factory=build_parser,
                   ai_help=AI_HELP, prog=PROG, reconfigure=reconfigure)


if __name__ == "__main__":
    sys.exit(main())
