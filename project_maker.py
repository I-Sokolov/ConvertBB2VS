"""Generate Visual Studio projects for a Bentley Part from existing build logs."""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import locale
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid
import xml.etree.ElementTree as ET


NS = "http://schemas.microsoft.com/developer/msbuild/2003"
ET.register_namespace("", NS)
CPP_GUID = "{8BC9CEB8-8B4A-11D0-8D11-00A0C91BC942}"
CS_GUID = "{FAE04EC0-301F-11D3-BF4B-00C04F79EFBC}"
SOLUTION_FOLDER_GUID = "{2150E333-8FDC-42A3-9474-1A3956D46DE8}"
COMPILER = re.compile(r'(?:^|[\\/\s"])(clang-cl|cl|csc)(?:\.exe)?(?:"?\s+)', re.I)
PROJECT_START = re.compile(r'\bProject "([^"]+\.(?:csproj|vcxproj))".* on node ', re.I)
PROJECT_END = re.compile(r'\bDone Building Project "([^"]+\.(?:csproj|vcxproj))"', re.I)


class GenerationError(Exception):
    """An actionable input or configuration error."""


def read_text(path: Path) -> str:
    data = path.read_bytes()
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode(locale.getpreferredencoding(False))


def windows_args(command: str) -> list[str]:
    """Split Windows compiler arguments, including quotes inside /I and /D."""
    args: list[str] = []
    i = 0
    while i < len(command):
        while i < len(command) and command[i].isspace():
            i += 1
        if i == len(command):
            break
        value: list[str] = []
        quoted = False
        while i < len(command) and (quoted or not command[i].isspace()):
            if command[i] == "\\":
                start = i
                while i < len(command) and command[i] == "\\":
                    i += 1
                count = i - start
                if i < len(command) and command[i] == '"':
                    value.extend("\\" * (count // 2))
                    if count % 2:
                        value.append('"')
                    else:
                        quoted = not quoted
                    i += 1
                else:
                    value.extend("\\" * count)
            elif command[i] == '"':
                quoted = not quoted
                i += 1
            else:
                value.append(command[i])
                i += 1
        if quoted:
            raise GenerationError("Unterminated quote in compiler arguments.")
        args.append("".join(value))
    return args


def absolute(value: str, cwd: Path) -> Path:
    if "$(" in value or re.search(r"%\w+%", value):
        raise GenerationError(f"Unexpanded variable in logged path: {value}")
    path = Path(value)
    return Path(os.path.abspath(path if path.is_absolute() else cwd / path))


def expand_response(args: list[str], cwd: Path, seen: tuple[Path, ...] = ()) -> list[str]:
    result: list[str] = []
    for arg in args:
        if not arg.startswith("@"):
            result.append(arg)
            continue
        path = absolute(arg[1:], cwd)
        if path in seen:
            raise GenerationError(f"Recursive response file: {path}")
        if not path.is_file():
            raise GenerationError(f"Logged response file is unavailable: {path}")
        result.extend(expand_response(windows_args(read_text(path)), cwd, (*seen, path)))
    return result


def unique(values):
    return list(dict.fromkeys(values))


@dataclass
class Compilation:
    language: str
    cwd: Path
    sources: list[Path] = field(default_factory=list)
    includes: list[str] = field(default_factory=list)
    defines: list[str] = field(default_factory=list)
    undefines: list[str] = field(default_factory=list)
    forced: list[str] = field(default_factory=list)
    assembly_dirs: list[str] = field(default_factory=list)
    references: list[tuple[str, str]] = field(default_factory=list)
    output: str = ""
    standard: str = ""
    platform: str = ""
    unsafe: bool = False
    clr: bool = False
    options: list[str] = field(default_factory=list)
    target: str = "library"
    compile_as: str = ""
    source_modes: dict[Path, str] = field(default_factory=dict)
    project: Path | None = None


def parse_compilation(compiler: str, args: list[str], cwd: Path) -> Compilation:
    unit = Compilation("cs" if compiler.lower() == "csc" else "cpp", cwd)
    i = 0
    while i < len(args):
        arg = args[i]
        i += 1
        if unit.language == "cpp":
            found = False
            # MSVC switches /D, /I, /FI, /FU and /U are case-sensitive.
            for switch, dest, is_path in (
                ("external:I", unit.includes, True),
                ("FI", unit.forced, True),
                ("FU", None, True),
                ("AI", unit.assembly_dirs, True),
                ("I", unit.includes, True),
                ("D", unit.defines, False),
                ("U", unit.undefines, False),
            ):
                prefix = next((p for p in ("/" + switch, "-" + switch) if arg.startswith(p)), None)
                if prefix is None:
                    continue
                value = arg[len(prefix):]
                if not value:
                    if i == len(args):
                        raise GenerationError(f"Missing value for {arg}")
                    value = args[i]
                    i += 1
                if dest is None:
                    unit.references.append(("", str(absolute(value, cwd))))
                else:
                    dest.append(str(absolute(value, cwd)) if is_path else value)
                found = True
                break
            if found:
                continue
            if arg.startswith(("/std:", "-std:")):
                unit.standard = arg.split(":", 1)[1]
            elif arg in ("/clr", "/clr:netcore", "/clr:nostdlib"):
                unit.clr = True
            elif arg.startswith(("/Zc:", "-Zc:", "/permissive", "-permissive")):
                unit.options.append(arg)
            elif arg in ("/TP", "-TP", "/TC", "-TC"):
                unit.compile_as = "CompileAsCpp" if arg.endswith("P") else "CompileAsC"
            elif arg.startswith(("/Tp", "/Tc", "-Tp", "-Tc")):
                value = arg[3:]
                if not value:
                    if i == len(args):
                        raise GenerationError(f"Missing source for {arg}")
                    value = args[i]
                    i += 1
                path = absolute(value, cwd)
                unit.sources.append(path)
                unit.source_modes[path] = "CompileAsCpp" if arg[2] == "p" else "CompileAsC"
            elif not arg.startswith(("/", "-")) and Path(arg).suffix.lower() in (".c", ".cpp", ".cxx", ".cc"):
                unit.sources.append(absolute(arg, cwd))
        else:
            key, _, value = arg.lstrip("/-").partition(":")
            key = key.lower()
            if key in ("define", "d"):
                unit.defines.extend(v for v in re.split("[;,]", value) if v)
            elif key in ("reference", "r"):
                for reference in value.split(";"):
                    if not reference:
                        raise GenerationError("Empty C# assembly reference.")
                    alias, equal, path = reference.partition("=")
                    unit.references.append((alias if equal else "", str(absolute(path if equal else reference, cwd))))
            elif key == "out":
                unit.output = str(absolute(value, cwd))
            elif key == "langversion":
                unit.standard = value
            elif key == "platform":
                unit.platform = value
            elif key in ("target", "t"):
                unit.target = value.lower()
            elif key == "unsafe+" or key == "unsafe":
                unit.unsafe = True
            elif not arg.startswith(("/", "-")) and Path(arg).suffix.lower() == ".cs":
                unit.sources.append(absolute(arg, cwd))
    unit.sources = unique(unit.sources)
    unit.includes = unique(unit.includes)
    unit.defines = unique(unit.defines)
    if not unit.sources:
        raise GenerationError(f"A logged {compiler} invocation has no source files.")
    return unit


def parse_log(path: Path, fallback_cwd: Path) -> list[Compilation]:
    lines = read_text(path).splitlines()
    units: list[Compilation] = []
    cwd = fallback_cwd
    projects: list[Path] = []
    msbuild_project: Path | None = None
    i = 0
    while i < len(lines):
        line = re.sub(r"^\s*\d+>", "", lines[i]).strip()
        i += 1
        start = PROJECT_START.search(line)
        end = PROJECT_END.search(line)
        if start:
            projects.append(absolute(start[1], cwd))
        if end:
            ended = absolute(end[1], cwd)
            projects = [p for p in projects if p != ended]
        cd = re.match(r'(?i)^cd\s+(?:/d\s+)?("[^"]+"|.+?)(?:\s+&|$)', line)
        if cd:
            cwd = absolute(cd[1].strip('"'), cwd)
        if re.search(r"(?i)\bmsbuild(?:\.exe)?\s", line):
            project = re.search(r'"([^"]+\.(?:csproj|vcxproj))"', line, re.I)
            if project:
                msbuild_project = absolute(project[1], cwd)
        match = COMPILER.search(line)
        if not match:
            continue
        command = line[match.end():]
        while command.endswith((" ^", " \\")) and i < len(lines):
            command = command[:-2] + " " + lines[i].strip()
            i += 1
        if "@<<" in command:
            inline: list[str] = []
            while i < len(lines) and lines[i].strip() != "<<":
                inline.append(lines[i])
                i += 1
            if i == len(lines):
                raise GenerationError(f"Unterminated inline response file in {path}")
            i += 1
            command = command.replace("@<<", " ".join(inline), 1)
        compiler_cwd = projects[-1].parent if projects else (
            msbuild_project.parent if msbuild_project else cwd
        )
        try:
            args = expand_response(windows_args(command), compiler_cwd)
            unit = parse_compilation(match[1], args, compiler_cwd)
            unit.project = projects[-1] if projects else msbuild_project
            units.append(unit)
        except GenerationError as error:
            raise GenerationError(f"{path}, near line {i}: {error}") from error
    if not units:
        raise GenerationError(INCOMPLETE_LOG)
    return units


def walk_files(root: Path, excluded: tuple[str, ...] = ()):
    """Follow junctions explicitly, but never revisit the same physical directory."""
    seen: set[str] = set()
    def on_error(error):
        raise error
    for directory, dirs, files in os.walk(root, followlinks=True, onerror=on_error):
        key = os.path.normcase(str(Path(directory).resolve()))
        if key in seen:
            dirs[:] = []
            continue
        seen.add(key)
        dirs[:] = sorted(d for d in dirs if d.lower() not in (".git", ".venv", "__pycache__", *excluded))
        for name in sorted(files):
            yield Path(directory) / name


def choose(paths: list[Path], description: str, interactive: bool) -> Path:
    if not paths:
        raise GenerationError(f"No {description} found.")
    if len(paths) == 1:
        return paths[0]
    listing = "\n".join(f"  {i}. {path}" for i, path in enumerate(paths, 1))
    if not interactive:
        raise GenerationError(f"Ambiguous {description}:\n{listing}\nSpecify the path explicitly.")
    print(f"Several {description} candidates:\n{listing}")
    while True:
        try:
            value = input(f"Select 1-{len(paths)} (or q to cancel): ").strip()
        except EOFError as error:
            raise GenerationError("Selection requires input; specify the path explicitly.") from error
        if value.lower() == "q":
            raise GenerationError("Cancelled.")
        if value.isdigit() and 1 <= int(value) <= len(paths):
            return paths[int(value) - 1]
        print("Invalid selection.")


def find_log(root: Path, part: str, interactive: bool) -> Path:
    if not root.is_dir():
        raise GenerationError(f"Log directory does not exist: {root}")
    expected = f"{part}_build.log".casefold()
    return choose([p for p in walk_files(root) if p.name.casefold() == expected],
                  f"build log for {part} under {root}", interactive)


def part_definition(repo: Path, part: str, explicit: Path | None, interactive: bool):
    candidates = [explicit] if explicit else [
        p for p in walk_files(repo, excluded=("bb",))
        if p.name.lower().endswith(".partfile.xml")
    ]
    matches: dict[Path, ET.Element] = {}
    for path in candidates:
        if not explicit and not re.search(
            r"""<Part\b[^>]*\bName\s*=\s*["']""" + re.escape(part) + r"""["']""",
            read_text(path), re.I,
        ):
            continue
        root = ET.parse(path).getroot()
        parts = [e for e in root.iter("Part") if e.get("Name", "").casefold() == part.casefold()]
        if len(parts) > 1:
            raise GenerationError(f"Duplicate Part {part} in {path}")
        if parts:
            matches[path] = parts[0]
    path = choose(list(matches), f"PartFile defining {part}", interactive)
    definition = matches[path]
    makefile = definition.get("BMakeFile")
    if not makefile:
        raise GenerationError(f"Part {part} has no BMakeFile: aggregate Parts are not yet supported.")
    make_path = absolute(makefile, path.parent)
    if not make_path.is_file():
        raise GenerationError(f"Part makefile is missing: {make_path}")
    return path, definition, make_path


MACRO = re.compile(r"\$\(([^)]+)\)|\$\{([^}]+)\}|\$\[([^]]+)\]")
ASSIGNMENT = re.compile(r"^([\w.-]+)\s*(=\s*%?|\+=|\+)\s*(.*)$")
SOURCE_SUFFIXES = (".c", ".cpp", ".cxx", ".cc", ".cs")
INCOMPLETE_LOG = ("The build log is incomplete. Perform a full rebuild of the Part "
                  "before generating the Visual Studio project. No project was written.")


def make_lines(path: Path) -> list[tuple[int, str]]:
    result: list[tuple[int, str]] = []
    pending = ""
    first_line = 0
    for number, original in enumerate(read_text(path).splitlines(), 1):
        # Makefile comments are not source declarations.
        line = re.split(r'#(?=(?:[^"]*"[^"]*")*[^"]*$)', original, maxsplit=1)[0].strip()
        if not line:
            continue
        if not pending:
            first_line = number
        if line.endswith("\\") and (len(line) == 1 or line[-2].isspace()):
            pending += line[:-1] + " "
        else:
            result.append((first_line, pending + line))
            pending = ""
    if pending:
        raise GenerationError(f"Unterminated makefile continuation: {path}:{first_line}")
    return result


class SourceManifest:
    """Conservative static coverage check, not a replacement for the bmake evaluator."""

    def __init__(self, makefile: Path, part_file: Path, src_root: Path, out_root: Path):
        self.makefile = makefile
        self.cwd = part_file.parent
        self.expected: dict[Path, str] = {}
        self.seen: set[Path] = set()
        self.active_makefiles: set[Path] = set()
        self.definitions: dict[str, set[str]] = {}
        self.mki_files: dict[str, list[Path]] = {}
        self.defaults = {
            "_MakeFilePath": str(makefile.parent) + os.sep,
            "_MakeFileSpec": str(makefile),
            "SrcRoot": str(src_root) + os.sep,
            "OutRoot": str(out_root) + os.sep,
            # BB buildpart.py supplies the platform-specific OutputRootDir.
            "OutputRootDir": str(out_root / "Winx64") + os.sep,
        }
        # common.mki's documented baseDir default; do not assume a srcDir.
        common = src_root / "bsicommon" / "PublicSDK" / "common.mki"
        if common.is_file() and re.search(r"(?im)^\s*baseDir\s*=\s*\$\(_MakeFilePath\)", read_text(common)):
            self.defaults["baseDir"] = self.defaults["_MakeFilePath"]
        policy_dirs = [part_file.parent / "privmki", makefile.parent]
        indexed: set[Path] = set()
        for directory in policy_dirs:
            if not directory.is_dir():
                continue
            for path in walk_files(directory):
                if path.suffix.lower() != ".mki" or path.resolve() in indexed:
                    continue
                indexed.add(path.resolve())
                self.mki_files.setdefault(path.name.casefold(), []).append(path)
                for _, line in make_lines(path):
                    assignment = ASSIGNMENT.match(line)
                    if assignment and assignment[2].replace(" ", "") in ("=", "=%"):
                        self.definitions.setdefault(assignment[1], set()).add(assignment[3])

    def expand(self, text: str, local: dict[str, str], stack: tuple[str, ...] = ()) -> str:
        def replace(match):
            name = next(group for group in match.groups() if group is not None)
            if name in stack:
                raise GenerationError(f"Recursive source manifest macro: {name}")
            if name in local:
                value = local[name]
            else:
                values = self.definitions.get(name, set())
                if len(values) != 1:
                    reason = "ambiguous" if values else "undefined"
                    raise GenerationError(f"Cannot verify source coverage: {reason} macro {name} in {text}")
                value = next(iter(values))
            return self.expand(value, local, (*stack, name))
        result = MACRO.sub(replace, text)
        if "$" in result:
            raise GenerationError(f"Cannot verify dynamic source expression: {text}")
        return result

    def add_source(self, value: str, origin: str):
        if any(c in value for c in "*?"):
            raise GenerationError(f"Cannot verify dynamic makefile source pattern {value}: {origin}")
        self.expected[absolute(value, self.cwd).resolve()] = origin

    def add_managed_sources(self, local: dict[str, str], origin: str):
        if "ASSEMBLY_SOURCE_LIST" not in local:
            raise GenerationError(f"Cannot verify ASSEMBLY_SOURCE_LIST: {origin}")
        for source in windows_args(self.expand(local["ASSEMBLY_SOURCE_LIST"], local)):
            if not source.lower().endswith(".cs"):
                raise GenerationError(f"Cannot verify C# source declaration: {origin}")
            self.add_source(source, origin)

    def read_project(self, path: Path):
        path = path.resolve()
        if path in self.seen:
            return
        self.seen.add(path)
        root = ET.parse(path).getroot()
        if root.get("Sdk"):
            raise GenerationError(f"Cannot verify implicit SDK source items: {path}")
        tag_name = lambda element: element.tag.rsplit("}", 1)[-1]
        for element in root.iter():
            tag = tag_name(element)
            if tag not in ("Compile", "ClCompile"):
                continue
            include = element.get("Include")
            if not include:
                if element.get("Remove") or element.get("Update"):
                    raise GenerationError(f"Cannot statically verify {tag} Remove/Update in {path}")
                continue
            if element.get("Exclude"):
                raise GenerationError(f"Cannot statically verify {tag} Exclude in {path}")
            for value in include.split(";"):
                value = value.replace("$(MSBuildThisFileDirectory)", str(path.parent) + os.sep)
                if "$" in value or "@(" in value or "%(" in value:
                    raise GenerationError(f"Cannot verify project source expression {value}: {path}")
                paths = list(path.parent.glob(value)) if any(c in value for c in "*?") else [
                    absolute(value, path.parent)
                ]
                if not paths:
                    raise GenerationError(f"Source pattern matches no files: {value} in {path}")
                for source in paths:
                    self.expected[source.resolve()] = str(path)
        for element in root.iter():
            if tag_name(element) != "Import":
                continue
            value = element.get("Project", "")
            # Standard compiler targets may generate extra sources, but do not replace
            # the explicit project source list being checked.
            if value.startswith(("$(MSBuildToolsPath)", "$(VCTargetsPath)",
                                 "$(MSBuildExtensionsPath)", "$(MSBuildExtensionsPath32)")):
                continue
            value = value.replace("$(MSBuildThisFileDirectory)", str(path.parent) + os.sep)
            if "$" in value or any(c in value for c in "*?"):
                raise GenerationError(f"Cannot verify imported source items: {value} in {path}")
            self.read_project(absolute(value, path.parent))

    def read_makefile(self, path: Path, inherited: dict[str, str] | None = None):
        path = path.resolve()
        if path in self.active_makefiles:
            raise GenerationError(f"Cannot verify cyclic makefile includes: {path}")
        self.active_makefiles.add(path)
        local = dict(inherited if inherited is not None else self.defaults)
        conditional_depth = 0
        for number, line in make_lines(path):
            origin = f"{path}:{number}"
            if re.match(r"(?i)^%if\b", line):
                conditional_depth += 1
            elif re.match(r"(?i)^%endif\b", line):
                conditional_depth -= 1
            assignment = ASSIGNMENT.match(line)
            if assignment:
                name, operator, value = assignment.groups()
                previous = local.get(name)
                if operator.startswith("+"):
                    local[name] = local.get(name, "") + " " + value
                elif conditional_depth and previous is not None and previous != value:
                    local[name] = "$UNVERIFIED_CONDITIONAL_" + name
                else:
                    local[name] = value
                if name == "MSBUILD_PROJECT_PATH":
                    expanded = self.expand(value, local)
                    self.read_project(absolute(expanded.strip('"'), self.cwd))
                continue
            include = re.match(r"(?i)^%include\s+(.+)$", line)
            if include:
                value = include[1].strip('"')
                # Local includes can contribute source lists. Policy defaults are
                # indexed separately; shared compiler inference rules are not sources.
                basename = MACRO.sub("", re.split(r"[\\/]", value)[-1])
                if basename.casefold() == "mkcsharp.mki":
                    self.add_managed_sources(local, origin)
                    continue
                if not basename or "$" in basename:
                    expanded = self.expand(value, local)
                    candidate = absolute(expanded, path.parent)
                    if not candidate.is_file():
                        raise GenerationError(f"Cannot verify included makefile {expanded}: {origin}")
                    self.read_makefile(candidate, local)
                    continue
                candidates = self.mki_files.get(basename.casefold(), [])
                if len(candidates) > 1:
                    raise GenerationError(f"Ambiguous local makefile include {value}: {origin}")
                if candidates:
                    self.read_makefile(candidates[0], local)
                continue
            rule = re.search(r":(?![\\/])", line)
            if not rule:
                continue
            target, dependencies = line[:rule.start()], line[rule.end():]
            if not re.search(r"\$(?:\(oext\)|\{oext\}|\[oext\])|\.(?:obj|o)(?:\s|$)", target, re.I):
                continue
            tokens = windows_args(dependencies)
            for index, token in enumerate(tokens):
                if token.lower().endswith(SOURCE_SUFFIXES) or (index == 0 and MACRO.fullmatch(token)):
                    expanded = self.expand(token, local)
                    sources = [expanded] if token.lower().endswith(SOURCE_SUFFIXES) else windows_args(expanded)
                    for source in sources:
                        if source.lower().endswith(SOURCE_SUFFIXES):
                            self.add_source(source, origin)
        self.active_makefiles.remove(path)
        if inherited is not None:
            inherited.update(local)

    def verify(self, units: list[Compilation]) -> int:
        self.read_makefile(self.makefile)
        # MSBuild wrappers can put their project path in policy macros rather than
        # a literal in the .mke. Check the original project logged for each compiler.
        for unit in units:
            if unit.project:
                self.read_project(unit.project)
        if not self.expected:
            raise GenerationError(f"Cannot determine source files from {self.makefile}; "
                                  "source coverage is unverified. No project was written.")
        logged = {path.resolve() for unit in units for path in unit.sources}
        missing = sorted(set(self.expected) - logged)
        if missing:
            raise GenerationError(INCOMPLETE_LOG)
        return len(self.expected)


def guid(value: str) -> str:
    return "{" + str(uuid.uuid5(uuid.NAMESPACE_URL, value)).upper() + "}"


def node(parent, tag: str, text: str | None = None, **attrs):
    element = ET.SubElement(parent, f"{{{NS}}}{tag}", attrs)
    element.text = text
    return element


def xml_text(root: ET.Element) -> str:
    ET.indent(root, space="  ")
    return '<?xml version="1.0" encoding="utf-8"?>\n' + ET.tostring(root, encoding="unicode") + "\n"


def msbuild_escape(value: str) -> str:
    for char in ("%", "$", "@", "'", ";", "?", "*"):
        value = value.replace(char, f"%{ord(char):02X}")
    return value


def mslist(values) -> str:
    return ";".join(msbuild_escape(str(v)) for v in values)


def linked_item(group, tag: str, path: Path, source_root: Path):
    element = node(group, tag, Include=msbuild_escape(str(path)))
    try:
        link = path.relative_to(source_root)
    except ValueError:
        link = Path("External") / path.name
    node(element, "Link", str(link))
    return element


def cpp_project(name: str, units: list[Compilation], source_root: Path, command: str,
                debug_exe: Path | None) -> dict[str, str]:
    root = ET.Element(f"{{{NS}}}Project", DefaultTargets="Build")
    configurations = node(root, "ItemGroup", Label="ProjectConfigurations")
    config = node(configurations, "ProjectConfiguration", Include="FromLog|x64")
    node(config, "Configuration", "FromLog")
    node(config, "Platform", "x64")
    globals_group = node(root, "PropertyGroup", Label="Globals")
    node(globals_group, "ProjectGuid", guid(name))
    node(globals_group, "Keyword", "MakeFileProj")
    node(globals_group, "ProjectName", name)
    node(root, "Import", Project=r"$(VCTargetsPath)\Microsoft.Cpp.Default.props")
    config_group = node(root, "PropertyGroup", Label="Configuration")
    node(config_group, "ConfigurationType", "Makefile")
    node(config_group, "PlatformToolset", "v143")
    node(root, "Import", Project=r"$(VCTargetsPath)\Microsoft.Cpp.props")
    properties = node(root, "PropertyGroup")
    for action in ("Build", "ReBuild", "Clean"):
        node(properties, f"NMake{action}CommandLine", command + " " + action.lower())
    node(properties, "OutDir", "$(ProjectDir).vs-output\\$(ProjectName)\\")
    node(properties, "IntDir", "$(ProjectDir).vs-output\\$(ProjectName)\\")
    node(properties, "NMakeIncludeSearchPath", mslist(units[0].includes))
    node(properties, "NMakePreprocessorDefinitions", mslist(units[0].defines))
    node(properties, "NMakeForcedIncludes", mslist(units[0].forced))
    node(properties, "NMakeAssemblySearchPath", mslist(units[0].assembly_dirs))
    node(properties, "NMakeForcedUsingAssemblies", mslist(path for _, path in units[0].references))
    node(properties, "AdditionalOptions", subprocess.list2cmdline(units[0].options))
    items = node(root, "ItemGroup")
    settings_by_source: dict[Path, Compilation] = {}
    for unit in units:
        for path in unit.sources:
            previous = settings_by_source.get(path)
            signature = lambda u: (u.includes, u.defines, u.undefines, u.forced,
                                   u.standard, u.options, u.clr, u.references, u.assembly_dirs,
                                   u.source_modes.get(path, u.compile_as))
            if previous and signature(previous) != signature(unit):
                raise GenerationError(f"Conflicting logged compiler settings for {path}")
            settings_by_source[path] = unit
    for path, unit in settings_by_source.items():
        item = linked_item(items, "ClCompile", path, source_root)
        node(item, "AdditionalIncludeDirectories", mslist(unit.includes))
        node(item, "PreprocessorDefinitions", mslist(unit.defines))
        node(item, "UndefinePreprocessorDefinitions", mslist(unit.undefines))
        node(item, "ForcedIncludeFiles", mslist(unit.forced))
        node(item, "AdditionalUsingDirectories", mslist(unit.assembly_dirs))
        node(item, "ForcedUsingFiles", mslist(path for _, path in unit.references))
        node(item, "AdditionalOptions", subprocess.list2cmdline(unit.options))
        compile_as = unit.source_modes.get(path, unit.compile_as)
        if compile_as:
            node(item, "CompileAs", compile_as)
        if unit.clr:
            node(item, "CompileAsManaged", "true")
        if unit.standard:
            standards = {"c++14": "stdcpp14", "c++17": "stdcpp17", "c++20": "stdcpp20",
                         "c++latest": "stdcpplatest", "c11": "stdc11", "c17": "stdc17"}
            if unit.standard not in standards:
                raise GenerationError(f"Unsupported logged C++ standard: {unit.standard}")
            node(item, "LanguageStandard_C" if unit.standard.startswith("c1") else "LanguageStandard",
                 standards[unit.standard])
    headers = [p for p in walk_files(source_root) if p.suffix.lower() in (".h", ".hpp", ".hxx", ".inl", ".fdf")]
    for path in headers:
        linked_item(items, "ClInclude", path, source_root)
    node(root, "Import", Project=r"$(VCTargetsPath)\Microsoft.Cpp.targets")
    filters = ET.Element(f"{{{NS}}}Project", ToolsVersion="4.0")
    filter_defs = node(filters, "ItemGroup")
    filter_items = node(filters, "ItemGroup")
    directories: set[str] = set()
    for tag, paths in (("ClCompile", settings_by_source), ("ClInclude", headers)):
        for path in paths:
            try:
                relative = path.relative_to(source_root)
                folder = str(relative.parent) if relative.parent != Path(".") else ""
            except ValueError:
                folder = "External"
            item = node(filter_items, tag, Include=msbuild_escape(str(path)))
            if folder:
                node(item, "Filter", folder)
                parent = Path(folder)
                while parent != Path("."):
                    directories.add(str(parent))
                    parent = parent.parent
    for directory in sorted(directories):
        node(node(filter_defs, "Filter", Include=directory), "UniqueIdentifier", guid(name + directory))
    result = {name + ".vcxproj": xml_text(root), name + ".vcxproj.filters": xml_text(filters)}
    if debug_exe:
        user = ET.Element(f"{{{NS}}}Project")
        props = node(user, "PropertyGroup")
        node(props, "LocalDebuggerCommand", msbuild_escape(str(debug_exe)))
        node(props, "LocalDebuggerWorkingDirectory", msbuild_escape(str(debug_exe.parent)))
        node(props, "DebuggerFlavor", "WindowsLocalDebugger")
        result[name + ".vcxproj.user"] = xml_text(user)
    return result


def framework_from_log(unit: Compilation) -> str:
    frameworks: set[str] = set()
    for _, path in unit.references:
        match = re.search(r"(?i)[\\/]\.NETFramework[\\/]v(\d+(?:\.\d+)+)[\\/]", path)
        if match:
            frameworks.add("v" + match[1])
    for source in unit.sources:
        if source.name.casefold() != "assemblyinfo.cs" or not source.is_file():
            continue
        frameworks.update(re.findall(
            r'\bTargetFramework(?:Attribute)?\s*\(\s*"\.NETFramework,Version=(v\d+(?:\.\d+)+)"',
            read_text(source),
        ))
    if len(frameworks) != 1:
        raise GenerationError("Cannot identify a single .NET Framework version from C# references "
                              "and logged AssemblyInfo.cs inputs. "
                              "Supply --framework (for example v4.8).")
    return frameworks.pop()


def cs_project(name: str, unit: Compilation, source_root: Path, command: str,
               framework: str | None, debug_exe: Path | None) -> dict[str, str]:
    framework = framework or framework_from_log(unit)
    if not re.fullmatch(r"v\d+(?:\.\d+)+", framework):
        raise GenerationError("This version supports C# .NET Framework projects; use e.g. --framework v4.8.")
    root = ET.Element(f"{{{NS}}}Project", ToolsVersion="Current", DefaultTargets="Build")
    props = node(root, "PropertyGroup")
    for key, value in {
        "Configuration": "FromLog", "Platform": "x64", "ProjectGuid": guid(name),
        "TargetFrameworkVersion": framework,
        "OutputType": {"library": "Library", "exe": "Exe", "winexe": "WinExe"}.get(unit.target, ""),
        "AssemblyName": Path(unit.output).stem if unit.output else name,
        "DefineConstants": mslist(unit.defines), "PlatformTarget": unit.platform or "AnyCPU",
        "OutputPath": ".vs-output\\" + name + "\\", "BaseIntermediateOutputPath": ".vs-output\\" + name + "\\obj\\",
        "AllowUnsafeBlocks": str(unit.unsafe).lower(), "EnableDefaultItems": "false",
    }.items():
        node(props, key, value)
    if unit.target not in ("library", "exe", "winexe"):
        raise GenerationError(f"Unsupported C# target: {unit.target}")
    if unit.standard:
        node(props, "LangVersion", unit.standard)
    if debug_exe:
        node(props, "StartAction", "Program")
        node(props, "StartProgram", msbuild_escape(str(debug_exe)))
        node(props, "StartWorkingDirectory", msbuild_escape(str(debug_exe.parent)))
    sources = node(root, "ItemGroup")
    for path in unit.sources:
        linked_item(sources, "Compile", path, source_root)
    refs = node(root, "ItemGroup")
    for alias, path in unique(unit.references):
        ref = node(refs, "Reference", Include=msbuild_escape(Path(path).stem))
        node(ref, "HintPath", msbuild_escape(path))
        node(ref, "Private", "false")
        if alias:
            node(ref, "Aliases", alias)
    node(root, "Import", Project=r"$(MSBuildToolsPath)\Microsoft.CSharp.targets")
    # Override after the import: normal builds go to BB; design-time targets remain available.
    for action in ("Build", "Rebuild", "Clean"):
        target = node(root, "Target", Name=action, Condition="'$(DesignTimeBuild)' != 'true'")
        node(target, "Exec", Command=command + " " + action.lower())
    return {name + ".csproj": xml_text(root)}


def solution(projects: list[tuple[str, str, str]]) -> str:
    lines = ["Microsoft Visual Studio Solution File, Format Version 12.00",
             "# Visual Studio Version 17"]
    for name, filename, kind in projects:
        lines.extend([f'Project("{kind}") = "{name}", "{filename}", "{guid(name)}"', "EndProject"])
    lines.extend(["Global", "\tGlobalSection(SolutionConfigurationPlatforms) = preSolution",
                  "\t\tFromLog|x64 = FromLog|x64", "\tEndGlobalSection",
                  "\tGlobalSection(ProjectConfigurationPlatforms) = postSolution"])
    for index, (name, _, _) in enumerate(projects):
        lines.append(f"\t\t{guid(name)}.FromLog|x64.ActiveCfg = FromLog|x64")
        # Each generated project builds the entire selected Part. Schedule it only once.
        if index == 0:
            lines.append(f"\t\t{guid(name)}.FromLog|x64.Build.0 = FromLog|x64")
    lines.extend(["\tEndGlobalSection", "EndGlobal", ""])
    return "\n".join(lines)


SOLUTION_PROJECT = re.compile(
    r'^Project\("(?P<kind>\{[^}]+\})"\) = "(?P<name>[^"]+)", '
    r'"(?P<path>[^"]+)", "(?P<guid>\{[^}]+\})"\n.*?^EndProject$', re.M | re.S,
)


def merge_solution(existing: str | None, projects: list[tuple[str, str, str]],
                   folders: tuple[str, ...] = ()) -> str:
    text = existing.replace("\r\n", "\n") if existing is not None else solution([])
    if not text.startswith("Microsoft Visual Studio Solution File, Format Version 12.00"):
        raise GenerationError("Unsupported MyBentley.sln format; the solution was not changed.")
    blocks = list(SOLUTION_PROJECT.finditer(text))
    additions: list[str] = []
    mappings: list[str] = []
    new_projects: list[str] = []
    for index, (name, filename, kind) in enumerate(projects):
        project_guid = guid(name)
        matching = [block for block in blocks if
                    block["guid"].casefold() == project_guid.casefold()
                    or (block["kind"].casefold() != SOLUTION_FOLDER_GUID.casefold() and (
                        block["path"].casefold() == filename.casefold()
                        or block["name"].casefold() == name.casefold()))]
        if matching:
            if len(matching) != 1 or (
                matching[0]["guid"].casefold(), matching[0]["path"].casefold(),
                matching[0]["name"].casefold(), matching[0]["kind"].casefold()
            ) != (project_guid.casefold(), filename.casefold(), name.casefold(), kind.casefold()):
                raise GenerationError(f"Conflicting project in MyBentley.sln: {name}")
            continue
        additions.append(f'Project("{kind}") = "{name}", "{filename}", "{project_guid}"\nEndProject\n')
        new_projects.append(project_guid)
        mappings.append(f"\t\t{project_guid}.FromLog|x64.ActiveCfg = FromLog|x64")
        if index == 0:
            mappings.append(f"\t\t{project_guid}.FromLog|x64.Build.0 = FromLog|x64")
    nesting: list[str] = []
    if folders and new_projects:
        parents: dict[str, str] = {}
        nested = re.search(r"\tGlobalSection\(NestedProjects\) = \w+\n(.*?)\tEndGlobalSection",
                           text, re.S)
        if nested:
            for child, parent in re.findall(r"(?m)^\s*(\{[^}]+\})\s*=\s*(\{[^}]+\})\s*$", nested[1]):
                key = child.casefold()
                if key in parents and parents[key] != parent.casefold():
                    raise GenerationError("Conflicting NestedProjects entries in MyBentley.sln.")
                parents[key] = parent.casefold()
        parent_guid = ""
        for folder in folders:
            matching_folders = [block for block in blocks
                                if block["kind"].casefold() == SOLUTION_FOLDER_GUID.casefold()
                                and block["name"].casefold() == folder.casefold()
                                and parents.get(block["guid"].casefold(), "") == parent_guid.casefold()]
            if len(matching_folders) > 1:
                raise GenerationError(f"Ambiguous solution folder in MyBentley.sln: {folder}")
            if matching_folders:
                folder_guid = matching_folders[0]["guid"]
            else:
                folder_guid = guid("solution-folder:" + parent_guid.casefold() + ":" + folder.casefold())
                if any(block["guid"].casefold() == folder_guid.casefold() for block in blocks) \
                        or folder_guid in new_projects:
                    raise GenerationError(f"Conflicting solution folder in MyBentley.sln: {folder}")
                additions.append(
                    f'Project("{SOLUTION_FOLDER_GUID}") = "{folder}", "{folder}", "{folder_guid}"\nEndProject\n')
                if parent_guid:
                    nesting.append(f"\t\t{folder_guid} = {parent_guid}")
            parent_guid = folder_guid
        nesting.extend(f"\t\t{project_guid} = {parent_guid}" for project_guid in new_projects)
    if text.count("\nGlobal\n") != 1:
        raise GenerationError("Cannot locate the Global section of MyBentley.sln.")
    text = text.replace("\nGlobal\n", "\n" + "".join(additions) + "Global\n", 1)
    for section, phase, new_lines in (
        ("SolutionConfigurationPlatforms", "preSolution", ["\t\tFromLog|x64 = FromLog|x64"]),
        ("ProjectConfigurationPlatforms", "postSolution", mappings),
        *([("NestedProjects", "preSolution", nesting)] if nesting else []),
    ):
        pattern = re.compile(r"(\tGlobalSection\(" + section + r"\) = \w+\n)(.*?)(\tEndGlobalSection)", re.S)
        match = pattern.search(text)
        if match:
            lines = [line for line in new_lines if line not in match[2].splitlines()]
            text = text[:match.start()] + match[1] + match[2] + "".join(
                line + "\n" for line in lines
            ) + match[3] + text[match.end():]
        else:
            if not re.search(r"(?m)^EndGlobal$", text):
                raise GenerationError("Cannot locate EndGlobal in MyBentley.sln.")
            content = f"\tGlobalSection({section}) = {phase}\n" + "".join(
                line + "\n" for line in new_lines
            ) + "\tEndGlobalSection\n"
            text = re.sub(r"(?m)^EndGlobal$", lambda _: content + "EndGlobal", text, count=1)
    return text


def legacy_projects(output: Path) -> list[tuple[Path, list[tuple[str, str, str]], list[Path]]]:
    """Recognize only unmodified per-Part solutions produced by the old generator."""
    result = []
    for path in sorted(output.glob("*.sln")):
        if path.name.casefold() == "mybentley.sln":
            continue
        text = read_text(path).replace("\r\n", "\n")
        blocks = list(SOLUTION_PROJECT.finditer(text))
        records = [(b["name"], b["path"], b["kind"]) for b in blocks]
        if not records or text != solution(records) or records[0][0] != path.stem:
            continue
        files = [output / (path.stem + ".build.cmd")]
        for _, filename, _ in records:
            project = output / filename
            if project.parent != output or project.suffix not in (".csproj", ".vcxproj"):
                raise GenerationError(f"Unsupported legacy project path: {project}")
            files.append(project)
            for suffix in (".filters", ".user"):
                extra = Path(str(project) + suffix)
                if extra.is_file():
                    files.append(extra)
        if not all(p.is_file() for p in files):
            raise GenerationError(f"Cannot migrate incomplete legacy project: {path}")
        result.append((path, records, files))
    return result


def static_environment(repo: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    path = repo / "bb" / "env.bat"
    if path.is_file():
        for line in read_text(path).splitlines():
            match = re.match(r'(?i)^\s*set\s+"?(SrcRoot|OutRoot)=([^"%]+)"?\s*$', line)
            if match:
                result[match[1].lower()] = match[2].strip()
    return result


def batch_arg(value: str) -> str:
    # Every argument is quoted; percent expansion is escaped in a batch file.
    if any(c in value for c in '"\r\n'):
        raise GenerationError(f"Unsupported character in batch argument: {value!r}")
    return '"' + value.replace("%", "%%") + '"'


def workspace_roots() -> tuple[Path, Path]:
    app_dir = Path(__file__).resolve().parent
    return app_dir.parent.parent, app_dir.parent


def build_wrapper(repo: Path, src_root: Path, out_root: Path, part_file: Path, part: str,
                  strategy: str, repository: str) -> str:
    python = src_root / "BentleyBuild" / ".venv" / "Scripts" / "python.exe"
    bb = src_root / "BentleyBuild" / "BentleyBuild.py"
    env = repo / "env.bat"
    for path in (python, bb, env):
        if not path.is_file():
            raise GenerationError(f"Required build launcher file is missing: {path}")
    args = ["--repository", repository, "--partFile", str(part_file),
            "--partName", part, "--srcroot", str(src_root), "--outputroot", str(out_root),
            "--noSubParts"]
    return "\n".join([
        "@echo off", "setlocal DisableDelayedExpansion",
        'if /i not "%~1"=="build" if /i not "%~1"=="rebuild" if /i not "%~1"=="clean" exit /b 2',
        f"call {batch_arg(str(env))} {batch_arg(strategy)}",
        "if errorlevel 1 exit /b %errorlevel%",
        f"pushd {batch_arg(str(repo))}", "if errorlevel 1 exit /b %errorlevel%",
        " ".join(batch_arg(v) for v in (str(python), str(bb), *args)) + ' "%~1"',
        'set "result=%errorlevel%"', "popd", "exit /b %result%", "",
    ])


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("project_folder", help="Absolute project folder or path relative to the current working directory")
    result.add_argument("part", help="Part name, for example tfelement")
    result.add_argument("--repository", help="Override the BB repository name (defaults to project folder name)")
    result.add_argument("--part-file", type=Path, help="Explicit PartFile.xml when ambiguous")
    result.add_argument("--out-root", type=Path, help="Default: OutRoot, bb/env.bat, then projectFolder/bb/out")
    result.add_argument("--src-root", type=Path, help="Default: SrcRoot or a literal definition in bb/env.bat")
    result.add_argument("--log", type=Path, help="Explicit log file instead of recursive lookup")
    result.add_argument("--output", type=Path, help="Output directory (default: parent of ConvertBB2VS)")
    result.add_argument("--strategy", help="Argument passed to repository env.bat (defaults to repository name)")
    result.add_argument("--framework", help="Explicit C# .NET Framework version, e.g. v4.8")
    result.add_argument("--debug-exe", type=Path, help="Host program launched by the VS debugger")
    result.add_argument("--non-interactive", action="store_true", help="Fail on ambiguous matches")
    result.add_argument("--force", action="store_true", help="Replace existing generated files")
    return result


def generate(args: argparse.Namespace) -> list[Path]:
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", args.part) or args.part.endswith("."):
        raise GenerationError("Part name must contain only letters, digits, underscores, dots or hyphens.")
    _, default_output = workspace_roots()
    repo = Path(args.project_folder).resolve()
    if not repo.is_dir():
        raise GenerationError(f"Repository directory does not exist: {repo}")
    repository = args.repository or repo.name
    strategy = args.strategy or repository
    config = static_environment(repo)
    out_root = (args.out_root or Path(os.environ.get("OutRoot") or config.get("outroot") or repo / "bb" / "out")).resolve()
    src_value = args.src_root or os.environ.get("SrcRoot") or config.get("srcroot")
    if not src_value:
        raise GenerationError("SrcRoot is undefined. Set it or pass --src-root; no value was assumed.")
    src_root = Path(src_value).resolve()
    interactive = not args.non_interactive
    log = args.log.resolve() if args.log else find_log(out_root / "Winx64" / "LogFiles", args.part, interactive)
    explicit = args.part_file.resolve() if args.part_file else None
    # Prefer the authoritative PartFile printed in this Part's own log.
    header = re.search(r"(?im)^\s*Building Part:\s*(.*?)\s+<([^>]+)>", read_text(log))
    if header:
        if header[2].casefold() != args.part.casefold():
            raise GenerationError(f"Log belongs to Part {header[2]}, not {args.part}: {log}")
        if not explicit and Path(header[1]).is_file():
            explicit = Path(header[1])
    part_file, definition, makefile = part_definition(repo, args.part, explicit, interactive)
    units = parse_log(log, part_file.parent)
    covered = SourceManifest(makefile, part_file, src_root, out_root).verify(units)
    for unit in units:
        for path in unit.sources:
            if not path.is_file():
                raise GenerationError(INCOMPLETE_LOG)
    if args.debug_exe:
        args.debug_exe = args.debug_exe.resolve()
        if not args.debug_exe.is_file():
            raise GenerationError(f"Debugger host does not exist: {args.debug_exe}")
    name = definition.attrib["Name"]
    files: dict[str, str] = {
        name + ".build.cmd": build_wrapper(repo, src_root, out_root, part_file, name,
                                          strategy, repository)
    }
    command = 'call "$(ProjectDir)' + name + '.build.cmd"'
    projects: list[tuple[str, str, str]] = []
    native = [u for u in units if u.language == "cpp"]
    managed = [u for u in units if u.language == "cs"]
    if native:
        files.update(cpp_project(name, native, makefile.parent, command, args.debug_exe))
        projects.append((name, name + ".vcxproj", CPP_GUID))
    seen_outputs: set[str] = set()
    for index, unit in enumerate(managed):
        if unit.output in seen_outputs:
            raise GenerationError(f"Multiple C# compilations of {unit.output}; use a log for a single build/configuration.")
        seen_outputs.add(unit.output)
        cs_name = name if not native and len(managed) == 1 else f"{name}.Managed{index + 1}"
        files.update(cs_project(cs_name, unit, unit.cwd, command, args.framework, args.debug_exe))
        projects.append((cs_name, cs_name + ".csproj", CS_GUID))
    output = (args.output or default_output).resolve()
    project_dir = output / "projects"
    paths = [project_dir / filename for filename in files]
    existing = [p for p in paths if p.exists()]
    if existing and not args.force:
        raise GenerationError("Output files already exist (use --force to replace):\n" + "\n".join(map(str, existing)))
    solution_path = output / "MyBentley.sln"
    solution_text = read_text(solution_path) if solution_path.exists() else None
    migrations = legacy_projects(output)
    for _, records, old_files in migrations:
        solution_text = merge_solution(solution_text, [
            (n, str(Path("projects") / f), k) for n, f, k in records
        ])
        for old in old_files:
            if (project_dir / old.name).exists():
                raise GenerationError(f"Cannot migrate {old}: destination already exists.")
    solution_text = merge_solution(solution_text, [
        (n, str(Path("projects") / f), k) for n, f, k in projects
    ], folders=tuple(name for name in (repo.parent.name, repo.name) if name))
    project_dir.mkdir(parents=True, exist_ok=True)
    for _, _, old_files in migrations:
        for old in old_files:
            old.rename(project_dir / old.name)
    for filename, content in files.items():
        (project_dir / filename).write_text(content, encoding="utf-8", newline="\r\n")
    solution_path.write_text(solution_text, encoding="utf-8-sig", newline="\r\n")
    for old_solution, _, _ in migrations:
        old_solution.unlink()
    print(f"Part: {name}\nLog: {log}\nCompiler invocations: {len(units)}\n"
          f"Source coverage: {covered}/{covered} declared files")
    return [*paths, solution_path]


def main() -> int:
    args = parser().parse_args()
    try:
        for path in generate(args):
            print(path)
        return 0
    except (GenerationError, OSError, ET.ParseError, UnicodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Cancelled.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
