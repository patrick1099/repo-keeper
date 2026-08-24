#!/usr/bin/env python3
"""
Keil2Clangd - Generate .clangd and compile_commands.json from Keil .uvprojx files.

Parses Keil MDK project files and generates clangd-compatible configuration
for embedded C projects using ARMCC v5 or ARM Clang v6.
"""

import os
import re
import sys
import argparse
import xml.etree.ElementTree as ET
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

import cli_common as cc
import k2c_common as common
import k2c_macroscan as macroscan


# ---------------------------------------------------------------------------
# Mapping tables
# ---------------------------------------------------------------------------

CPU_TARGET_MAP = {
    "Cortex-M0":  "armv6m-none-eabi",
    "Cortex-M0+": "armv6m-none-eabi",
    "Cortex-M3":  "armv7m-none-eabi",
    "Cortex-M4":  "armv7em-none-eabi",
    "Cortex-M7":  "armv7em-none-eabi",
    "Cortex-M23": "armv8m.base-none-eabi",
    "Cortex-M33": "armv8m.main-none-eabi",
}

CPU_ARCH_DEFINE_MAP = {
    "Cortex-M0":  "__ARM_ARCH_6M__",
    "Cortex-M0+": "__ARM_ARCH_6M__",
    "Cortex-M3":  "__ARM_ARCH_7M__",
    "Cortex-M4":  "__ARM_ARCH_7EM__",
    "Cortex-M7":  "__ARM_ARCH_7EM__",
    "Cortex-M23": "__ARM_ARCH_8M_BASE__",
    "Cortex-M33": "__ARM_ARCH_8M_MAIN__",
}

KEIL_FALLBACK_PATHS = ["D:/Keil_v5", "C:/Keil_v5", "C:/Keil"]
CONFIG_FILE = common.CONFIG_FILE


def compat_macros(compiler_info, cpu, cc_arm=False):
    """The compiler-identity macros to hand clang, as bare macro names.

    AC6 gets ``__ARMCC_VERSION`` because armclang really is clang and the
    vendor headers behind that macro parse.

    AC5 is the opposite trade, and it used to be made silently. ``__CC_ARM``
    is not read by project code -- it is read by the CMSIS headers on the
    include path, where ``cmsis_compiler.h`` routes it to ``cmsis_armcc.h``,
    a header written in ARMCC-only syntax that clang cannot parse. Defining
    it costs a dozen errors in that header and takes the whole translation
    unit's index down with it; leaving it undefined costs one
    ``#warning Not supported compiler type`` and indexes cleanly. Since the
    macro's real consumer is never in the repo, grepping the sources for it
    always says "unused" -- which is why the wrong default went unnoticed.

    So AC5 omits it by default. A project that vendored the CMSIS headers
    into its own tree, or whose own code branches on ``__CC_ARM``, opts back
    in with ``--cc-arm``; the hidden-macro scan will name it if the sources
    do test it.
    """
    macros = []
    if compiler_info["is_ac6"]:
        macros.append("__ARMCC_VERSION=6000000")
    elif cc_arm:
        macros.append("__CC_ARM")
    macros.append("__arm__")
    arch_define = CPU_ARCH_DEFINE_MAP.get(cpu)
    if arch_define:
        macros.append(arch_define)
    return macros


# ---------------------------------------------------------------------------
# UvprojxParser
# ---------------------------------------------------------------------------

class UvprojxParser:
    """Parse a Keil .uvprojx project file and extract build configuration."""

    def __init__(self, file_path, target_name=None):
        self.file_path = Path(file_path).resolve()
        self.project_root = self.file_path.parent
        self.tree = ET.parse(str(self.file_path))
        self.root = self.tree.getroot()
        self.target_name = target_name
        self.target = self._find_target()

    def _find_target(self):
        """Find a specific target by name, or return the first target."""
        targets = self.root.findall('.//Target')
        if not targets:
            raise ValueError(f"No targets found in {self.file_path}")

        if self.target_name:
            for t in targets:
                name_elem = t.find('TargetName')
                if name_elem is not None and name_elem.text == self.target_name:
                    return t
            available = [t.find('TargetName').text for t in targets
                         if t.find('TargetName') is not None]
            raise ValueError(
                f"Target '{self.target_name}' not found. "
                f"Available targets: {available}"
            )
        return targets[0]

    def get_target_name(self):
        elem = self.target.find('TargetName')
        return elem.text if elem is not None else "Unknown"

    def list_targets(self):
        targets = self.root.findall('.//Target')
        names = []
        for t in targets:
            name_elem = t.find('TargetName')
            if name_elem is not None and name_elem.text:
                names.append(name_elem.text)
        return names

    def get_cpu_type(self):
        """Extract CPU type from AdsCpuType or fall back to Cpu CPUTYPE regex."""
        # Try AdsCpuType first
        elem = self.target.find('.//TargetArmAds/ArmAdsMisc/AdsCpuType')
        if elem is not None and elem.text:
            # Strip surrounding quotes if present
            return elem.text.strip().strip('"')

        # Fallback: parse from Cpu element
        cpu_elem = self.target.find('.//TargetCommonOption/Cpu')
        if cpu_elem is not None and cpu_elem.text:
            match = re.search(r'CPUTYPE\("([^"]+)"\)', cpu_elem.text)
            if match:
                return match.group(1)

        return None

    def get_compiler_info(self):
        """Return dict with is_ac6 (bool) and version_string."""
        uac6_elem = self.target.find('uAC6')
        is_ac6 = False
        if uac6_elem is not None and uac6_elem.text:
            is_ac6 = uac6_elem.text.strip() == '1'

        version_string = "ARMCC v5"
        pcc_elem = self.target.find('pCCUsed')
        if pcc_elem is not None and pcc_elem.text:
            version_string = pcc_elem.text.strip()

        return {"is_ac6": is_ac6, "version_string": version_string}

    def get_pack_id(self):
        elem = self.target.find('.//TargetCommonOption/PackID')
        if elem is not None and elem.text:
            return elem.text.strip()
        return None

    def get_defines(self):
        """Get project defines (comma-separated in the XML)."""
        elem = self.target.find('.//TargetArmAds/Cads/VariousControls/Define')
        if elem is not None and elem.text:
            return [d.strip() for d in elem.text.split(',') if d.strip()]
        return []

    def get_include_paths(self):
        """Get include paths (semicolon-separated), resolved to absolute."""
        elem = self.target.find('.//TargetArmAds/Cads/VariousControls/IncludePath')
        if elem is None or not elem.text:
            return []
        raw_paths = elem.text.split(';')
        abs_paths = []
        seen = set()
        for p in raw_paths:
            p = p.strip().replace('\\', '/')
            if not p:
                continue
            resolved = (self.project_root / p).resolve()
            key = str(resolved).lower()
            if key not in seen:
                seen.add(key)
                abs_paths.append(resolved)
        return abs_paths

    def get_source_files(self):
        """Find all source file paths, resolved to absolute."""
        files = []
        for group in self.root.findall('.//Group'):
            for file_elem in group.findall('.//File'):
                fp_elem = file_elem.find('FilePath')
                if fp_elem is not None and fp_elem.text:
                    raw = fp_elem.text.strip().replace('\\', '/')
                    resolved = (self.project_root / raw).resolve()
                    files.append(resolved)
        return files

    @property
    def project_name(self):
        return self.file_path.stem

    def get_output_dir(self):
        """Relative build output dir (forward slashes). Fallback: 'Objects'."""
        elem = self.target.find('.//TargetOption/TargetCommonOption/OutputDirectory')
        if elem is not None and elem.text and elem.text.strip():
            d = elem.text.strip().replace('\\', '/').rstrip('/')
            return d if d else "Objects"
        return "Objects"


