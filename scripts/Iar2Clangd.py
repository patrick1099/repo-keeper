#!/usr/bin/env python3
"""Iar2Clangd - Generate .clangd and compile_commands.json from IAR .ewp files.

Works with any IAR Embedded Workbench architecture (ICCARM, ICCRL78, ICCRX,
ICC430, ...): the compiler node is found by prefix, not hard-coded.

Rather than guessing the compiler's built-in macros, this runs the real
``icc*.exe`` with ``--predef_macros`` and writes the result to a generated
preinclude header. That yields the exact macro set for the installed
toolchain, including architecture macros (``__ICCRL78__``, ``__CORE__``,
``__DATA_MODEL__``) that no static table could keep correct.

IAR *extended keywords* (``__near``, ``__saddr``, ``__interrupt``, ...) are not
macros and never appear in ``--predef_macros``, so a compatibility block that
neutralises them is appended to the same header.
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import k2c_common as common
import k2c_macroscan as macroscan
from toolname import TOOL_NAME


# A --target is not optional. Without one clang defaults to the host triple,
# and on Windows that is an MSVC triple whose predeclared size_t collides with
# the IAR standard headers' target-sized size_t.
#
# Architectures where clang has a genuinely matching backend:
TOOLCHAIN_TARGET_MAP = {
    "ARM": "arm-none-eabi",
    "RISCV": "riscv32-none-elf",
    "RISC-V": "riscv32-none-elf",
    "AVR": "avr",
    "430": "msp430-none-elf",
    "MSP430": "msp430-none-elf",
}

# Everything else (RL78, RX, STM8, 78K, ...) has no clang backend at all, so a
# stand-in is picked purely to get the type sizes right -- chosen by the
# pointer width the probe reported. The stand-in's own architecture identity
# macros are then #undef'd in the generated header, so code that tests them
# does not silently take a branch meant for a chip this project is not.
STANDIN_TRIPLES = {
    2: ("msp430-none-elf", ["__MSP430__", "__msp430__", "__MSP430", "MSP430"]),
    4: ("i386-none-elf", ["__i386__", "__i386", "i386",
                          "__SSE__", "__SSE2__", "__MMX__"]),
    8: ("x86_64-none-elf", ["__x86_64__", "__x86_64", "__amd64__", "__amd64"]),
}

# clang identifies itself as a GCC-alike; IAR does not. Left defined, code
# guarded on these takes the wrong branch.
ALWAYS_UNDEF = ["__GNUC__", "__GNUC_MINOR__", "__GNUC_PATCHLEVEL__", "__clang__",
                "__clang_major__", "__clang_minor__", "_MSC_VER", "_WIN32"]

IAR_ROOT_GLOBS = [
    "C:/Program Files/IAR Systems",
    "C:/Program Files (x86)/IAR Systems",
    "D:/IAR Systems",
    "D:/Software/IAR Systems",
    "E:/IAR Systems",
]

# IAR extended keywords, neutralised so clang can parse IAR headers and IAR
# style source. Union across architectures -- defining one the current
# toolchain does not have is harmless.
EXTENDED_KEYWORDS_EMPTY = [
    "__near", "__far", "__huge", "__saddr", "__sfr", "__tiny",
    "__near_func", "__far_func", "__callt", "__banked",
    "__interrupt", "__fast_interrupt", "__trap", "__nested", "__irq", "__fiq",
    "__root", "__no_init", "__monitor", "__ramfunc", "__task", "__swi",
    "__absolute", "__regvar", "__brel", "__nounwind", "__intrinsic",
    "__no_alloc", "__no_alloc16", "__no_alloc_str", "__no_alloc_str16",
    "__arm", "__thumb", "__big_endian", "__little_endian",
    "__cc_version1", "__cc_version2", "__spec_string", "__stackless",
]

EXTENDED_KEYWORDS_MAPPED = [
    ("__weak", "__attribute__((weak))"),
    ("__packed", "__attribute__((packed))"),
    ("__noreturn", "__attribute__((noreturn))"),
]

PREDEF_HEADER_NAME = "k2c_iar_predef.h"


# ---------------------------------------------------------------------------
# EwpParser
# ---------------------------------------------------------------------------

class EwpParser:
    """Parse an IAR .ewp project file for one configuration."""

    def __init__(self, file_path, config_name=None):
        self.file_path = Path(file_path).resolve()
        self.project_root = self.file_path.parent
        self.tree = ET.parse(str(self.file_path))
        self.root = self.tree.getroot()
        self.config_name = config_name
        self.config = self._find_config()
        self._argvars = self._load_custom_argvars()

    # -- configuration selection -------------------------------------------

    def _find_config(self):
        configs = self.root.findall('configuration')
        if not configs:
            raise ValueError("No <configuration> found in {0}".format(self.file_path))
        if self.config_name:
            for c in configs:
                name = c.find('name')
                if name is not None and name.text == self.config_name:
                    return c
            raise ValueError(
                "Configuration '{0}' not found. Available: {1}".format(
                    self.config_name, self.list_configs()))
        return configs[0]

    def list_configs(self):
        names = []
        for c in self.root.findall('configuration'):
            name = c.find('name')
            if name is not None and name.text:
                names.append(name.text)
        return names

    def get_config_name(self):
        name = self.config.find('name')
        return name.text if name is not None else "Unknown"

    @property
    def project_name(self):
        return self.file_path.stem

    # -- toolchain ----------------------------------------------------------

    def get_toolchain(self):
        """Toolchain name as IAR writes it, e.g. 'ARM', 'RL78', 'RX'."""
        elem = self.config.find('toolchain/name')
        if elem is not None and elem.text:
            return elem.text.strip()
        return None

    def _compiler_settings(self):
        """The <settings> node whose name starts with ICC, for any architecture."""
        for settings in self.config.findall('settings'):
            name = settings.find('name')
            if name is not None and name.text and name.text.strip().upper().startswith('ICC'):
                return settings
        return None

    def get_compiler_id(self):
        """Settings node name, e.g. 'ICCRL78'. None when the node is missing."""
        settings = self._compiler_settings()
        if settings is None:
            return None
        name = settings.find('name')
        return name.text.strip() if name is not None and name.text else None

    def _options(self):
        settings = self._compiler_settings()
        if settings is None:
            return {}
        result = {}
        for option in settings.iter('option'):
            name = option.find('name')
            if name is None or not name.text:
                continue
            states = [s.text.strip() for s in option.findall('state')
                      if s.text and s.text.strip()]
            result[name.text.strip()] = states
        return result

    # -- variable expansion -------------------------------------------------

    def _load_custom_argvars(self):
        """Read $CUSTOM$ variables from settings/*.custom_argvars, if any."""
        argvars = {}
        settings_dir = self.project_root / 'settings'
        if not settings_dir.is_dir():
            return argvars
        for f in settings_dir.glob('*.custom_argvars'):
            try:
                root = ET.parse(str(f)).getroot()
            except ET.ParseError:
                continue
            for var in root.iter('variable'):
                name = var.find('name')
                value = var.find('value')
                if name is not None and name.text and value is not None and value.text:
                    argvars[name.text.strip()] = value.text.strip()
        return argvars

    def expand(self, raw, toolkit_dir=None, ew_dir=None):
        """Expand IAR path variables and normalise separators."""
        text = raw.strip()
        if not text:
            return ""
        text = text.replace('$PROJ_DIR$', str(self.project_root))
        if toolkit_dir:
            text = text.replace('$TOOLKIT_DIR$', str(toolkit_dir))
        if ew_dir:
            text = text.replace('$EW_DIR$', str(ew_dir))
        for name, value in self._argvars.items():
            text = text.replace('$' + name + '$', value)
        return text.replace('\\', '/')

    def _resolve(self, raw, toolkit_dir=None, ew_dir=None):
        expanded = self.expand(raw, toolkit_dir, ew_dir)
        if not expanded:
            return None
        if re.search(r'\$[A-Za-z_]', expanded):
            # an unexpanded IAR variable remains -- caller reports it
            return expanded
        return Path(expanded).resolve()

    # -- build settings -----------------------------------------------------

    def get_defines(self):
        return list(self._options().get('CCDefines', []))

    def get_include_paths(self, toolkit_dir=None, ew_dir=None):
        """Project include paths, absolute, order-preserving, deduped."""
        paths = []
        seen = set()
        unresolved = []
        for raw in self._options().get('CCIncludePath2', []):
            resolved = self._resolve(raw, toolkit_dir, ew_dir)
            if resolved is None:
                continue
            if isinstance(resolved, str):
                unresolved.append(resolved)
                continue
            key = str(resolved).lower()
            if key not in seen:
                seen.add(key)
                paths.append(resolved)
        self.unresolved_includes = unresolved
        return paths

    def get_preinclude(self, toolkit_dir=None, ew_dir=None):
        """Project preinclude header (IAR --preinclude), or None."""
        for raw in self._options().get('PreInclude', []):
            resolved = self._resolve(raw, toolkit_dir, ew_dir)
            if isinstance(resolved, Path):
                return resolved
        return None

    def _general_options(self):
        for settings in self.config.findall('settings'):
            name = settings.find('name')
            if name is not None and name.text and name.text.strip() == 'General':
                result = {}
                for option in settings.iter('option'):
                    opt_name = option.find('name')
                    if opt_name is None or not opt_name.text:
                        continue
                    states = [s.text.strip() for s in option.findall('state')
                              if s.text and s.text.strip()]
                    result[opt_name.text.strip()] = states
                return result
        return {}

    def get_device(self):
        """Device part number, e.g. 'R5F10WMG'.

        IAR stores it as "<part>\t<family> - <part>"; only the first field is
        the part number.
        """
        states = self._general_options().get('GenDeviceSelect', [])
        if not states:
            return None
        return re.split(r'[\t\s]', states[0].strip(), maxsplit=1)[0] or None

    def get_dlib_config(self, toolkit_dir=None, ew_dir=None):
        """Runtime library configuration header (IAR --dlib_config), or None.

        Passing this to the probe matters: it decides which
        ``_DLIB_CONFIG_FILE_HEADER_NAME`` the predefined macros carry, and
        therefore which header the IAR standard headers pull in.
        """
        states = self._general_options().get('GenRTConfigPath', [])
        if not states:
            return None
        resolved = self._resolve(states[0], toolkit_dir, ew_dir)
        return resolved if isinstance(resolved, Path) else None

    def get_extra_options(self):
        """Raw extra compiler options, only when the project enables them."""
        options = self._options()
        enabled = options.get('IccUseExtraOptions', ['0'])
        if not enabled or enabled[0] != '1':
            return []
        return list(options.get('IccExtraOptions', []))

    # -- source files -------------------------------------------------------

    def get_source_files(self, include_headers=False):
        """Source files for the selected configuration, honouring exclusions."""
        config_name = self.get_config_name()
        files = []
        seen = set()
        for file_elem in self.root.iter('file'):
            if self._is_excluded(file_elem, config_name):
                continue
            name = file_elem.find('name')
            if name is None or not name.text:
                continue
            resolved = self._resolve(name.text)
            if not isinstance(resolved, Path):
                continue
            if not include_headers and resolved.suffix.lower() not in ('.c', '.cpp', '.cc', '.cxx'):
                continue
            key = str(resolved).lower()
            if key not in seen:
                seen.add(key)
                files.append(resolved)
        return files

    @staticmethod
    def _is_excluded(file_elem, config_name):
        excluded = file_elem.find('excluded')
        if excluded is None:
            return False
        for c in excluded.findall('configuration'):
            if c.text and c.text.strip() == config_name:
                return True
        return False

    def defines_by_config(self):
        """Map every configuration name to its define set (for cross-checks)."""
        result = {}
        for name in self.list_configs():
            result[name] = set(EwpParser(self.file_path, config_name=name).get_defines())
        return result


# ---------------------------------------------------------------------------
# IarPathResolver
# ---------------------------------------------------------------------------

class IarPathResolver:
    """Locate the IAR Embedded Workbench installation and its toolkit dir."""

    def __init__(self, iar_path=None, toolchain=None, compiler_id=None,
                 interactive=True):
        self.toolchain = toolchain
        self.compiler_id = compiler_id
        self.ew_root = None

        for candidate in self._candidates(iar_path):
            if candidate and Path(candidate).is_dir():
                self.ew_root = Path(candidate).resolve()
                break

        if self.ew_root is None:
            found = self._scan_common_locations()
            if found:
                self.ew_root = found
                common.config_set('iar_path', str(found))
                print("Found IAR at {0}, saved to {1}".format(found, common.CONFIG_FILE))
            elif interactive:
                self._prompt_and_save()

        self.toolkit_dir = self._find_toolkit_dir()

    @staticmethod
    def _candidates(iar_path):
        return [iar_path, os.environ.get('IAR_PATH'), common.config_get('iar_path')]

    @staticmethod
    def _scan_common_locations():
        """Newest 'Embedded Workbench N.N' under any known IAR Systems dir."""
        best = None
        for base in IAR_ROOT_GLOBS:
            base_path = Path(base)
            if not base_path.is_dir():
                continue
            for child in sorted(base_path.iterdir()):
                if child.is_dir() and child.name.lower().startswith('embedded workbench'):
                    best = child
        return best.resolve() if best else None

    def _prompt_and_save(self):
        if not common.stdin_is_interactive():
            print("IAR Embedded Workbench not found, and stdin is not a "
                  "terminal -- not prompting.")
            print("  Pass --iar-path, or set 'iar_path' in {0}."
                  .format(common.CONFIG_FILE))
            return
        print("IAR Embedded Workbench installation not found automatically.")
        print("Enter the workbench path "
              "(e.g. D:/Software/IAR Systems/Embedded Workbench 8.0):")
        try:
            answer = input("> ").strip()
        except EOFError:
            return
        if answer and Path(answer).is_dir():
            self.ew_root = Path(answer).resolve()
            common.config_set('iar_path', str(self.ew_root))
            print("Saved to {0}".format(common.CONFIG_FILE))
        else:
            print("WARNING: '{0}' is not a valid directory.".format(answer))

    def _find_toolkit_dir(self):
        """$TOOLKIT_DIR$ -- the architecture subdirectory of the workbench."""
        if self.ew_root is None or not self.toolchain:
            return None
        wanted = self.toolchain.lower().replace('-', '')
        direct = self.ew_root / wanted
        if direct.is_dir():
            return direct
        for child in self.ew_root.iterdir():
            if child.is_dir() and child.name.lower() == wanted:
                return child
        return None

    def found(self):
        return self.toolkit_dir is not None

    def get_system_includes(self):
        """Toolchain system header directories that actually exist."""
        if not self.found():
            return []
        candidates = [
            self.toolkit_dir / 'inc',
            self.toolkit_dir / 'inc' / 'c',
            self.toolkit_dir / 'inc' / 'dlib' / 'c',
            # DLib_Config_*.h and the per-project dlib config header live here
            # on the older architecture toolchains (RL78, RX, 430).
            self.toolkit_dir / 'lib',
        ]
        existing = []
        for c in candidates:
            if not c.is_dir():
                continue
            if c.name == 'lib' and not any(c.glob('*.h')):
                continue
            existing.append(c)
        return existing

    def get_compiler_exe(self):
        """Path to icc<arch>.exe, derived from the .ewp settings node name."""
        if not self.found():
            return None
        stem = (self.compiler_id or ('icc' + (self.toolchain or ''))).lower()
        for name in (stem + '.exe', stem):
            candidate = self.toolkit_dir / 'bin' / name
            if candidate.is_file():
                return candidate
        return None


# ---------------------------------------------------------------------------
# Predefined-macro probe
# ---------------------------------------------------------------------------

_DEFINE_RE = re.compile(r'^#define\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)')


class PredefProbe:
    """Run the real IAR compiler to capture its predefined macros.

    Beats any static table: the macro set follows the installed compiler
    version and the architecture options actually passed.
    """

    def __init__(self, compiler_exe, extra_args=()):
        self.compiler_exe = Path(compiler_exe) if compiler_exe else None
        self.extra_args = list(extra_args)
        self.macros = []          # raw '#define ...' lines
        self.error = None

    def run(self):
        if self.compiler_exe is None or not self.compiler_exe.is_file():
            self.error = "IAR compiler executable not found"
            return self
        workdir = tempfile.mkdtemp(prefix='k2c_iar_')
        try:
            source = Path(workdir) / 'k2c_probe.c'
            source.write_text("void k2c_probe(void) {}\n", encoding='utf-8')
            out = Path(workdir) / 'predef.txt'
            cmd = [str(self.compiler_exe), str(source),
                   '--predef_macros', str(out),
                   '-o', str(Path(workdir) / 'k2c_probe.o')] + self.extra_args
            proc = subprocess.run(cmd, capture_output=True, timeout=120)
            if not out.is_file():
                stderr = (proc.stderr or b'').decode('utf-8', 'replace').strip()
                stdout = (proc.stdout or b'').decode('utf-8', 'replace').strip()
                self.error = "probe failed (exit {0}): {1}".format(
                    proc.returncode, stderr or stdout or 'no output')
                return self
            text = out.read_text(encoding='utf-8', errors='replace')
            self.macros = [line.rstrip() for line in text.splitlines()
                           if line.startswith('#define')]
        except (OSError, subprocess.SubprocessError) as exc:
            self.error = "probe failed: {0}".format(exc)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
        return self

    @property
    def ok(self):
        return bool(self.macros)

    def macro_value(self, name):
        """Value text of a probed object-like macro, or None."""
        prefix = '#define ' + name + ' '
        for line in self.macros:
            if line.startswith(prefix):
                return line[len(prefix):].strip()
        return None

    def defined_names(self):
        names = set()
        for line in self.macros:
            match = _DEFINE_RE.match(line)
            if match:
                names.add(match.group('name'))
        return names

    def char_is_unsigned(self):
        """Derive char signedness from the probe instead of guessing."""
        char_min = self.macro_value('__CHAR_MIN__')
        if char_min is None:
            return None
        return char_min.strip() == '0'


class CoreNegotiator:
    """Find the ``--core`` value the project's device header demands.

    The .ewp only stores the IDE dropdown *index* (``IccCore``), and that index
    means different things per architecture and per workbench version, so
    mapping it statically would be a guess. Instead the compiler is used as the
    oracle: try to compile a translation unit that includes the device header,
    first with the current options and then with each candidate core, and keep
    whichever the compiler accepts.

    Candidate cores are read back out of the probe's own macros (``__RL78_0__``,
    ``__RL78_1__``, ...), so no per-architecture table is needed either.
    """

    def __init__(self, compiler_exe, toolchain, device, base_args=()):
        self.compiler_exe = Path(compiler_exe) if compiler_exe else None
        self.toolchain = toolchain
        self.device = device
        self.base_args = list(base_args)
        self.chosen = None       # e.g. 'rl78_1'
        self.status = "skipped"
        self.detail = ""

    def device_header(self):
        if not self.device:
            return None
        return "io{0}.h".format(self.device.lower())

    @staticmethod
    def candidates_from(probe, toolchain):
        if probe is None or not probe.ok or not toolchain:
            return []
        pattern = re.compile(r'^__({0}_\d+)__$'.format(re.escape(toolchain.upper())))
        found = []
        for name in sorted(probe.defined_names()):
            match = pattern.match(name)
            if match:
                found.append(match.group(1).lower())
        return found

    def _compiles(self, extra_args, header):
        workdir = tempfile.mkdtemp(prefix='k2c_core_')
        try:
            source = Path(workdir) / 'k2c_core.c'
            source.write_text("#include <{0}>\nvoid k2c_core(void) {{}}\n".format(header),
                              encoding='utf-8')
            cmd = ([str(self.compiler_exe), str(source),
                    '-o', str(Path(workdir) / 'k2c_core.o')]
                   + self.base_args + list(extra_args))
            proc = subprocess.run(cmd, capture_output=True, timeout=120)
            return proc.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    def run(self, candidates):
        header = self.device_header()
        if self.compiler_exe is None or not header:
            self.detail = "no device declared in the project" if not header else "no compiler"
            return self
        if any(a.startswith('--core') for a in self.base_args):
            self.status = "overridden"
            self.detail = "--core supplied via --probe-args"
            return self

        if self._compiles([], header):
            self.status = "default-ok"
            self.detail = "{0} accepts the compiler's default core".format(header)
            return self

        for candidate in candidates:
            if self._compiles(['--core', candidate], header):
                self.chosen = candidate
                self.status = "negotiated"
                self.detail = "{0} requires --core {1}".format(header, candidate)
                return self

        self.status = "failed"
        self.detail = ("{0} rejected every candidate core ({1})"
                       .format(header, ', '.join(candidates) or 'none found'))
        return self


def choose_target(toolchain, probe, override=None):
    """Pick the clang --target triple, and the identity macros it needs undone.

    Returns ``(triple_or_None, [macro_to_undef, ...])``.
    """
    if override is not None:
        return (override or None), list(ALWAYS_UNDEF)

    genuine = TOOLCHAIN_TARGET_MAP.get((toolchain or '').upper())
    if genuine:
        return genuine, list(ALWAYS_UNDEF)

    width = None
    if probe is not None and probe.ok:
        for name in ('__DEF_PTR_SIZE__', '__INT_SIZE__'):
            value = probe.macro_value(name)
            if value and value.strip().isdigit():
                width = int(value.strip())
                break
    triple, undefs = STANDIN_TRIPLES.get(width, STANDIN_TRIPLES[4])
    return triple, undefs + ALWAYS_UNDEF


def render_predef_header(probe, toolchain, compiler_id, undef_macros=()):
    """Build the generated preinclude header: probed macros + keyword shims."""
    lines = [
        "/* Generated by {0} (Iar2Clangd.py) -- do not edit.".format(TOOL_NAME),
        " *",
        " * Fed to clangd via -imacros. Two parts:",
        " *   1. the IAR compiler's own predefined macros, captured with",
        " *      --predef_macros from the installed toolchain;",
        " *   2. shims neutralising IAR extended keywords, which are language",
        " *      extensions rather than macros and so never appear in part 1.",
        " *",
        " * Toolchain: {0}   Compiler node: {1}".format(toolchain or '?', compiler_id or '?'),
        " */",
        "#ifndef K2C_IAR_PREDEF_H",
        "#define K2C_IAR_PREDEF_H",
        "",
    ]

    if undef_macros:
        lines.append("/* --- 0. drop identity macros clang predefines but IAR does not --- */")
        for macro in undef_macros:
            lines.append("#undef {0}".format(macro))
        lines.append("")

    if probe is not None and probe.ok:
        lines.append("/* --- 1. predefined macros ({0}) --- */".format(len(probe.macros)))
        lines += probe.macros
        already = probe.defined_names()
    else:
        reason = probe.error if probe is not None else "probe skipped"
        lines.append("/* --- 1. predefined macros: UNAVAILABLE ({0}) --- */".format(reason))
        lines.append("/* Minimal fallback so IAR headers at least identify the vendor. */")
        fallback = ["#define __IAR_SYSTEMS_ICC__ 9"]
        if compiler_id:
            fallback.append("#define __{0}__ 1".format(compiler_id.upper()))
        lines += fallback
        already = set()

    lines += ["", "/* --- 2. extended-keyword shims --- */"]
    for keyword in EXTENDED_KEYWORDS_EMPTY:
        if keyword in already:
            continue
        lines += ["#ifndef {0}".format(keyword),
                  "#define {0}".format(keyword),
                  "#endif"]
    for keyword, expansion in EXTENDED_KEYWORDS_MAPPED:
        if keyword in already:
            continue
        lines += ["#ifndef {0}".format(keyword),
                  "#define {0} {1}".format(keyword, expansion),
                  "#endif"]

    lines += ["", "#endif /* K2C_IAR_PREDEF_H */", ""]
    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# Build-flag assembly
