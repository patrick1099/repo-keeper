#!/usr/bin/env python3
"""Two-layer TOML config: reusable global defaults, per-project overrides.

Why two layers. Identity, doc globs and the protected-branch list are the same
across every repo one person works in;
branch refs, code paths and deliberate-drift whitelists are not. One file per
repo means copying the shared half into every one of them and watching the
copies drift. One global file means the project-specific half has nowhere to
live. So:

    ~/<GLOBAL_DIR_NAME>/defaults.toml   cross-project, outside every repo,
                                        needs no ignore rule
    <repo>/<PROJECT_CONFIG_NAME>        project-specific, overrides the global,
                                        kept out of git by .git/info/exclude
                                        (NOT .gitignore -- that is a shared file)

Merge semantics, stated because getting them wrong produces bugs nobody can
see:

  * **Tables deep-merge by key.** Adding one ``[expected_drift]`` entry in a
    project must not wipe the global ones.
  * **Arrays replace wholesale.** An array is one value, not a set. Concatenating
    would make "I deleted an entry and it is still there" a routine confusion,
    and there would be no way to shrink an inherited list.
  * **Scalars: the project wins.**

Because a layered config's mistakes are invisible -- a value silently coming
from the wrong file looks exactly like a value coming from the right one --
every leaf records which layer produced it and ``Config.explain()`` prints it.
Any CLI that reads config should expose that as ``--explain``.

Nothing here validates on load. Which keys are required depends on the
consumer: branch reconciliation needs ``branches.clean``, repo hygiene needs no
config at all. Callers state their own needs via ``Config.require()``, which
refuses to guess a default and says which layer to put the key in.
"""
import os
import subprocess
import sys
import tomllib
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from toolname import (GLOBAL_CONFIG_NAME, GLOBAL_DIR_NAME,  # noqa: E402
                      PROJECT_CONFIG_NAME, TOOL_NAME)
import cli_common as cc  # noqa: E402

GLOBAL = "global"
PROJECT = "project"


class ConfigError(Exception):
    """A missing or malformed setting. Callers print it and exit non-zero."""


# Every key a consumer may require, with what it is for and where it belongs.
# The layer suggestion is the whole point of the table: "branches.clean is
# missing" is useless if the reader then has to guess which of two files to
# open.
KEY_HINTS = {
    "branches.clean": (
        "干净分支的 ref —— 纯代码、对外/对客户的那条",
        PROJECT, 'clean = "customer/xxx-20260519"'),
    "branches.work": (
        "工作分支的 ref —— 代码 + 文档 + 工具的超集",
        PROJECT, 'work = "task/xxx-20260519"'),
    "branches.main": (
        "主分支的 ref —— 同步时只读核对,绝不触碰",
        GLOBAL, 'main = "main"'),
    "branches.protected": (
        "受保护分支列表 —— 在这些分支上不直接干活",
        GLOBAL, 'protected = ["main", "master"]'),
    "paths.code": (
        "算作固件代码的路径 —— drift 以这些路径为准,每个仓库结构都不同",
        PROJECT, 'code = ["App", "Boot", "drv", "bsp"]'),
    "paths.doc_globs": (
        "越界文档的探测模式 —— 跟项目无关,填全局",
        GLOBAL, 'doc_globs = ["*.md", "*.csv", "*.txt", "*.docx", "docs/"]'),
    "identity.clean": (
        "干净分支上的提交身份 —— 身份跟着分支纯度走,不跟着仓库走",
        GLOBAL, 'clean = { name = "...", email = "..." }'),
    "identity.work": (
        "工作分支上的提交身份",
        GLOBAL, 'work = { name = "...", email = "..." }'),
    "scan.depth": (
        "匹配同步点时回看多少条提交",
        GLOBAL, "depth = 60"),
}


# ---------------------------------------------------------------------------
# merge
# ---------------------------------------------------------------------------

def merge_layers(layers):
    """Merge ``[(layer_name, table), ...]`` lowest-precedence first.

    Returns ``(merged, origins)`` where *origins* maps every leaf's dotted key
    to the layer that supplied it. A leaf is anything that is not a table:
    scalars and arrays both.
    """
    return _merge(layers, "")


def _merge(layers, prefix):
    merged, origins, order = {}, {}, []
    for _, table in layers:
        for key in table:
            if key not in order:
                order.append(key)

    for key in order:
        present = [(name, table[key]) for name, table in layers if key in table]
        if all(isinstance(value, dict) for _, value in present):
            sub, sub_origins = _merge(present, prefix + key + ".")
            merged[key] = sub
            origins.update(sub_origins)
        else:
            # Not all tables -> not a deep merge. Last layer wins outright,
            # which is also how a project scalar shadows a global table: the
            # sub-keys are gone, so they get no origin entry either.
            name, value = present[-1]
            merged[key] = value
            origins[prefix + key] = name
    return merged, origins