# ---------------------------------------------------------------------------
# .dep enrichment (ground-truth from Keil build output)
# ---------------------------------------------------------------------------

@dataclass
class DepEnrichment:
    """Supplementary build facts parsed from a Keil .dep file.

    Only fields that .uvprojx XML cannot provide. Never carries -D macros or
    project -I paths — those stay sourced from live XML.
    """
    found: bool = False
    stale: bool = False
    dep_path: Optional[Path] = None
    system_includes: List[Path] = field(default_factory=list)
    preinclude_files: List[Path] = field(default_factory=list)
    source_files: List[Path] = field(default_factory=list)


_F_LINE_RE = re.compile(r'^F \((?P<file>[^)]+)\)')
_PREINCLUDE_RE = re.compile(
    r'(?:--preinclude|-imacros)\s+(?:"(?P<a>[^"]+)"|(?P<b>[^"\s)]+))'
    r'|-preinclude=(?:"(?P<c>[^"]+)"|(?P<d>[^"\s)]+))')
_TOOLCHAIN_RE = re.compile(r'^Toolchain Path:\s*(?P<p>.+?)\s*$')


def _dedup(seq):
    seen = set()
    out = []
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _parse_dep_text(text):
    """Parse raw .dep text into supplementary build facts (raw strings).

    Returns dict with system_includes / preinclude_files / source_files,
    forward-slashed and order-preserving deduped. Does NOT extract -D/-I.
    """
    sources = []
    preincludes = []
    sysincs = []
    for raw_line in text.splitlines():
        line = raw_line.replace('\\', '/').rstrip('\r')

        tc = _TOOLCHAIN_RE.match(line)
        if tc:
            p = tc.group('p').rstrip('/')
            # .../Bin -> .../Include
            for tail in ('/Bin', '/bin'):
                if p.endswith(tail):
                    p = p[: -len(tail)] + '/Include'
                    break
            sysincs.append(p)
            continue

        fm = _F_LINE_RE.match(line)
        if fm:
            sources.append(fm.group('file').strip())
            for m in _PREINCLUDE_RE.finditer(line):
                preincludes.append(
                    m.group('a') or m.group('b') or m.group('c') or m.group('d'))

    return {
        "system_includes": _dedup(sysincs),
        "preinclude_files": _dedup(preincludes),
        "source_files": _dedup(sources),
    }


class DepParser:
    """Locate and parse a target's Keil .dep, producing a DepEnrichment.

    Degrades gracefully: missing/unreadable .dep -> found=False; a .dep older
    than the .uvprojx -> found=True, stale=True (caller ignores its data).
    """

    def __init__(self, uvprojx_parser, dep_path_override=None):
        self.p = uvprojx_parser
        self.override = dep_path_override

    def locate(self):
        if self.override:
            cand = Path(self.override)
            return cand if cand.exists() else None
        out_dir = self.p.get_output_dir()
        name = "{0}_{1}.dep".format(self.p.project_name, self.p.get_target_name())
        cand = (self.p.project_root / out_dir / name)
        return cand if cand.exists() else None

    def parse(self):
        try:
            dep_path = self.locate()
            if dep_path is None:
                return DepEnrichment(found=False)

            uv_mtime = self.p.file_path.stat().st_mtime
            dep_mtime = dep_path.stat().st_mtime
            if uv_mtime > dep_mtime:
                return DepEnrichment(found=True, stale=True, dep_path=dep_path)

            raw = _parse_dep_text(dep_path.read_text(encoding="utf-8", errors="ignore"))
            root = self.p.project_root
            return DepEnrichment(
                found=True,
                stale=False,
                dep_path=dep_path,
                system_includes=[Path(s) for s in raw["system_includes"]],
                preinclude_files=[(root / f).resolve() for f in raw["preinclude_files"]],
                source_files=[(root / f).resolve() for f in raw["source_files"]],
            )
        except Exception as exc:  # never break the main flow
            print("WARNING: .dep parse failed ({0}); using .uvprojx only.".format(exc))
            return DepEnrichment(found=False)