# ---------------------------------------------------------------------------

class IarFlags:
    """Everything needed to emit both artifacts, computed once."""

    def __init__(self, parser, resolver, probe, predef_header, base_dir,
                 use_absolute=False, triple=None):
        self.parser = parser
        self.resolver = resolver
        self.probe = probe
        self.predef_header = predef_header
        self.base_dir = Path(base_dir).resolve()
        self.use_absolute = use_absolute
        self.triple = triple

    def _fmt(self, path):
        return common.format_path(path, self.base_dir, self.use_absolute)

    def groups(self):
        """Ordered (comment, flags) groups shared by both artifacts."""
        groups = []

        if self.triple:
            groups.append(("Target for {0}".format(self.parser.get_toolchain()),
                           ["--target={0}".format(self.triple)]))

        # IAR device headers declare SFRs with the vendor '@ address' placement
        # syntax, which clang cannot parse. Without lifting the error limit the
        # first 19 of those abort the parse and NOTHING in the file gets
        # indexed; with it lifted the errors stay confined to the vendor header.
        groups.append(("Keep vendor-header syntax errors from aborting the parse",
                       ["-ferror-limit=0"]))

        if self.probe is not None and self.probe.char_is_unsigned() is not None:
            flag = "-funsigned-char" if self.probe.char_is_unsigned() else "-fsigned-char"
            groups.append(("char signedness, read from the compiler probe", [flag]))

        if self.predef_header is not None:
            groups.append(
                ("IAR predefined macros + extended-keyword shims (generated)",
                 ["-imacros", self._fmt(self.predef_header)]))

        defines = self.parser.get_defines()
        if defines:
            groups.append(("IAR project macros ({0})".format(self.parser.get_config_name()),
                           ["-D{0}".format(d) for d in defines]))

        toolkit = self.resolver.toolkit_dir
        ew_root = self.resolver.ew_root
        includes = self.parser.get_include_paths(toolkit, ew_root)
        if includes:
            groups.append(("Project include paths",
                           ["-I{0}".format(self._fmt(p)) for p in includes]))

        system_includes = self.resolver.get_system_includes()
        if system_includes:
            # The IAR toolchain ships its own standard library headers, sized
            # for the target (16-bit size_t on RL78). Left to itself clang also
            # searches the host system dirs *and* its own builtin dir, and the
            # two size_t typedefs then collide. Cut both out -- but only once
            # IAR's headers are actually available to replace them.
            groups.append(("Use IAR's standard headers, not clang's or the host's",
                           ["-nostdinc"]))
            groups.append(("IAR toolchain system headers",
                           ["-I{0}".format(self._fmt(p)) for p in system_includes]))

        preinclude = self.parser.get_preinclude(toolkit, ew_root)
        if preinclude is not None:
            groups.append(("Project preinclude header (IAR --preinclude)",
                           ["-imacros", self._fmt(preinclude)]))

        return groups

    def flat_args(self):
        args = []
        for _, flags in self.groups():
            args += flags
        return args


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def report(parser, resolver, probe, negotiator=None):
    """Print what was parsed and what could not be resolved."""
    toolkit = resolver.toolkit_dir
    ew_root = resolver.ew_root
    includes = parser.get_include_paths(toolkit, ew_root)
    defines = parser.get_defines()
    sources = parser.get_source_files()

    print("=" * 60)
    print("  Project:       {0}".format(parser.file_path))
    print("  Configuration: {0}".format(parser.get_config_name()))
    print("  Toolchain:     {0}   (compiler node: {1})".format(
        parser.get_toolchain(), parser.get_compiler_id()))
    print("  Source files:  {0}".format(len(sources)))
    print("=" * 60)

    print("\n[Project macros] ({0} found)".format(len(defines)))
    if defines:
        for d in defines:
            print("  -D{0}".format(d))
    else:
        print("  WARNING: no project macros found for this configuration!")

    print("\n[Project include paths] ({0})".format(len(includes)))
    for p in includes:
        print("  [{0}] {1}".format("OK" if p.is_dir() else "MISSING", p))
    for raw in getattr(parser, 'unresolved_includes', []):
        print("  [UNRESOLVED VAR] {0}".format(raw))

    if resolver.found():
        print("\n[IAR installation] {0}".format(resolver.ew_root))
        print("  $TOOLKIT_DIR$: {0}".format(toolkit))
        exe = resolver.get_compiler_exe()
        print("  compiler:      {0}".format(exe or "NOT FOUND"))
        system_includes = resolver.get_system_includes()
        print("[IAR system include paths] ({0})".format(len(system_includes)))
        for p in system_includes:
            print("  [OK] {0}".format(p))
    else:
        print("\n[IAR installation] NOT FOUND"
              " -- toolchain headers and predefined macros are unavailable.")
        print("  Pass --iar-path, or set IAR_PATH, to fix.")

    if probe is None:
        print("\n[Predefined macros] skipped (--no-probe)")
    elif probe.ok:
        print("\n[Predefined macros] {0} captured from {1}".format(
            len(probe.macros), probe.compiler_exe.name))
        for name in ('__VERSION__', '__CORE__', '__DATA_MODEL__', '__CODE_MODEL__',
                     '__INT_SIZE__', '__CHAR_MIN__'):
            value = probe.macro_value(name)
            if value is not None:
                print("  {0} = {1}".format(name, value))
        if negotiator is not None and negotiator.status != "skipped":
            print("  core: {0} -- {1}".format(negotiator.status, negotiator.detail))
            if negotiator.status == "failed":
                print("  Pick one manually: --probe-args \"--core <value>\"")
        triple, _ = choose_target(parser.get_toolchain(), probe)
        genuine = TOOLCHAIN_TARGET_MAP.get((parser.get_toolchain() or '').upper())
        print("  clang --target: {0}{1}".format(
            triple, "" if genuine else "  (size-matched stand-in; clang has no "
                                       "backend for this architecture)"))
        print("  NOTE: everything except the core comes from the compiler's DEFAULT")
        print("  options. If the IDE sets a non-default data/code model, pass it:")
        print("    --probe-args \"--data_model far\"")
    else:
        print("\n[Predefined macros] FAILED: {0}".format(probe.error))
        print("  Falling back to a minimal macro set; expect unresolved IAR headers.")

    configs = parser.list_configs()
    if len(configs) > 1:
        selected = parser.get_config_name()
        by_config = parser.defines_by_config()
        print("\n[All configurations] ({0})".format(len(configs)))
        # Say what the marker actually means: without -c it is the first
        # Configuration element in the .ewp, not the one selected in the IDE.
        if parser.config_name is not None:
            marker_text = " <-- chosen by -c"
        else:
            marker_text = " <-- default: FIRST IN XML (not the IDE's selection)"
        for name in configs:
            marker = marker_text if name == selected else ""
            macros = ", ".join(sorted(by_config[name])) or "(none)"
            print("  {0}{1}".format(name, marker))
            print("    Macros: {0}".format(macros))
        others = set()
        for name, macros in by_config.items():
            if name != selected:
                others |= macros
        missing = others - by_config.get(selected, set())
        if missing:
            print("\n[WARN] Macros in other configurations but NOT in '{0}':".format(selected))
            for macro in sorted(missing):
                sources_of = [n for n, m in by_config.items()
                              if macro in m and n != selected]
                print("  -D{0}  (from: {1})".format(macro, ', '.join(sources_of)))

        if parser.config_name is None:
            print("\n[ACTION REQUIRED] No -c was given. Ask which configuration "
                  "the user actually builds, then re-run with -c <name>.")

    print()
    return known_macro_names(parser, probe)


