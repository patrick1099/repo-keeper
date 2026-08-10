---
name: clangd-config
description: Generate and validate .clangd + compile_commands.json for embedded C projects from Keil .uvprojx, IAR .ewp (any architecture — ICCARM, ICCRL78, ICCRX, ICC430), or CMake. Use when setting up clangd-based jump/completion/diagnostics for a firmware project, when clangd reports missing macros / include paths / vendor-extension syntax errors, when cross-file jump-to-definition silently fails while same-file navigation works, when a generator refuses to pick between build targets or project files, or when an existing config must be carried somewhere else — the project moved, a new machine, another worktree/checkout, or "pull the config over from that other repo".
---

# Project to clangd Configuration Generator

Generate `.clangd` and `compile_commands.json` for an embedded C project, then
validate the output and fix what the scripts cannot.

Generating is the default path. Do **not** go looking for an existing config to
reuse first — searching for one costs more than regenerating. Reuse only comes
up when the user brings it up ("the project moved", "pull the config over from
that other repo", "new machine"), and it is handled under
[Project moved / new machine](#project-moved--new-machine-re-anchor).

## Pick a backend

```powershell
py -3 "${CLAUDE_PLUGIN_ROOT}/scripts/Proj2Clangd.py" -p <dir> --detect-only
```

`Proj2Clangd.py` detects the project type and forwards every other argument to
the matching backend. The backends also run standalone:

| Project | Detected by | Backend | What it does |
|---|---|---|---|
| Keil MDK | `*.uvprojx` | `Keil2Clangd.py` | Parses XML + `.dep`, synthesises all flags |
| IAR EW | `*.ewp` | `Iar2Clangd.py` | Parses XML, **probes the real IAR compiler** for macros |
| CMake | `CMakeLists.txt` | `Cmake2Clangd.py` | Runs configure, then makes CMake's own database discoverable |

If a tree holds more than one, `Proj2Clangd.py` refuses to guess — pass
`--kind keil|iar|cmake`.

---

# Keil (.uvprojx)

## Keil path configuration

The script persists the Keil installation path to `~/.keil2clangd.json`.
Discovery priority: `-k`/`--keil-path` → `KEIL_PATH` → `~/.keil2clangd.json` →
scan of `D:/Keil_v5`, `C:/Keil_v5`, `C:/Keil` → interactive prompt (saved).

## CMSIS version selection

The script parses the device pack's `.pdsc` for CMSIS version requirements and
picks the closest installed version ≥ that. Otherwise the latest installed.

## Steps

### 1. List the targets — with the script, not by hand

```powershell
py -3 "${CLAUDE_PLUGIN_ROOT}/scripts/Keil2Clangd.py" -p <uvprojx_parent_dir> --dry-run
```

This is the Keil counterpart of IAR's `--list-configs`: it prints every target
with its macros and warns about macros present in other targets but not the one
that would be used. Do **not** hand-parse the XML — the script already does it,
and a second parser only invites disagreement.

The marker on that listing says exactly what it means. `<-- chosen by -t` is
your choice; `<-- default: FIRST IN XML` is **not** the target selected in the
Keil IDE — that lives in `.uvoptx` and nothing here reads it.

**You do not have to remember to ask.** Both ambiguous choices are refused by
the script with a non-zero exit and a listing of the candidates:

| Situation | Exit | Way out |
|---|---|---|
| Several `.uvprojx` under `-p` | 2 | `--project <path>` |
| Several targets and no `-t` (when writing) | 2 | `-t <name>` |

`--dry-run` is exempt — it is how you look before choosing, and it ends with an
`[ACTION REQUIRED]` line naming what to pass next. `--use-first-target` exists
for unattended runs that genuinely do not care which build gets indexed; it is
not a shortcut around asking the user.

**Key point:** targets often differ in chip-variant macros (`__G048` vs
`__LG048`), feature flags (`__CODE_IAP`, `USE_FULL_ASSERT`) and BSP paths. If a
target's name contains a variant (`LG048`) but its macros lack the matching
define (`__LG048`), **say so** — that is a common Keil misconfiguration, and
the user usually wants to know even when it is not the target being generated.
No script checks this; it is a reminder to you, not a guarantee from the tool.

### 2. Run the script

```powershell
py -3 "${CLAUDE_PLUGIN_ROOT}/scripts/Keil2Clangd.py" -p <uvprojx_parent_dir> -o . -t <target_name> --fix-placement --scan-hidden-macros
```

`--fix-placement` belongs in the default command, not in a second run: Keil's
standard layout puts the output in `Proj/` and the sources in a sibling
`Code/`, so the placement problem is the norm, not the exception. When the
placement is already fine the flag does nothing.

`--scan-hidden-macros` performs step 3a/3d below.

The exe for later re-anchoring is placed at the project root automatically;
`--no-exe` skips it, `--exe-dest` picks a different directory.

Flag any warnings: empty project macros, MISSING include paths, Keil not found.

### 2b. Understand the two data sources (.uvprojx vs .dep)

- **`.uvprojx` (XML)** is the live source of truth for **macros (`-D`) and
  project include paths (`-I`)** — edit in Keil and it takes effect at once.
- **`.dep`** is written by Keil **after a build** and supplies only what XML
  cannot: compiler **system headers**, **preinclude** headers (`-imacros`), and
  the **real compiled file list**. Used automatically when fresh.
- **Staleness guard:** if `.uvprojx` is newer than `.dep`, the script prints
  `.dep: STALE ... ignored` and falls back to XML-only. Expected after a macro
  edit — rebuild, or ignore since macros come from XML anyway.
- Read the `.dep:` log line. `--no-dep` forces XML-only, `--dep-path` points at
  a non-standard output dir.

### 3. Validate macros (CRITICAL)

**3a / 3d. Cross-target and hidden macros — run `--scan-hidden-macros`.** Do not
grep and tally by hand; this is a set difference over hundreds of files and two
macro sets, and a manual pass is neither fast nor reproducible. The scan reads
the generated `compile_commands.json` for its file list and splits every macro
the code branches on into:

- **defined by some target or the compiler** — resolved, silent;
- **`#define`d by the sources themselves** (a chip-family macro,
  `FL_*_DRIVER_ENABLED`, project switches living in a header) — listed, no action;
- **UNRESOLVED** — tested but defined nowhere. Those branches are inactive in
  every build. Confirm with the user that this is intended.

The middle bucket is the one hand-grepping gets wrong: a macro can be absent
from every Keil target and still be **active**, because a header defines it.
Never call a macro "dead in all builds" from an `#ifdef` grep alone.

Include guards and language probes (`__cplusplus`, `__STDC__`, …) are filtered
out; without that the report is mostly noise.

**3b. Compiler macros (auto-added)** — ARMCC v5 (uAC6=0) needs `__CC_ARM`,
`__arm__`, arch define; ARM Clang v6 (uAC6=1) needs `__ARMCC_VERSION=6000000`,
`__arm__`, arch define. Arch must match CPU: M0 → `__ARM_ARCH_6M__`,
M3 → `__ARM_ARCH_7M__`, M4/M7 → `__ARM_ARCH_7EM__`.

### 4. Read the self-check — it already ran

Checking each `-I`, each source file and each `-D` by hand used to be three
sections of this document. It is now a check the script runs on its own output
after every write, because a manual pass leaves no trace when it is skipped —
and it was skipped. Both backends end with:

```
verify: OK -- 12 include path(s), 0 missing; 56 source file(s), 0 missing; 31 macro(s), consistent across both files
```

or a list of what is wrong. What it reads back off disk:

| Check | Severity | Why |
|---|---|---|
| every `-I` directory exists | warning | a toolchain missing on this machine is an honest cause |
| every `file` entry exists | warning | a `.dep` can list a since-deleted source |
| `directory` is an existing **absolute** path | **error** | clangd refuses a relative anchor outright |
| `.clangd` and `compile_commands.json` agree on `-D` | **error** | the two files disagreeing can never be legitimate |

Errors exit **3**. Warnings only print — pass `--verify-strict` to fail on them
too, `--no-verify` to skip the check entirely.

Your job is to **read the report and act on it**, not to redo it. Missing Keil
Pack paths are the common warning: scan `{keil}/ARM/PACK/{vendor}/{pack}/` for
the installed versions and correct the version in the path.

---

# IAR (.ewp)

Works with **any** IAR architecture. The compiler settings node is found by its
`ICC` prefix, so ICCARM, ICCRL78, ICCRX, ICC430 and friends all parse.

> The retired `Ewp2Json.py` matched a node named literally `ICCARM`. On any
> other architecture it parsed zero macros and zero include paths and still
> emitted a full compile_commands.json, so the output looked fine and was
> useless. It now forwards here.

## What makes the IAR backend different: it asks the compiler

Instead of hard-coding a macro table, the script runs the installed
`icc<arch>.exe` with `--predef_macros` and writes the result into a generated
preinclude header, wired up with `-imacros`. The macro set therefore always
matches the installed compiler version and options (300+ macros for RL78,
including `__ICCRL78__`, `__CORE__`, `__DATA_MODEL__`).

Things derived from that probe rather than guessed:

- **char signedness** — read from `__CHAR_MIN__`, emitted as
  `-funsigned-char`/`-fsigned-char`.
- **`--target` triple** — chosen by `__DEF_PTR_SIZE__`. A target is always
  emitted: with none, clang falls back to the host triple, and on Windows that
  is an MSVC triple whose predeclared `size_t` collides with IAR's target-sized
  one. Architectures clang has no backend for (RL78, RX, STM8) get a
  size-matched stand-in, and the stand-in's identity macros (`__MSP430__`, …)
  plus clang's own (`__GNUC__`, `__clang__`) are `#undef`'d in the generated
  header so code testing them is not fooled.
- **`--core`** — see below.

## Core negotiation

`.ewp` stores only the IDE dropdown *index* (`IccCore`), whose meaning varies by
architecture and workbench version. Instead of mapping it, the script uses the
compiler as the oracle: it compiles a TU that includes the project's device
header (derived from `GenDeviceSelect`, e.g. `R5F10WMG` → `ior5f10wmg.h`), first
with default options and then with each candidate core, keeping whichever is
accepted. Candidates come from the probe's own `__<ARCH>_<n>__` macros.

Getting this wrong is not subtle — device headers carry
`#error "... for use with ICCRL78 option --core rl78_1 only"`. Disable with
`--no-core-probe`, override with `--probe-args="--core s2"`.

## Extended keywords

`__near`, `__saddr`, `__interrupt`, `__no_init`, `__root`, … are language
extensions, not macros, so they never appear in `--predef_macros`. The generated
header shims them (`__weak` → `__attribute__((weak))`, most to nothing).

## Steps

### 1. List the build configurations

```powershell
py -3 "${CLAUDE_PLUGIN_ROOT}/scripts/Iar2Clangd.py" -p <dir> --list-configs
```

Configurations usually differ in macros the same way Keil targets do (`Debug` =
`_CODE_DEBUG_` vs `Releas_004` = `DEF_004`). The script prints a
cross-configuration table and warns about macros present elsewhere but not in
the one that would be used.

The same refusals as Keil apply, for the same reason:

| Situation | Exit | Way out |
|---|---|---|
| Several `.ewp` under `-p` | 1 | `--project <path>` |
| Several configurations and no `-c` (when writing) | 2 | `-c <name>` |

`--list-configs` and `--dry-run` are exempt; `--use-first-config` is the
unattended escape hatch.

### 2. Run

```powershell
py -3 "${CLAUDE_PLUGIN_ROOT}/scripts/Iar2Clangd.py" -p <dir> -o <output_dir> -c <configuration> --fix-placement --scan-hidden-macros
```

Same reasoning as Keil: `--fix-placement` is a no-op when the placement is
already fine, so it belongs in the default command rather than a second run.
No re-anchor exe is placed for IAR projects — ReAnchor does not understand IAR
layouts, and `Iar2Clangd.py` re-probes everything anyway, so a moved IAR
project is regenerated instead.

Review the report: unresolved `$VAR$` in include paths, MISSING directories,
whether the probe succeeded, which core was negotiated, which triple was picked.

### 3. Validate

The same self-check runs here (Keil step 4), plus these to read for yourself:

- `-nostdinc` is emitted whenever IAR's own headers were found, so the standard
  library comes from the toolchain rather than the host. If IAR is **not**
  found, that flag is omitted — check the report and pass `--iar-path`.
- The `--dlib_config` from the project's `GenRTConfigPath` is fed to the probe,
  so `_DLIB_CONFIG_FILE_HEADER_NAME` matches the real build. `<toolkit>/lib` is
  added to the include path because that config header lives there.

## Known limitation: SFR names in vendor device headers

IAR device headers declare special function registers with two vendor
extensions at once:

```c
__saddr __no_init volatile union { unsigned char P0; __BITS8 P0_bit; } @ 0xFFF00;
```

clang cannot parse the `@ address` placement syntax, and even with it removed a
**file-scope anonymous union does not export its members** in C (verified: also
not under `-fms-extensions`). So SFR names do not resolve.

Mitigation in place: `-ferror-limit=0` is always emitted. Without it the first
19 parse errors abort the whole translation unit and *nothing* gets indexed;
with it the damage stays inside the vendor header. clangd also collapses
included-file errors, so the editor shows one squiggle at the `#include`, not
hundreds.

Measured on a 56-file RL78 project: 33 files (59%) clean apart from that single
header squiggle, 23 files (41%) — the BSP/register layer — still report
`use of undeclared identifier` for SFR names.

Fixing it properly means generating named `extern` declarations plus member
`#define`s from the device header and pre-defining its include guard so the
original body is skipped. Not implemented.

---

# CMake

CMake already emits a `compile_commands.json`; re-deriving one would only
produce a worse copy. What CMake does *not* do is make it findable — see the
placement section below, which is the entire reason this backend exists.

```powershell
py -3 "${CLAUDE_PLUGIN_ROOT}/scripts/Cmake2Clangd.py" -p <project_dir>
```

It runs `cmake -S <root> -B <build> -DCMAKE_EXPORT_COMPILE_COMMANDS=ON`,
preferring `-G Ninja` (multi-config IDE generators — Visual Studio, Xcode — do
not honour that switch and are refused up front), then locates the database,
checks it covers real files, and drops a pointer `.clangd` where clangd will
find it.

- `--no-configure` consumes an existing database instead of running cmake.
- `-b/--build-dir`, `-G/--generator`, `--cmake-args` pass through.
- If configure dies in CMake's **compiler check** — normal for a cross
  toolchain, or a host clang with no MSVC/SDK libraries — the script says so and
  suggests `--cmake-args="-DCMAKE_TRY_COMPILE_TARGET_TYPE=STATIC_LIBRARY"`,
  which makes that check compile-only.
- If the database's compiler is a cross driver (`arm-none-eabi-gcc`, …), the
  script says so and prints the `CompileFlags.Compiler` + `--query-driver`
  incantation. It does not add them itself.

---

# Config discovery placement — applies to ALL backends (CRITICAL)

Validating content is not enough: clangd must be able to *find* the files.
**clangd searches a source file's own directory and its ANCESTOR directories
only — never sibling directories.**

The trap: output lands in the `Proj`/`build` folder while sources live in a
*sibling* dir (`Code`, `src`). The symptom is deceptive — **same-file navigation
still works, but cross-file jump-to-definition and find-references silently
fail**, because those need the background index, which needs the database.

All three backends now check this automatically and print
`placement: OK` or `placement: PROBLEM`. On PROBLEM, `--fix-placement`
(Keil/IAR) or the default behaviour (CMake) writes a pointer `.clangd` at the
sources' deepest common ancestor:

```yaml
CompileFlags:
  CompilationDatabase: <relative-path-to-the-dir-holding-compile_commands.json>
```

The pointer carries the `Diagnostics`/`ClangTidy` blocks too, since the real
`.clangd` sits where clangd will never read it for those sources. Place it at
the tightest ancestor that covers the sources but not unrelated sibling
projects (e.g. `App/`, not the repo root, so a separate `Boot/` is unaffected).

---

# Fix issues found

Hand-editing `.clangd` alone will make the self-check report an ERROR next run,
because `compile_commands.json` still carries the old `-D` set. Either edit both
or regenerate.

- **Missing macros**: add `-D` to `.clangd` `CompileFlags.Add` *and* to the
  database's `arguments`.
- **Missing include paths**: remove or correct.
- **Wrong Pack version**: update the version in the path.
- **ARMCC syntax extensions** clangd rejects — add only if clangd complains:
  - `__packed` → `-D__packed=__attribute__((packed))`
  - `__align(n)` → `-D__align(n)=__attribute__((aligned(n)))`
  - `__weak` → `-D__weak=__attribute__((weak))`
- **Excessive errors from vendor headers**: add specific `Diagnostics.Suppress`
  entries.

Then tell the user what was generated, what was fixed, what still needs
attention, and to restart clangd: Ctrl+Shift+P → "clangd: Restart language
server".

---

# Project moved / new machine (re-anchor)

## Reuse or regenerate?

Only reachable when the user brought up an existing config — the project moved,
a new machine, a second worktree, "pull the config over from that other repo".
Two questions, then run the third check:

| Check | Reuse | Regenerate |
|---|---|---|
| Project file name (`.uvprojx`) | same | different |
| Target being generated for | same | different |
| `ReAnchor.py --dry-run` accepts the database | exit 0 | non-zero |

```powershell
py -3 "${CLAUDE_PLUGIN_ROOT}/scripts/ReAnchor.py" --root <new_root> --dry-run
```

That third check is not a judgement call: ReAnchor's ownership guard passes when
**at least 90 % of the `file` entries exist under the new root**
(`--ownership-threshold`, default 0.10 missing) and refuses otherwise.

- **All three pass** → copy `.clangd` + `compile_commands.json` over by hand,
  then re-anchor. Keil only. This preserves hand edits, which regenerating loses.
- **Any fails** → regenerate, and say why reuse was not possible.

ReAnchor rewrites *paths only* — never the file list, never `-D` macros. A
config from a **different project** cannot be fixed by re-anchoring however
similar the layout looks; the guard refuses it rather than producing a silently
useless index.

## Why paths break

Generated files contain machine/location-bound paths. Measured behavior
(clangd 22, Windows):

1. `compile_commands.json`'s `directory` MUST be a correct absolute path on the
   current machine — a relative value never works (clangd hard limit), and a
   stale absolute value only works while clangd's CWD happens to be the project
   root.
2. Relative `-I` in `.clangd` resolve against that `directory` anchor.
3. Absolute toolchain `-I` (e.g. `C:/Keil_v5/...`) break across machines — only
   re-probing can fix them.

```powershell
py -3 "${CLAUDE_PLUGIN_ROOT}/scripts/ReAnchor.py" --root <project_root>
# or double-click keil2clangd-reanchor.exe at the project root
# (build: scripts/build_exe.bat; needs no Python or plugin on the target machine)
```

**The exe lives at the project root**, and the Keil backend puts it there
automatically after a successful generation. It searches **downwards** from its
own directory, so one exe at the top of the repo fixes every config below it —
`Code/App/Proj/` and `Code/Boot/Proj/` in the same run, each anchored to its own
directory. Pointer `.clangd` files are recognised and left alone.

**Ship it INSIDE the project (commit it)** — the point of the exe is to run on
machines with no Python and no plugin, so it must travel with the project. If
the repo ignores `*.exe` the generator says so and offers the two ways out:

```powershell
git add -f keil2clangd-reanchor.exe     # track this one binary
# or add  !keil2clangd-reanchor.exe  to .gitignore
```

Leaving it untracked is a legitimate third choice — a 9 MB binary in a firmware
repo is a real cost. It is the user's call; do not edit `.gitignore` unasked.

**The exe is a build artifact and can lag the scripts.** `scripts/dist/` is
gitignored, so editing `ReAnchor.py` / `Keil2Clangd.py` / `k2c_common.py` /
`k2c_macroscan.py` does not rebuild anything — an old exe keeps shipping the old
behaviour and reports success while doing it.

Deployment answers two different questions with two different comparisons; they
are easy to confuse:

| Question | Compared by | Outcome |
|---|---|---|
| Is the built exe older than the scripts? | **mtime** | prints `WARNING the prebuilt exe is OUT OF DATE` |
| Does the copy in the project differ from the built one? | **content hash** | re-copies, or reports "already current" |

So a project copy is never judged by size or date — only by content. When you
see that WARNING, or after touching any of those four files, run
`scripts/build_exe.bat` before trusting the exe.

Behavior:
- Same machine, moved folder: fully automatic — rewrites `directory` only.
- New machine: probes Keil (`KEIL_PATH` → `~/.keil2clangd.json` → common
  locations → prompt, saved) and rewrites dead toolchain `-I`/`-imacros`.
  Pack-version mismatches are kept + warned — re-run this skill instead.
- Surgical: relative `-I`, `-D` macros, comments and AI-added lines survive
  byte-for-byte. Originals backed up to `*.bak`. `--dry-run` previews.
- **Ownership guard:** before writing anything it checks that the listed `file`
  entries actually exist under the new root. A database belonging to a
  *different* project is refused with a non-zero exit instead of being
  "successfully" re-anchored into a silently useless index. ReAnchor only ever
  rewrites paths — it cannot repair a wrong file list or wrong `-D` macros, so
  regenerate instead. `--force` overrides; `--ownership-threshold` tunes how
  many genuinely-deleted files are tolerated (default 10%).
- Out of scope: files generated with `-a`/`--absolute`, and project-local
  preinclude headers resolved to absolute paths. Regenerate instead.
- **IAR is not covered.** ReAnchor only knows Keil layouts; for a moved IAR
  project just re-run `Iar2Clangd.py`, which re-probes everything anyway.

Flags: `--root PATH`, `-k/--keil-path PATH`, `--dry-run`, `--force`,
`--ownership-threshold F`, `--max-depth N`, `--no-pause`.

---

# Common issues the scripts can't handle

| Issue | Symptom | Fix |
|-------|---------|-----|
| Keil Pack version mismatch | MISSING pack include path | Scan Pack dir, update path |
| Macros defined in a batch build | `#ifdef` on undefined macro | Ask user, add `-D` |
| ARMCC `__packed`/`__align` | clangd syntax errors | Add `-D` compat macros |
| Multiple targets/configurations | Refused, exit 2 | Ask the user, then `-t`/`-c` |
| Multiple project files under `-p` | Refused, exit 2 (Keil) / 1 (IAR) | `--project <path>` |
| `.clangd` and the database disagree on `-D` | `verify:` reports an ERROR, exit 3 | Regenerate; do not hand-patch one of the two |
| Vendor headers clangd can't parse | `fatal_too_many_errors` | `-ferror-limit=0` (IAR backend does this) |
| IAR SFR `@ address` declarations | `use of undeclared identifier 'P0'` | Not solved — see the IAR limitation section |
| Cross-drive paths (C: vs D:) | Relative path fails | Handled, but verify |
| Toolchain not found, run from a terminal | Prompted on first run | Enter path, saved to `~/.keil2clangd.json` |
| Toolchain not found, run by an agent or CI | No prompt — stdin is not a tty, so it says so and carries on | Pass `-k`/`--iar-path`, or pre-fill `~/.keil2clangd.json` |
| Output dir is a sibling of the sources | Same-file jump works, cross-file silently fails | `--fix-placement` (already in the default command) |
| CMake configured with a VS generator | No `compile_commands.json` at all | `-G Ninja` |
| Config copied in from another project | ReAnchor refuses: "does not belong to this project" | Regenerate; the file list and `-D` cannot be re-anchored |
| Repo ignores `*.exe` | Re-anchor exe stays untracked | `git add -f`, negate in `.gitignore`, or accept local-only |
| Exe behaves like an older version | Its `--help`/errors don't match `ReAnchor.py` | It is a stale build — `scripts/build_exe.bat`, then redeploy |

# Script options

`Proj2Clangd.py`
```
-p, --path PATH       Directory to search (default: current dir)
--kind {keil,iar,cmake}   Force a backend instead of detecting one
--detect-only         Report what was found and exit
<everything else>     Forwarded to the backend
```

`Keil2Clangd.py`
```
-p PATH  -o PATH  -a/--absolute  -t/--target-name NAME  -k/--keil-path PATH
--no-clangd  --no-compile-commands  --no-dep  --dep-path PATH
--project PATH         Explicit .uvprojx, skipping the search (required when
                       -p finds more than one)
--use-first-target     Take the first target in the XML instead of requiring -t
--fix-placement        Pointer .clangd when the output dir is not an ancestor
--scan-hidden-macros   Report macros the code tests that nothing defines
--no-verify            Skip the post-generation self-check (it runs by default)
--verify-strict        Fail on self-check warnings too, not just errors
--no-exe               Do not place keil2clangd-reanchor.exe at the project root
--exe-dest DIR         Put the exe somewhere other than the git repo root
--dry-run              Report everything, write nothing (honoured by every
                       writer, --fix-placement and the exe included)
```

`Iar2Clangd.py`
```
-p PATH  -o PATH  -a/--absolute  -c/--config NAME (alias -t/--target-name)
--project PATH        Explicit .ewp, skipping the search (required when -p
                      finds more than one)
--use-first-config    Take the first configuration in the XML instead of
                      requiring -c
--iar-path PATH       Workbench root, e.g. ".../Embedded Workbench 8.0"
--iar-target TRIPLE   Override the clang --target ('' omits it)
--no-probe            Do not run the compiler for predefined macros
--probe-args="..."    Extra probe options, e.g. --probe-args="--core s2"
--no-core-probe       Skip device-header core negotiation
--force-predef-header Write the preinclude header even with --no-probe
--scan-hidden-macros  Report macros the code tests that no configuration defines
--no-verify           Skip the post-generation self-check (it runs by default)
--verify-strict       Fail on self-check warnings too, not just errors
--list-configs  --no-clangd  --no-compile-commands  --fix-placement  --dry-run
```

`ReAnchor.py`
```
--root PATH            Search here AND below (default: exe dir / cwd)
-k/--keil-path PATH    Keil installation, skips the probe
--dry-run              Report without writing
--force                Re-anchor even when the file list does not match
--ownership-threshold F  Fraction of listed files allowed missing (default 0.10)
--max-depth N          How deep to search below the root (default 6)
--no-pause             Do not wait for Enter (frozen exe)
```

`Cmake2Clangd.py`
```
-p PATH  -b/--build-dir PATH  -G/--generator NAME  --cmake PATH
--cmake-args="..."  --no-configure  -o PATH (pointer .clangd location)
--no-clangd  --dry-run
```

**`--probe-args` and `--cmake-args` must use `=`.** Their values begin with a
dash, so the space form (`--cmake-args "-DFOO=BAR"`) makes argparse read the
value as another option and fail with "expected one argument".

# Config file

`~/.keil2clangd.json` — auto-created, shared by the backends:
```json
{
  "keil_path": "D:\\Keil_v5",
  "iar_path": "D:\\Software\\IAR Systems\\Embedded Workbench 8.0"
}
```

# Generated files

| File | Backend | Note |
|---|---|---|
| `.clangd` | all | Flags + diagnostics, or a pointer when placement fails |
| `compile_commands.json` | Keil, IAR | CMake writes its own into the build dir |
| `k2c_iar_predef.h` | IAR | Probed macros + keyword shims, referenced via a **relative** `-imacros` so re-anchoring survives |
| `keil2clangd-reanchor.exe` | Keil | Placed at the git repo root; `--no-exe` skips it. Not written for IAR — ReAnchor cannot read IAR layouts |