# ---------------------------------------------------------------------------
# KeilPathResolver
# ---------------------------------------------------------------------------

class KeilPathResolver:
    """Locate Keil installation and resolve compiler / pack include paths."""

    def __init__(self, keil_path=None, interactive=True):
        self.keil_root = None

        # 1. Explicit CLI path
        if keil_path and Path(keil_path).is_dir():
            self.keil_root = Path(keil_path).resolve()
            return

        # 2. Environment variable
        env_path = os.environ.get('KEIL_PATH')
        if env_path and Path(env_path).is_dir():
            self.keil_root = Path(env_path).resolve()
            return

        # 3. User config file (k2c_common.CONFIG_FILE)
        config_path = self._load_config_keil_path()
        if config_path and Path(config_path).is_dir():
            self.keil_root = Path(config_path).resolve()
            return

        # 4. Fallback: search common locations
        for sp in KEIL_FALLBACK_PATHS:
            if Path(sp).is_dir():
                self.keil_root = Path(sp).resolve()
                self._save_config_keil_path(str(self.keil_root))
                print(f"Found Keil at {self.keil_root}, saved to {CONFIG_FILE}")
                return

        # 5. Interactive prompt
        if interactive:
            self._prompt_and_save()

    _load_config = staticmethod(common.load_config)
    _save_config = staticmethod(common.save_config)

    @classmethod
    def _load_config_keil_path(cls):
        return common.config_get('keil_path')

    @classmethod
    def _save_config_keil_path(cls, keil_path):
        common.config_set('keil_path', keil_path)

    def _prompt_and_save(self):
        if not common.stdin_is_interactive():
            # An agent or CI run inherits a pipe, not a console. EOFError below
            # only saves us when that pipe is closed; one that stays open and
            # silent would block forever, so do not reach the prompt at all.
            print("Keil not found, and stdin is not a terminal -- not prompting.")
            print(f"  Pass -k/--keil-path, set KEIL_PATH, or put 'keil_path' "
                  f"in {CONFIG_FILE}.")
            return
        print("Keil installation not found automatically.")
        print("Please enter the Keil installation path (e.g. D:/Keil_v5):")
        try:
            user_path = input("> ").strip()
        except EOFError:
            return
        if user_path and Path(user_path).is_dir():
            self.keil_root = Path(user_path).resolve()
            self._save_config_keil_path(str(self.keil_root))
            print(f"Saved to {CONFIG_FILE}")
        else:
            print(f"WARNING: '{user_path}' is not a valid directory.")

    def found(self):
        return self.keil_root is not None

    def get_compiler_includes(self, is_ac6):
        """Return list of existing compiler include directories."""
        if not self.found():
            return []
        paths = []
        candidates = [
            self.keil_root / "ARM" / "ARMCLANG" / "include",
        ]
        for c in candidates:
            if c.is_dir():
                paths.append(c)
        return paths

    def _find_cmsis_version_from_pdsc(self, vendor, pack_name, version):
        """Try to find required CMSIS version from device pack's .pdsc file."""
        pack_dir = self.keil_root / "ARM" / "PACK" / vendor / pack_name / version
        pdsc_files = list(pack_dir.glob("*.pdsc")) if pack_dir.is_dir() else []
        if not pdsc_files:
            return None

        try:
            tree = ET.parse(str(pdsc_files[0]))
            root = tree.getroot()
            ns = ''
            if root.tag.startswith('{'):
                ns = root.tag.split('}')[0] + '}'
            # Check <require Cclass="CMSIS" Cversion="..."/>
            for req in root.iter(f'{ns}require'):
                if req.get('Cclass') == 'CMSIS' and req.get('Cversion'):
                    return req.get('Cversion')
            # Check <package vendor="ARM" name="CMSIS" version="..."/>
            for pkg in root.iter(f'{ns}package'):
                if pkg.get('vendor') == 'ARM' and pkg.get('name') == 'CMSIS':
                    ver = pkg.get('version', '')
                    if ver:
                        return ver.split(':')[0]
        except ET.ParseError:
            pass
        return None

    @staticmethod
    def _parse_pack_id(pack_id):
        """Parse PackID into (vendor, pack_name, version) or None."""
        parts = pack_id.split('.')
        if len(parts) < 4:
            return None

        vendor = parts[0]
        version_start = None
        for i in range(len(parts) - 2):
            if (parts[i].isdigit() and parts[i + 1].isdigit()
                    and parts[i + 2].isdigit()
                    and i + 2 == len(parts) - 1):
                version_start = i
                break

        if version_start is None:
            return None

        pack_name = '.'.join(parts[1:version_start])
        version = '.'.join(parts[version_start:])
        return vendor, pack_name, version

    def get_pack_includes(self, pack_id):
        """Parse PackID and return existing pack include directories."""
        if not self.found() or not pack_id:
            return []

        parsed = self._parse_pack_id(pack_id)
        if not parsed:
            return []
        vendor, pack_name, version = parsed

        paths = []

        # Device include
        device_inc = (self.keil_root / "ARM" / "PACK" / vendor
                      / pack_name / version / "Device" / "Include")
        if device_inc.is_dir():
            paths.append(device_inc)

        # CMSIS Core Include — prefer version matching device pack .pdsc hint
        cmsis_base = self.keil_root / "ARM" / "PACK" / "ARM" / "CMSIS"
        if cmsis_base.is_dir():
            installed = sorted(
                [d for d in cmsis_base.iterdir() if d.is_dir()],
                key=lambda d: d.name,
            )
            chosen = None

            hint = self._find_cmsis_version_from_pdsc(vendor, pack_name, version)
            if hint:
                for d in installed:
                    if d.name >= hint:
                        chosen = d
                        break

            if chosen is None and installed:
                chosen = installed[-1]

            if chosen:
                core_inc = chosen / "CMSIS" / "Core" / "Include"
                if core_inc.is_dir():
                    paths.append(core_inc)

        return paths