def known_macro_names(parser, probe):
    """Every macro name defined by any configuration or by the compiler itself.

    The probe contributes the 300+ predefined macros the real compiler reports,
    so an architecture macro the code tests never shows up as "unresolved".
    """
    known = set()
    for macros in parser.defines_by_config().values():
        known |= macroscan.macro_names(macros)
    known |= macroscan.macro_names(parser.get_defines())
    if probe is not None and probe.ok:
        known |= macroscan.names_from_define_lines(probe.macros)
    return known


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------

def generate(parser, resolver, args):
    output_dir = Path(args.output).resolve()
    if not args.dry_run:
        output_dir.mkdir(parents=True, exist_ok=True)

    probe = None
    negotiator = None
    if not args.no_probe:
        compiler_exe = resolver.get_compiler_exe()
        probe_args = _split_probe_args(args.probe_args)

        dlib_config = parser.get_dlib_config(resolver.toolkit_dir, resolver.ew_root)
        if dlib_config is not None and dlib_config.is_file() \
                and not any(a.startswith('--dlib_config') for a in probe_args):
            probe_args = probe_args + ['--dlib_config', str(dlib_config)]

        probe = PredefProbe(compiler_exe, probe_args).run()

        if probe.ok and not args.no_core_probe:
            toolchain = parser.get_toolchain()
            negotiator = CoreNegotiator(compiler_exe, toolchain,
                                        parser.get_device(), probe_args)
            negotiator.run(CoreNegotiator.candidates_from(probe, toolchain))
            if negotiator.chosen:
                # Re-probe: the core changes the predefined macro set.
                probe = PredefProbe(
                    compiler_exe, probe_args + ['--core', negotiator.chosen]).run()

    known_macros = report(parser, resolver, probe, negotiator)

    sources = parser.get_source_files()

    if args.scan_hidden_macros:
        # Headers carry most of the interesting #ifdefs, so scan them too even
        # though they are not compilation units.
        macroscan.report(parser.get_source_files(include_headers=True),
                         known_macros, base_dir=parser.project_root)

    placement = common.check_placement(output_dir, sources)
    print(placement.describe())
    if not placement.ok and args.fix_placement and placement.anchor:
        common.write_pointer_clangd(output_dir, placement.anchor,
                                    dry_run=args.dry_run)
    elif not placement.ok:
        print("  (re-run with --fix-placement to write that pointer automatically)")
    print()

    triple, undefs = choose_target(parser.get_toolchain(), probe, args.iar_target)

    predef_header = None
    if not args.no_probe or args.force_predef_header:
        predef_header = output_dir / PREDEF_HEADER_NAME
        if args.dry_run:
            print("Would generate: {0}".format(predef_header))
        else:
            predef_header.write_text(
                render_predef_header(probe, parser.get_toolchain(),
                                     parser.get_compiler_id(), undefs),
                encoding='utf-8')
            print("Generated: {0}".format(predef_header))

    flags = IarFlags(parser, resolver, probe, predef_header, output_dir,
                     use_absolute=args.absolute, triple=triple)

    if not args.no_clangd:
        doc = common.ClangdDoc()
        for comment, group_flags in flags.groups():
            doc.add_group(comment, group_flags)
        doc.write(output_dir, dry_run=args.dry_run)

    if not args.no_compile_commands:
        entries = common.make_compile_entries(
            "arm-none-eabi-gcc" if (parser.get_toolchain() or '').upper() == 'ARM' else "clang",
            flags.flat_args(), sources, output_dir, use_absolute=args.absolute)
        common.write_compile_commands(entries, output_dir, dry_run=args.dry_run)

    # No re-anchor exe here on purpose: ReAnchor only understands Keil layouts.
    # A moved IAR project is re-generated instead -- Iar2Clangd re-probes the
    # compiler anyway, so re-anchoring would buy nothing.

    if args.dry_run:
        print("--dry-run: no files written.")
        return 0

    return common.run_verify(output_dir, no_verify=args.no_verify,
                             strict=args.verify_strict)


