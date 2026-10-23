# ConvertBB2VS

Python 3.10+ console utility, standard library only. It reads existing Bentley
build logs; it never builds, pulls packages, or modifies source makefiles.
The tool is located at `W:\DevArea\Bentley\VS\ConvertBB2VS`.
The shared solution defaults to `W:\DevArea\Bentley\VS\MyBentley.sln`, and
generated projects and build wrappers are stored in
`W:\DevArea\Bentley\VS\projects`. `--output` changes that output root.

## Usage

From PowerShell in the OpenBuildings repository:

```powershell
..\..\VS\ConvertBB2VS\convert.ps1 . tfelement
```

`convert.ps1` and `convert.bat` accept exactly two arguments: a project folder
specified as an absolute path or relative to the current working directory,
then a Part name. They require Python on
`PATH`, locate the converter beside the launcher, and return its exit code.
Missing or extra arguments print usage and return exit code 2. If PowerShell's
execution policy prevents running the script, invoke it with
`powershell -ExecutionPolicy Bypass -File .\convert.ps1 ...` as permitted by
your local policy. The converter uses the specified project folder as the BB
repository root; BB repository name and build-strategy argument default to
that folder's name.

For diagnostics or custom configurations, invoke Python directly:

```powershell
python .\project_maker.py W:\DevArea\Bentley\OBD.26.Escalations\OpenBuildings tfelement
```

For example, from `W:\DevArea\Bentley`, use
`.\VS\ConvertBB2VS\convert.ps1 .\OBD.26.Escalations\OpenBuildings tfelement`.
The project folder may be outside the Bentley directory. Changing the current
working directory does not change the default solution or project output folder.

The utility searches recursively for `<Part>_build.log` under
`%OutRoot%\Winx64\LogFiles`, case-insensitively. It asks which file to use if
there are multiple matches. `--non-interactive` reports ambiguity as an error.
Use `--log <path>` or `--part-file <path>` to disambiguate explicitly.
Junctions are followed with physical-directory cycle detection.

Root configuration precedence: explicit `--out-root`/`--src-root`, environment,
assignments in the selected project's `bb\env.bat`. Only OutRoot has a
fallback: `<projectFolder>\bb\out`. Batch environment files are read, not
executed during generation. SrcRoot must be configured; its value is never guessed.
Both selected root directories must exist, including when a fallback is used.
`%NAME%` references in paths are expanded from the current environment; an
undefined or empty referenced variable is an error and stops generation.
MSBuild properties such as `$(Platform)` are not environment-variable references.

Open `MyBentley.sln` in Visual Studio 2022 with C++ desktop
development tools and/or the matching .NET Framework targeting pack installed.
Use `--debug-exe <host.exe>` to configure launching a host application; otherwise
attach the debugger to an existing process. No host executable is guessed.

Existing output files are protected; `--force` explicitly permits replacement.
Do not use `--force` on files whose manual edits you want to preserve.
The shared solution is updated automatically: new projects are added under two
nested solution folders named after the last two directories of the resolved
`projectFolder` path. For example, `OBD.26.Escalations\OpenBuildings` produces
`OBD.26.Escalations` > `OpenBuildings`. Existing folders at the corresponding
level are reused. These are virtual solution folders; output paths do not change.
Previously registered projects keep their existing placement.
New project entries are appended,
existing entries and other solution sections are retained, and regenerating a
project does not duplicate its entry. Each Part is scheduled once for a solution
build, including Parts with multiple generated projects.
Solutions saved by Visual Studio with initial blank lines are accepted and
retain those lines and their version metadata.

Unmodified per-Part solutions created by the previous version are recognized
automatically. Their project files, filters, debugger settings and build wrappers
are moved into `projects` and registered in `MyBentley.sln`; those old per-Part
solutions are then removed. Unrecognized or manually edited solutions are not
migrated. A conflicting destination stops generation without overwriting it.

## Generated projects