# ---------------------------------------------------------------------------
# Path formatting helper
# ---------------------------------------------------------------------------

_format_path = common.format_path


# ---------------------------------------------------------------------------
# ClangdGenerator
# ---------------------------------------------------------------------------

class ClangdGenerator:
    """Generate a .clangd YAML configuration file."""

    CLANG_TIDY_ADD = common.CLANG_TIDY_ADD
    CLANG_TIDY_REMOVE = common.CLANG_TIDY_REMOVE
    DIAGNOSTICS_SUPPRESS = common.DIAGNOSTICS_SUPPRESS

    def __init__(self, parser, keil_resolver, use_absolute=False, base_dir=None,
                 enrichment=None, cc_arm=False):
        self.parser = parser
        self.keil = keil_resolver
        self.use_absolute = use_absolute
        self.base_dir = Path(base_dir).resolve() if base_dir else Path.cwd().resolve()
        self.enrichment = enrichment
        self.cc_arm = cc_arm

    def generate(self):
        """Return the .clangd YAML string."""
        cpu = self.parser.get_cpu_type()
        compiler_info = self.parser.get_compiler_info()
        pack_id = self.parser.get_pack_id()
        defines = self.parser.get_defines()
        include_paths = self.parser.get_include_paths()

        doc = common.ClangdDoc()

        target = CPU_TARGET_MAP.get(cpu, "armv6m-none-eabi")
        doc.add_group(f"{cpu}", [f"--target={target}"])

        compat = ["-D" + m for m in compat_macros(compiler_info, cpu, self.cc_arm)]
        doc.add_group("ARM C Compiler compatibility macros", compat)

        if defines:
            doc.add_group("Keil project macros", [f"-D{d}" for d in defines])

        doc.add_group("Include paths",
                      [f"-I{_format_path(p, self.base_dir, self.use_absolute)}"
                       for p in include_paths])

        if self.keil.found():
            keil_incs = (self.keil.get_compiler_includes(compiler_info["is_ac6"])
                         + self.keil.get_pack_includes(pack_id))
            doc.add_group(
                "Keil/ARMCC standard library and CMSIS/device headers for clangd.",
                [f"-I{_format_path(ki, self.base_dir, self.use_absolute)}"
                 for ki in keil_incs])

        enr = self.enrichment
        if enr and enr.found and not enr.stale:
            doc.add_group(
                "Compiler system headers (from .dep)",
                [f"-I{_format_path(inc, self.base_dir, self.use_absolute)}"
                 for inc in enr.system_includes])
            preinclude_flags = []
            for pf in enr.preinclude_files:
                preinclude_flags += [
                    "-imacros", _format_path(pf, self.base_dir, self.use_absolute)]
            doc.add_group("Preinclude headers (from .dep)", preinclude_flags,
                          allow_duplicates=True)

        doc.set_diagnostics(self.DIAGNOSTICS_SUPPRESS,
                            self.CLANG_TIDY_ADD, self.CLANG_TIDY_REMOVE)
        return doc.render()

    def write(self, output_path, dry_run=False):
        out = Path(output_path) / '.clangd'
        if dry_run:
            print(f"Would generate: {out}")
            return out
        out.write_text(self.generate(), encoding='utf-8')
        print(f"Generated: {out}")
        return out


# ---------------------------------------------------------------------------
# CompileCommandsGenerator
# ---------------------------------------------------------------------------