def _split_probe_args(raw):
    if not raw:
        return []
    import shlex
    return shlex.split(raw)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_arg_parser():
    ap = argparse.ArgumentParser(
        description="Generate .clangd and compile_commands.json from an IAR .ewp")
    ap.add_argument('-p', '--path', default='.',
                    help='Search path for the .ewp file (default: current dir)')
    ap.add_argument('--project', default=None,
                    help='Explicit .ewp path, skipping the search')
    ap.add_argument('-c', '--config', '-t', '--target-name', dest='config',
                    default=None,
                    help='Build configuration to generate for (IAR calls these '
                         'configurations; -t is accepted for symmetry with Keil)')
    ap.add_argument('--use-first-config', '--use-first-target',
                    dest='use_first_config', action='store_true',
                    help='Accept the first configuration in the XML instead of '
                         'requiring -c. Only for unattended runs that truly do '
                         'not care which build configuration is indexed')
    ap.add_argument('-a', '--absolute', action='store_true',
                    help='Use absolute paths in generated files')
    ap.add_argument('-o', '--output', default='.',
                    help='Output directory (default: current dir)')
    ap.add_argument('--iar-path', default=None,
                    help='IAR Embedded Workbench path '
                         '(e.g. "D:/Software/IAR Systems/Embedded Workbench 8.0")')
    ap.add_argument('--iar-target', default=None,
                    help="Override the clang --target triple; pass '' to omit it")
    ap.add_argument('--no-probe', action='store_true',
                    help='Do not run the IAR compiler to capture predefined macros')
    ap.add_argument('--probe-args', default=None,
                    help='Extra options for the probe. The value starts with a '
                         'dash, so it must be attached with "=": '
                         '--probe-args="--core s2 --data_model far"')
    ap.add_argument('--no-core-probe', action='store_true',
                    help='Do not ask the compiler which --core the device header needs')
    ap.add_argument('--force-predef-header', action='store_true',
                    help='Write the preinclude header even when the probe is skipped')
    ap.add_argument('--no-clangd', action='store_true', help='Skip .clangd generation')
    ap.add_argument('--no-compile-commands', action='store_true',
                    help='Skip compile_commands.json generation')
    ap.add_argument('--fix-placement', action='store_true',
                    help='Write a pointer .clangd when the output dir is not an '
                         'ancestor of the sources')
    ap.add_argument('--list-configs', action='store_true',
                    help='List the build configurations and exit')
    ap.add_argument('--scan-hidden-macros', action='store_true',
                    help='Report macros the sources test that no configuration defines')
    ap.add_argument('--no-verify', action='store_true',
                    help='Skip the post-generation self-check')
    ap.add_argument('--verify-strict', action='store_true',
                    help='Treat self-check warnings (missing include dirs, '
                         'missing sources) as failures too')
    ap.add_argument('--dry-run', action='store_true',
                    help='Print the analysis without writing files')
    return ap


