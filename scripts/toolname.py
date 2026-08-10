#!/usr/bin/env python3
"""The one place the tool's own name is written down.

The name leaks into five user-visible surfaces -- the global config directory,
the per-project config file, the banner stamped into generated ignore blocks,
the negation that keeps the re-anchor exe in the repo, and the exe's own
filename. Spelling it out five times means a rename silently half-lands: the
banner still says the old name while the config file has the new one, and
nothing fails, so nobody notices. Everything imports from here instead.
"""

TOOL_NAME = "repo-keeper"

#: Global, cross-project layer. Lives in $HOME, never inside a repo, so it
#: needs no ignore rule.
GLOBAL_DIR_NAME = "." + TOOL_NAME
GLOBAL_CONFIG_NAME = "defaults.toml"

#: Per-project layer. Lives *in* the repo and is therefore kept out of it by a
#: line in .git/info/exclude -- not by .gitignore, which is a shared file.
PROJECT_CONFIG_NAME = "." + TOOL_NAME + ".local.toml"

#: The re-anchor helper is committed into repos on purpose (it has to travel
#: with the checkout), so it is the one artifact whose name the ignore rules
#: must explicitly let through.
REANCHOR_EXE = TOOL_NAME + "-reanchor.exe"