class CompileCommandsGenerator:
    """Generate compile_commands.json for clangd / IDE integration."""

    def __init__(self, parser, keil_resolver, use_absolute=False, base_dir=None,
                 enrichment=None, cc_arm=False):
        self.parser = parser
        self.keil = keil_resolver
        self.use_absolute = use_absolute
        self.base_dir = Path(base_dir).resolve() if base_dir else Path.cwd().resolve()
        self.enrichment = enrichment
        self.cc_arm = cc_arm

    def generate(self):
        """Return a list of compile-command entry dicts."""
        cpu = self.parser.get_cpu_type()
        compiler_info = self.parser.get_compiler_info()
        pack_id = self.parser.get_pack_id()
        defines = self.parser.get_defines()
        include_paths = self.parser.get_include_paths()
        source_files = self.parser.get_source_files()
        enr = self.enrichment
        use_enr = bool(enr and enr.found and not enr.stale)
        if use_enr and enr.source_files:
            source_files = enr.source_files

        target = CPU_TARGET_MAP.get(cpu, "armv6m-none-eabi")

        # Build common arguments
        base_args = [f"--target={target}"]

        # Compiler macros -- same source of truth as the .clangd, or verify
        # will (correctly) report the two files disagreeing on -D.
        for macro in compat_macros(compiler_info, cpu, self.cc_arm):
            base_args.append(f"-D{macro}")

        # Project defines
        for d in defines:
            base_args.append(f"-D{d}")

        # Project includes
        for p in include_paths:
            formatted = _format_path(p, self.base_dir, self.use_absolute)
            base_args.append(f"-I{formatted}")

        # Keil includes
        if self.keil.found():
            compiler_incs = self.keil.get_compiler_includes(
                compiler_info["is_ac6"])
            pack_incs = self.keil.get_pack_includes(pack_id)
            for ki in compiler_incs + pack_incs:
                formatted = _format_path(ki, self.base_dir, self.use_absolute)
                base_args.append(f"-I{formatted}")

        # .dep enrichment: compiler system includes (XML can't provide these)
        if use_enr:
            existing = {a for a in base_args if a.startswith("-I")}
            for inc in enr.system_includes:
                formatted = _format_path(inc, self.base_dir, self.use_absolute)
                flag = f"-I{formatted}"
                if flag not in existing:
                    base_args.append(flag)
                    existing.add(flag)

        compiler = "arm-none-eabi-gcc"

        preinclude_args = []
        if use_enr:
            for pf in enr.preinclude_files:
                formatted = _format_path(pf, self.base_dir, self.use_absolute)
                preinclude_args += ["-imacros", formatted]

        return common.make_compile_entries(
            compiler, base_args + preinclude_args, source_files,
            self.base_dir, self.use_absolute)

    def write(self, output_path, dry_run=False):
        return common.write_compile_commands(self.generate(), output_path,
                                             dry_run=dry_run)


# ---------------------------------------------------------------------------
# Macro checker
# ---------------------------------------------------------------------------

def check_macros(parser, keil_resolver, cc_arm=False):
    """Print diagnostic info about the parsed project.

    Returns the set of macro names known to be defined somewhere -- this
    target, any other target, or the compiler -- which is what the hidden-macro
    scan subtracts from the macros the sources actually test.
    """
    cpu = parser.get_cpu_type()
    compiler_info = parser.get_compiler_info()
    pack_id = parser.get_pack_id()
    defines = parser.get_defines()
    include_paths = parser.get_include_paths()

    print("=" * 60)
    print(f"  Target:    {parser.get_target_name()}")
    print(f"  CPU:       {cpu}")
    print(f"  Compiler:  {'AC6 (armclang)' if compiler_info['is_ac6'] else 'AC5 (armcc)'}"
          f"  [{compiler_info['version_string']}]")
    print(f"  PackID:    {pack_id}")
    print(f"  Clang target: {CPU_TARGET_MAP.get(cpu, '???')}")
    print("=" * 60)

    # Project macros
    print(f"\n[Project macros] ({len(defines)} found)")
    if defines:
        for d in defines:
            print(f"  -D{d}")
    else:
        print("  WARNING: no project macros found in uvprojx!")

    # Compiler macros (auto-added)
    auto_macros = compat_macros(compiler_info, cpu, cc_arm)

    print(f"\n[Auto-added compiler macros] ({len(auto_macros)})")
    for m in auto_macros:
        print(f"  -D{m}")
    if not compiler_info["is_ac6"] and not cc_arm:
        # Say it out loud: the omission is deliberate, and the one project that
        # needs it back has no other way to learn the flag exists.
        print("  (AC5: __CC_ARM deliberately NOT defined -- it makes clang "
              "choke on the pack's cmsis_armcc.h.")
        print("   Pass --cc-arm if this project vendors CMSIS headers or its "
              "own code branches on it.)")

    total = len(defines) + len(auto_macros)
    print(f"\n  Total macros: {total}")

    # Include paths
    print(f"\n[Project include paths] ({len(include_paths)})")
    for p in include_paths:
        exists = p.is_dir()
        marker = "OK" if exists else "MISSING"
        print(f"  [{marker}] {p}")

    # Keil paths
    if keil_resolver.found():
        print(f"\n[Keil installation] {keil_resolver.keil_root}")
        compiler_incs = keil_resolver.get_compiler_includes(
            compiler_info["is_ac6"])
        pack_incs = keil_resolver.get_pack_includes(pack_id)
        all_keil = compiler_incs + pack_incs
        print(f"[Keil include paths] ({len(all_keil)})")
        for ki in all_keil:
            exists = ki.is_dir()
            marker = "OK" if exists else "MISSING"
            print(f"  [{marker}] {ki}")
    else:
        print("\n[Keil installation] NOT FOUND")

    known = macroscan.macro_names(defines) | macroscan.macro_names(auto_macros)

    # All targets with their macros
    targets = parser.list_targets()
    if len(targets) > 1:
        print(f"\n[All targets and their macros] ({len(targets)})")
        selected_name = parser.get_target_name()
        # The marker must not claim more than it knows: without -t this is
        # merely the first Target element in the XML, which has nothing to do
        # with whichever target is selected in the Keil IDE (that lives in
        # .uvoptx and is not read here).
        if parser.target_name is not None:
            marker_text = " <-- chosen by -t"
        else:
            marker_text = " <-- default: FIRST IN XML (not Keil's IDE selection)"
        all_target_defines = {}
        for t_name in targets:
            t_parser = UvprojxParser(str(parser.file_path), target_name=t_name)
            t_defines = t_parser.get_defines()
            all_target_defines[t_name] = set(t_defines)
            known |= macroscan.macro_names(t_defines)
            cur = marker_text if t_name == selected_name else ""
            macros_str = ", ".join(t_defines) if t_defines else "(none)"
            print(f"  {t_name}{cur}")
            print(f"    Macros: {macros_str}")

        # Warn about macros in other targets but missing from selected
        selected_defines = all_target_defines.get(selected_name, set())
        other_macros = set()
        for t_name, t_defs in all_target_defines.items():
            if t_name != selected_name:
                other_macros |= t_defs
        missing_from_selected = other_macros - selected_defines
        if missing_from_selected:
            print(f"\n[WARN] Macros in other targets but NOT in '{selected_name}':")
            for m in sorted(missing_from_selected):
                sources = [t for t, d in all_target_defines.items()
                           if m in d and t != selected_name]
                print(f"  -D{m}  (from: {', '.join(sources)})")

        if parser.target_name is None:
            print("\n[ACTION REQUIRED] No -t was given. Ask which target the "
                  "user actually builds, then re-run with -t <name>.")

    print()
    return known