# ---------------------------------------------------------------------------
# discovery
# ---------------------------------------------------------------------------

def _git(args, cwd):
    """Run git, returning stripped stdout, or None when it fails.

    Failure is ordinary here (not a repo, no worktree) and must not be fatal:
    the global layer alone is still a usable config.
    """
    try:
        r = subprocess.run(["git", "-C", str(cwd), *args],
                           capture_output=True, encoding="utf-8")
    except OSError:
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def global_config_path():
    return Path.home() / GLOBAL_DIR_NAME / GLOBAL_CONFIG_NAME


def project_config_path(start=None):
    """Where this repo's config file is, or None.

    Looks in the current worktree first, then the main worktree -- so one file
    at the top of the main checkout serves every linked worktree, which is the
    normal arrangement when clean and work branches each have their own.
    """
    start = Path(start or os.getcwd()).resolve()

    top = _git(["rev-parse", "--show-toplevel"], start)
    if top:
        candidate = Path(top) / PROJECT_CONFIG_NAME
        if candidate.is_file():
            return candidate

    # --git-common-dir, not --git-dir: a linked worktree's private git dir is
    # <main>/.git/worktrees/<name>, whose parent is not a checkout at all.
    common = _git(["rev-parse", "--path-format=absolute", "--git-common-dir"],
                  start)
    if common:
        candidate = Path(common).parent / PROJECT_CONFIG_NAME
        if candidate.is_file():
            return candidate
    return None


def _read_toml(path):
    try:
        with open(path, "rb") as fh:
            return tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError("{0} 解析失败: {1}".format(path, exc)) from exc
    except OSError as exc:
        raise ConfigError("{0} 读不了: {1}".format(path, exc)) from exc


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

class Config:
    def __init__(self, data, origins, sources):
        self._data = data
        self._origins = origins
        #: {layer: path or None} -- None means that layer was absent, which is
        #: legal for the global one and worth showing in --explain.
        self.sources = sources

    # -- reading ----------------------------------------------------------
    def get(self, dotted, default=None):
        node = self._data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def has(self, dotted):
        sentinel = object()
        return self.get(dotted, sentinel) is not sentinel

    def require(self, *dotted_keys):
        """Return the values, or raise ConfigError naming every missing key.

        Reports them all at once: fixing one at a time, re-running, and being
        told about the next is a worse experience than one complete list.
        """
        missing = [k for k in dotted_keys if not self.has(k)]
        if missing:
            raise ConfigError(self._missing_message(missing))
        return [self.get(k) for k in dotted_keys]

    def _missing_message(self, missing):
        lines = ["配置缺少 {0} 个必填项:".format(len(missing)), ""]
        for key in missing:
            why, layer, example = KEY_HINTS.get(
                key, ("(没有登记说明)", PROJECT, ""))
            target = (self.sources.get(layer)
                      or self._suggested_path(layer))
            lines.append("  {0}".format(key))
            lines.append("      作用: {0}".format(why))
            lines.append("      填到: {0}  [{1} 层]".format(target, layer))
            if example:
                section = key.split(".")[0]
                lines.append("      例:   [{0}]".format(section))
                lines.append("            {0}".format(example))
            lines.append("")
        lines.append("不猜默认值 —— 猜错的分支 ref 会让工具对着错的分支干活。")
        return "\n".join(lines)

    @staticmethod
    def _suggested_path(layer):
        if layer == GLOBAL:
            return str(global_config_path())
        return "<仓库根>/" + PROJECT_CONFIG_NAME

    # -- provenance -------------------------------------------------------
    def origin(self, dotted):
        """Which layer supplied this leaf, or None if the key is unset."""
        return self._origins.get(dotted)

    def explain(self):
        lines = ["{0} 配置来源".format(TOOL_NAME), "=" * 60]
        for layer in (GLOBAL, PROJECT):
            path = self.sources.get(layer)
            lines.append("  {0:<8} {1}".format(
                layer, path if path else "(不存在 —— 全部继承/缺省)"))
        lines.append("")
        if not self._origins:
            lines.append("  (没有任何配置项)")
            return "\n".join(lines)
        width = max(len(k) for k in self._origins)
        for key in sorted(self._origins):
            lines.append("  [{0:<7}] {1:<{2}}  = {3!r}".format(
                self._origins[key], key, width, self.get(key)))
        return "\n".join(lines)