- C++: Makefile `.vcxproj`, `.vcxproj.filters`, optional `.vcxproj.user`.
  Compiler include paths, definitions, undefines, forced includes, language
  standard, forced source language (`/TP`, `/TC`) and selected language switches
  are captured per source file.
  C++/CLI assembly references and assembly search paths are retained.
- C#: `.csproj` with logged source files, resolved assembly references/aliases,
  defines, language version, platform and unsafe setting. .NET Framework version
  is inferred from reference assembly paths and the target-framework attribute
  in logged `AssemblyInfo.cs` inputs, or supplied with `--framework v4.8`.
  Semicolon-separated compiler references and the `-t:` target shorthand are supported.
  Input can be an existing `.csproj` or direct C# compilation from a `.mke`
  using `ASSEMBLY_SOURCE_LIST` and `mkcsharp.mki`; an input `.csproj` is not required.
  This initial version supports .NET Framework, not SDK-style .NET projects.
- One common `MyBentley.sln` and a per-Part `.build.cmd`. If a Part contains native and managed
  compiler commands, both project types appear in the solution.

The single `FromLog|x64` configuration represents captured settings. It does
not fabricate Debug/Release configurations. C# retains its logged compiler
platform (including AnyCPU) inside that solution configuration.
Build, Rebuild and Clean invoke the real BentleyBuild Python entry point
through its existing virtual environment, after `env.bat <strategy>`.
Override that environment argument using `--strategy`.
`--repository` and `--strategy` default to the selected project folder's name.
BB receives the selected Part, its full PartFile path, configured roots, and
`--noSubParts`: dependencies are not rebuilt. The solution schedules the Part
only once even when it contains multiple generated projects.
C# design-time targets remain available, but normal builds never use Csc from
the generated project.

## Log requirements and limitations

Use a log containing full `cl[.exe]`, `clang-cl[.exe]` or `csc[.exe]` commands,
not just an incremental/no-op bmake invocation. Commands may reference response
files (including nested files) only if those files still exist; inline `@<<`
response blocks are supported. UTF-8 and BOM-marked UTF-16 logs are supported,
with the current Windows encoding used for other text.

Before writing any output, the utility compares logged compiler inputs with
source prerequisites of object targets in the `.mke`, local `.mki` source lists,
C# `ASSEMBLY_SOURCE_LIST` at each `mkcsharp.mki` inclusion, and explicit
Compile/ClCompile items in referenced MSBuild projects.
Paths are compared by physical location, not just filename. Generated sources
declared as object prerequisites are included in the check. Missing coverage
stops generation with a short request to perform a full rebuild before generating
the Visual Studio project, without listing missing files. Settings are never
copied from another file. Existing projects and the shared solution are left
unchanged on a coverage error.

This is a conservative static check, not a bmake interpreter. Simple source-path
macros and local policy definitions are supported. Undefined/ambiguous macros,
dynamic source expressions, unsupported project item transforms and an unknown
source list stop generation. Conditional declarations are checked conservatively
across branches: a file absent from the log is not assumed inactive.
Shared compiler inference rules are not treated as concrete source declarations.
Standard MSBuild/Visual C++ toolchain imports and the VS template's
`$(UserRootDir)\Microsoft.Cpp.$(Platform).user.props` compiler-settings import
are excluded from source-list traversal. Custom property sheets are still
checked; unresolved custom imports stop generation.

Relative paths are resolved using logged `cd` commands or the active MSBuild
project. Missing sources, conflicting settings for a source and missing
response files are errors, rather than success-shaped fallbacks.
Do not combine multiple configurations or build sessions in one log.
Interleaved parallel MSBuild project output is not supported.
An incremental log containing only some compilations is rejected when any
declared source has no logged compiler command. Use a complete compilation log.
Local native headers are added for browsing. C# generated `.g.cs` sources are
included as logged; XAML/resource generation remains the responsibility of BB.
Aggregate Parts without `BMakeFile` are not yet supported.

## Tests (no Bentley compilation)

```powershell
Set-Location W:\DevArea\Bentley\VS\ConvertBB2VS
python -B -m unittest -v
```