# ---------------------------------------------------------------------------
# Ambiguity gates
#
# Both of these used to be a silent `[0]`. A caller that did not choose got a
# config for whichever project/target happened to come first, with exit 0 and a
# clean-looking report -- the failure surfaced days later as wrong macros. The
# instruction "ask the user which one" only binds a caller that reads it, so the
# refusal lives here instead of in the skill prose.
# ---------------------------------------------------------------------------

def _refuse_ambiguous_project(uvprojx_files, search_path, json_mode=False):
    """Refuse to guess between several .uvprojx. Returns a CliResult."""
    if json_mode:
        return cc.fail(
            "E_VALIDATION",
            "{0} .uvprojx files found under {1}; pass --project to choose one"
            .format(len(uvprojx_files), search_path),
            details={
                "candidates": [str(p) for p in uvprojx_files],
                "suggestion": '--project "{0}"'.format(uvprojx_files[0]),
            },
            exit_code=cc.EXIT_ARG)
    print(f"ERROR: {len(uvprojx_files)} .uvprojx files found under "
          f"{search_path}; refusing to guess.", file=sys.stderr)
    for p in uvprojx_files:
        print(f"  {p}", file=sys.stderr)
    print("\nPick one and pass it explicitly:", file=sys.stderr)
    print(f'  --project "{uvprojx_files[0]}"', file=sys.stderr)
    return cc.fail("E_VALIDATION", "", exit_code=cc.EXIT_ARG)


def _refuse_ambiguous_target(uvprojx_path, target_names, json_mode=False):
    """Refuse to guess between several targets. Returns a CliResult."""
    if json_mode:
        targets = []
        for name in target_names:
            try:
                defines = UvprojxParser(str(uvprojx_path),
                                        target_name=name).get_defines()
            except Exception:
                defines = []
            targets.append({"target": name, "macros": list(defines)})
        return cc.fail(
            "E_VALIDATION",
            "{0} has {1} build targets and no -t was given; pass -t"
            .format(uvprojx_path.name, len(target_names)),
            details={
                "targets": targets,
                "suggestion": '-t "{0}"'.format(target_names[0]),
            },
            exit_code=cc.EXIT_ARG)
    print(f"ERROR: {uvprojx_path.name} has {len(target_names)} build targets "
          f"and no -t was given; refusing to guess.", file=sys.stderr)
    print("Targets differ in macros, so the wrong one indexes the wrong "
          "build.\n", file=sys.stderr)
    for name in target_names:
        try:
            defines = UvprojxParser(str(uvprojx_path),
                                    target_name=name).get_defines()
        except Exception:
            defines = []
        macros = ", ".join(defines) if defines else "(none)"
        print(f"  {name}", file=sys.stderr)
        print(f"    Macros: {macros}", file=sys.stderr)
    print("\nRe-run with the target the user actually builds:", file=sys.stderr)
    print(f'  -t "{target_names[0]}"', file=sys.stderr)
    print("\nUse --dry-run to inspect targets without writing, or "
          "--use-first-target only when the choice genuinely does not matter.",
          file=sys.stderr)
    return cc.fail("E_VALIDATION", "", exit_code=cc.EXIT_ARG)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def build_arg_parser():
    ap = cc.CliFriendlyParser(
        prog="Keil2Clangd",
        description="LLMs/agents: run 'Keil2Clangd --ai-help' for usage guidance. "
                    "Generate .clangd and compile_commands.json from Keil .uvprojx.")
    ap.add_argument('-p', '--path', default='.',
                    help='Search path for .uvprojx file (default: current dir)')
    ap.add_argument('-a', '--absolute', action='store_true',
                    help='Use absolute paths in generated files')
    ap.add_argument('-t', '--target-name', default=None,
                    help='Select a specific build target by name')
    ap.add_argument('--project', default=None,
                    help='Explicit .uvprojx path, skipping the search')
    ap.add_argument('--use-first-target', action='store_true',
                    help='Accept the first target in the XML instead of '
                         'requiring -t. Only for unattended runs that truly '
                         'do not care which build configuration is indexed')
    ap.add_argument('-k', '--keil-path', default=None,
                    help='Keil installation path (e.g. D:/Keil_v5)')
    ap.add_argument('--no-clangd', action='store_true',
                    help='Skip .clangd generation')
    ap.add_argument('--no-compile-commands', action='store_true',
                    help='Skip compile_commands.json generation')
    ap.add_argument('--dry-run', action='store_true',
                    help='Print info without writing any files')
    ap.add_argument('-o', '--output', default=None,
                    help="Output directory (default: the .uvprojx's own "
                         "directory)")
    ap.add_argument('--cc-arm', action='store_true',
                    help='AC5 only: define __CC_ARM. Off by default because it '
                         'routes the pack CMSIS headers into cmsis_armcc.h, '
                         'which clang cannot parse. Turn it on for projects '
                         'that vendor their own CMSIS headers')
    ap.add_argument('--no-dep', action='store_true',
                    help='Ignore Keil .dep build output; use .uvprojx only')
    ap.add_argument('--dep-path', default=None,
                    help='Explicit path to the target .dep file')
    ap.add_argument('--fix-placement', action='store_true',
                    help='Write a pointer .clangd when the output dir is not an '
                         'ancestor of the sources')
    ap.add_argument('--scan-hidden-macros', action='store_true',
                    help='Report macros the sources test that no target defines')
    ap.add_argument('--no-verify', action='store_true',
                    help='Skip the post-generation self-check')
    ap.add_argument('--verify-strict', action='store_true',
                    help='Treat self-check warnings (missing include dirs, '
                         'missing sources) as failures too')
    ap.add_argument('--no-syntax-probe', action='store_true',
                    help='Skip the self-check step that parses a couple of '
                         'entries with a real clang')
    ap.add_argument('--no-exe', action='store_true',
                    help='Do not place {0} in the project root'.format(
                        common.REANCHOR_EXE_NAME))
    ap.add_argument('--no-build-exe', action='store_true',
                    help='Deploy {0} only if it was already built; never '
                         'invoke PyInstaller'.format(common.REANCHOR_EXE_NAME))
    ap.add_argument('--exe-dest', default=None,
                    help='Directory for {0} '
                         '(default: the git repo root above the output dir)'
                         .format(common.REANCHOR_EXE_NAME))
    ap.add_argument('--json', action='store_true',
                    help='以 JSON 信封输出(与 --format json 等价)')
    ap.add_argument('--format', choices=('json',), default='json',
                    help='输出格式:仅支持 json(与 --json 等价)')
    ap.add_argument('--ai-help', action='store_true',
                    help='输出 AI 优化的使用说明并退出')
    return ap