def load(start=None, global_path=None, project_path=None):
    """Load and merge both layers.

    *global_path* / *project_path* exist for tests; normal callers pass
    nothing and get the discovery order documented in the module docstring.
    A missing global layer is not an error -- the whole point of the project
    layer is that it can stand alone.
    """
    gp = Path(global_path) if global_path else global_config_path()
    pp = Path(project_path) if project_path else project_config_path(start)

    layers, sources = [], {GLOBAL: None, PROJECT: None}
    if gp and gp.is_file():
        layers.append((GLOBAL, _read_toml(gp)))
        sources[GLOBAL] = str(gp)
    if pp and Path(pp).is_file():
        layers.append((PROJECT, _read_toml(pp)))
        sources[PROJECT] = str(pp)

    merged, origins = merge_layers(layers)
    return Config(merged, origins, sources)


def add_explain_flag(parser):
    parser.add_argument(
        "--explain", action="store_true",
        help="打印每个配置值来自哪一层(全局/项目),然后退出")


def main(argv=None, sinks=None):
    """`python local_config.py [--explain] [--json]` -- inspect the merged
    config. Machine channel follows the CLI-AI spec via cli_common."""
    return cc.main(argv, sinks, command=command, parser_factory=build_parser,
                   ai_help=AI_HELP, prog="local_config")


def explain_data(cfg):
    """Structured view of the merged config for the JSON channel."""
    keys = sorted(cfg._origins)
    return {
        "sources": {layer: (cfg.sources.get(layer) or None)
                    for layer in (GLOBAL, PROJECT)},
        "origins": {key: cfg.origin(key) for key in keys},
        "config": {key: cfg.get(key) for key in keys},
    }


def build_parser():
    parser = cc.CliFriendlyParser(
        prog="local_config",
        description="LLMs/agents: run 'local_config --ai-help' for usage guidance. "
                    "查看两层合并后的配置(全局默认 + 项目覆盖),以及每个值来自哪一层。")
    add_explain_flag(parser)
    parser.add_argument("--json", action="store_true",
                        help="以 JSON 信封输出(与 --format json 等价)")
    parser.add_argument("--format", choices=("json",), default="json",
                        help="输出格式:仅支持 json(与 --json 等价)")
    parser.add_argument("--ai-help", action="store_true",
                        help="输出 AI 优化的使用说明并退出")
    return parser


def command(argv, context):
    parser = build_parser()
    parser.parse_args(argv)
    try:
        cfg = load()
    except ConfigError as exc:
        return cc.fail("E_VALIDATION", str(exc), exit_code=cc.EXIT_ARG)
    if context.json_mode:
        return cc.ok(explain_data(cfg))
    context.sinks.out.write(cfg.explain() + "\n")
    return cc.ok()


AI_HELP = """---
name: local_config
description: >
  Inspect repo-keeper's merged two-layer config (global defaults in
  ~/.repo-keeper/defaults.toml + per-project overrides in .repo-keeper.local.toml)
  and show which layer every value came from. Use when the user asks where a
  config value is set, why a branch ref resolves a certain way, or wants the
  effective config of the current repo.
ai_help_version: 0.1.0
---

# local_config AI Help Guide

## Quick Reference

- **Inspect config (human):** `local_config.py [--explain]`
- **Inspect config (machine):** `local_config.py --json`

## When to Use

Use this tool when the user asks to:
- see the effective repo-keeper config (global + project merged)
- know which layer (global / project) a config key comes from
- debug why a branch ref / code path resolves a certain way

Do NOT use for:
- writing config (this tool only reads and explains)

## Command Reference

- `--explain`: print which layer each config value came from, then exit
- `--json`: machine envelope output (equivalent to `--format json`)

## Input / Output

- `--json` success: `{ok:true, data:{sources, origins, config}, error:null, meta:{}}`
- `--json` failure: envelope on stderr, stdout empty; `error.code` from the table below
- human mode: the `explain()` text on stdout, errors on stderr

## Side Effects & Safety

- Read-only: never writes config files.

## Exit Codes

| code | meaning |
|---|---|
| 0 | success |
| 1 | runtime failure (see error.code) |
| 2 | parameter / usage error (E_VALIDATION) |

## Errors & Recovery

| code | meaning | recovery |
|---|---|---|
| `E_VALIDATION` | config file missing or malformed | fix the named file per the message |
| `E_INTERNAL` | unexpected bug | report it |
"""


if __name__ == "__main__":
    sys.exit(main())