def locate_ewp(args):
    if args.project:
        path = Path(args.project).resolve()
        if not path.is_file():
            print("ERROR: {0} does not exist".format(path))
            return None
        return path
    search_path = Path(args.path).resolve()
    candidates = sorted(search_path.glob('**/*.ewp'))
    if not candidates:
        print("ERROR: No .ewp file found under {0}".format(search_path))
        return None
    if len(candidates) > 1:
        # Printing a notice and carrying on with [0] is not a choice, it is a
        # guess the caller never made. Refuse instead.
        sys.stderr.write("ERROR: {0} .ewp files found under {1}; refusing to "
                         "guess.\n".format(len(candidates), search_path))
        for c in candidates:
            sys.stderr.write("  {0}\n".format(c))
        sys.stderr.write("\nPick one and pass it explicitly:\n")
        sys.stderr.write('  --project "{0}"\n'.format(candidates[0]))
        return None
    return candidates[0]


def refuse_ambiguous_config(ewp_path, parser):
    """Refuse to guess between several build configurations. Returns exit code."""
    configs = parser.list_configs()
    by_config = parser.defines_by_config()
    sys.stderr.write("ERROR: {0} has {1} build configurations and no -c was "
                     "given; refusing to guess.\n"
                     .format(ewp_path.name, len(configs)))
    sys.stderr.write("Configurations differ in macros, so the wrong one "
                     "indexes the wrong build.\n\n")
    for name in configs:
        macros = ", ".join(sorted(by_config.get(name, set()))) or "(none)"
        sys.stderr.write("  {0}\n    Macros: {1}\n".format(name, macros))
    sys.stderr.write("\nRe-run with the configuration the user actually "
                     "builds:\n")
    sys.stderr.write('  -c "{0}"\n'.format(configs[0]))
    sys.stderr.write("\nUse --list-configs or --dry-run to inspect without "
                     "writing, or --use-first-config only when the choice "
                     "genuinely does not matter.\n")
    return 2


def main(argv=None):
    args = build_arg_parser().parse_args(argv)

    ewp_path = locate_ewp(args)
    if ewp_path is None:
        return 1
    print("Using: {0}".format(ewp_path))

    try:
        parser = EwpParser(str(ewp_path), config_name=args.config)
    except ValueError as exc:
        print("ERROR: {0}".format(exc))
        return 1

    if args.list_configs:
        for name in parser.list_configs():
            print(name)
        return 0

    # An ambiguous configuration must be resolved by the caller, never by XML
    # order. --dry-run and --list-configs stay open: they are how you look.
    if (args.config is None and not args.use_first_config
            and not args.dry_run and len(parser.list_configs()) > 1):
        return refuse_ambiguous_config(ewp_path, parser)

    if parser.get_compiler_id() is None:
        print("ERROR: no ICC* compiler settings found in configuration '{0}'."
              .format(parser.get_config_name()))
        return 1

    resolver = IarPathResolver(iar_path=args.iar_path,
                               toolchain=parser.get_toolchain(),
                               compiler_id=parser.get_compiler_id())

    return generate(parser, resolver, args)


if __name__ == '__main__':
    raise SystemExit(main())