def command(argv, context):
    args = build_arg_parser().parse_args(argv)

    # Find .uvprojx file
    if args.project:
        uvprojx_path = Path(args.project).resolve()
        if not uvprojx_path.is_file():
            return cc.fail("E_NOT_FOUND",
                           "ERROR: --project does not exist: {0}".format(uvprojx_path),
                           details={"path": str(uvprojx_path)})
    else:
        search_path = Path(args.path).resolve()
        uvprojx_files = sorted(search_path.glob('**/*.uvprojx'))
        if not uvprojx_files:
            return cc.fail("E_NOT_FOUND",
                           "ERROR: No .uvprojx file found under {0}".format(search_path),
                           details={"path": str(search_path)})
        if len(uvprojx_files) > 1:
            return _refuse_ambiguous_project(uvprojx_files, search_path,
                                             json_mode=context.json_mode)
        uvprojx_path = uvprojx_files[0]
    print(f"Using: {uvprojx_path}")

    # An ambiguous target must be resolved by the caller, never by XML order:
    # picking silently produces a config for the wrong build with exit 0.
    if args.target_name is None and not args.use_first_target:
        all_targets = UvprojxParser(str(uvprojx_path)).list_targets()
        if len(all_targets) > 1 and not args.dry_run:
            return _refuse_ambiguous_target(uvprojx_path, all_targets,
                                            json_mode=context.json_mode)

    # Parse
    parser = UvprojxParser(str(uvprojx_path), target_name=args.target_name)

    # Resolve Keil
    keil = KeilPathResolver(keil_path=args.keil_path,
                            interactive=not context.json_mode)

    # Output directory
    if args.output is None:
        output_dir = uvprojx_path.parent.resolve()
        print(f"Output:  {output_dir}  (the project's own directory; -o to change)")
    else:
        output_dir = Path(args.output).resolve()

    # Always print macro / path check
    known_macros = check_macros(parser, keil, cc_arm=args.cc_arm)

    # Build .dep enrichment (ground-truth supplement; XML stays authoritative)
    enrichment = DepEnrichment(found=False)
    if not args.no_dep:
        enrichment = DepParser(parser, dep_path_override=args.dep_path).parse()
        if enrichment.found and not enrichment.stale:
            print(f".dep: using {enrichment.dep_path} "
                  f"(+{len(enrichment.system_includes)} sysinc, "
                  f"+{len(enrichment.preinclude_files)} preinclude, "
                  f"{len(enrichment.source_files)} files)")
        elif enrichment.stale:
            # Plain ASCII on purpose: this goes to Windows consoles running the
            # GBK code page, where an em-dash cannot be encoded.
            print(f".dep: STALE ({enrichment.dep_path} older than .uvprojx) -- "
                  f"ignored; rebuild the project to refresh system headers/preincludes.")
        else:
            print(".dep: not found -- using .uvprojx only (no build output).")
    else:
        print(".dep: skipped (--no-dep).")

    # Content is not enough: clangd must also be able to FIND the config, and it
    # only ever searches a source file's own directory and its ancestors.
    sources = (enrichment.source_files
               if enrichment.found and not enrichment.stale and enrichment.source_files
               else parser.get_source_files())
    if args.scan_hidden_macros:
        macroscan.report(sources, known_macros,
                         base_dir=Path(uvprojx_path).parent)

    generated = []
    placement = common.check_placement(output_dir, sources)
    print(placement.describe())
    if not placement.ok and args.fix_placement and placement.anchor:
        common.write_pointer_clangd(output_dir, placement.anchor,
                                    dry_run=args.dry_run)
        generated.append(str(Path(placement.anchor) / '.clangd'))
    elif not placement.ok:
        print("  (re-run with --fix-placement to write that pointer automatically)")

    # Generate
    if not args.no_clangd:
        gen = ClangdGenerator(parser, keil,
                              use_absolute=args.absolute,
                              base_dir=output_dir,
                              enrichment=enrichment,
                              cc_arm=args.cc_arm)
        gen.write(output_dir, dry_run=args.dry_run)
        generated.append(str(output_dir / '.clangd'))

    if not args.no_compile_commands:
        gen = CompileCommandsGenerator(parser, keil,
                                       use_absolute=args.absolute,
                                       base_dir=output_dir,
                                       enrichment=enrichment,
                                       cc_arm=args.cc_arm)
        gen.write(output_dir, dry_run=args.dry_run)
        generated.append(str(output_dir / 'compile_commands.json'))

    if not args.no_exe:
        root = (Path(args.exe_dest).resolve() if args.exe_dest
                else common.find_project_root(output_dir, sources))
        exe_dest = common.deploy_reanchor_exe(root, dry_run=args.dry_run,
                                              auto_build=not args.no_build_exe)
        if exe_dest is not None:
            generated.append(str(exe_dest))

    data = {
        "project": str(uvprojx_path),
        "target": parser.get_target_name(),
        "output_dir": str(output_dir),
        "generated": generated,
    }
    if args.dry_run:
        data["dry_run"] = True
        print("--dry-run: no files written.")
        return cc.ok(data)

    ok, report = common.run_verify(output_dir, no_verify=args.no_verify,
                                   strict=args.verify_strict,
                                   probe=not args.no_syntax_probe)
    if report is not None:
        data["verify"] = {
            "ok": ok,
            "errors": list(report.errors),
            "warnings": list(report.warnings),
            "notes": list(report.notes),
        }
    if not ok:
        return cc.fail(
            "E_VERIFICATION_FAILED", "post-generation self-check failed",
            details={
                "errors": list(report.errors),
                "warnings": list(report.warnings),
                "notes": list(report.notes),
                "strict": args.verify_strict,
                "summary": "{0} error(s), {1} warning(s)".format(
                    len(report.errors), len(report.warnings)),
            })
    return cc.ok(data)


AI_HELP = """---
name: Keil2Clangd
description: >
  Generate .clangd and compile_commands.json from a Keil .uvprojx project for
  clangd. Use when user asks to set up clangd jump/completion/diagnostics for
  a Keil MDK project, or mentions .uvprojx / Keil v5/v6 / ARMCC / armclang.
ai_help_version: 0.1.0
---

# Keil2Clangd AI Help Guide

## Quick Reference

- **Generate from a project:** `Keil2Clangd.py --project <file.uvprojx> --json`
- **Search a directory:** `Keil2Clangd.py -p <dir> --json`
- **Preview without writing:** `Keil2Clangd.py -p <dir> --dry-run`

## When to Use

Use this tool when the user asks to:
- set up clangd for a Keil MDK project (`.uvprojx`)
- fix cross-file jump-to-definition that silently fails
- regenerate `.clangd` / `compile_commands.json` after editing the project

Do NOT use for:
- IAR `.ewp` projects (use `Iar2Clangd.py`)
- CMake projects (use `Cmake2Clangd.py`)

## Command Reference

- `-p, --path <dir>`: search path for a single `.uvprojx` (default: current dir)
- `--project <file>`: explicit `.uvprojx` path, skipping the search
- `-t, --target-name <name>`: build target to generate for
- `--use-first-target`: accept the first XML target without asking
- `-a, --absolute`: absolute paths in the generated files
- `-o, --output <dir>`: output directory (default: the project's own dir)
- `-k, --keil-path <dir>`: Keil installation path
- `--cc-arm`: define `__CC_ARM` (AC5 only; off by default)
- `--no-clangd` / `--no-compile-commands`: skip one of the two artifacts
- `--fix-placement`: write a pointer `.clangd` when the output dir is a sibling of the sources
- `--scan-hidden-macros`: report macros the sources test that no target defines
- `--no-verify` / `--verify-strict` / `--no-syntax-probe`: self-check controls
- `--no-exe` / `--no-build-exe` / `--exe-dest <dir>`: re-anchor exe controls
- `--dry-run`: preview without writing
- `--json`: machine envelope output (equivalent to `--format json`)

## Input / Output

- `--json` success: `{ok:true, data:{project, target, output_dir, generated, verify, dry_run?}, error:null, meta:{log}}`
- `--json` failure: envelope on stderr, stdout empty; `error.code` from the table below
- human mode: the existing report on stdout, errors on stderr

## Side Effects & Safety

- Writes `.clangd` and `compile_commands.json` under the output dir by default.
- `--fix-placement` writes a pointer `.clangd` at the sources' common ancestor.
- Deploys the re-anchor exe to the project root unless `--no-exe` (may invoke PyInstaller unless `--no-build-exe`).
- `--dry-run` previews without writing (data carries `dry_run: true`).

## Exit Codes

| code | meaning |
|---|---|
| 0 | success |
| 1 | runtime failure (see error.code) |
| 2 | parameter / usage error (E_VALIDATION) |

## Errors & Recovery

| code | meaning | recovery |
|---|---|---|
| `E_VALIDATION` | bad argument / ambiguous project or target | fix the argument, or pass `--project` / `-t` |
| `E_NOT_FOUND` | no `.uvprojx`, or `--project` missing | point at a real project file |
| `E_VERIFICATION_FAILED` | generated files disagree or self-check failed | inspect error.details |
| `E_INTERNAL` | unexpected bug | report it |
"""


def main(argv=None, sinks=None):
    return cc.main(argv, sinks, command=command,
                   parser_factory=build_arg_parser, ai_help=AI_HELP,
                   prog="Keil2Clangd")


if __name__ == '__main__':
    raise SystemExit(main())
