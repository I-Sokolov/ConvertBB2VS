# ConvertBB2VS

Python 3.10+ console utility, standard library only. It reads existing Bentley
build logs; it never builds, pulls packages, or modifies source makefiles.
The shared solution defaults to `Bentley2VS\BentleyVS.sln`, one directory above
`ConvertBB2VS`. Projects and build wrappers are stored in `Bentley2VS\projects`.
`--output` changes the root containing the shared solution and `projects`.

## Usage

From the OpenBuildings repository:

```powershell
python ..\Bentley2VS\ConvertBB2VS\project_maker.py tfelement
```

From another directory, supply `--repo-root`:

```powershell
python .\project_maker.py tfelement --repo-root ..\..\OpenBuildings
```

The utility searches recursively for `<Part>_build.log` under
`%OutRoot%\Winx64\LogFiles`, case-insensitively. It asks which file to use if
there are multiple matches. `--non-interactive` reports ambiguity as an error.
Use `--log <path>` or `--part-file <path>` to disambiguate explicitly.
Junctions are followed with physical-directory cycle detection.

Root configuration precedence: explicit `--out-root`/`--src-root`, environment,
literal assignments in the repository's `bb\env.bat`. Only OutRoot has a
fallback: `<repo-root>\bb\out`. Batch environment files are read, not executed
during generation. SrcRoot must be configured; its value is never guessed.

Open `BentleyVS.sln` in Visual Studio 2022 with C++ desktop
development tools and/or the matching .NET Framework targeting pack installed.
Use `--debug-exe <host.exe>` to configure launching a host application; otherwise
attach the debugger to an existing process. No host executable is guessed.

Existing output files are protected; `--force` explicitly permits replacement.
Do not use `--force` on files whose manual edits you want to preserve.
The shared solution is updated automatically: new projects are appended,
existing entries and other solution sections are retained, and regenerating a
project does not duplicate its entry. Each Part is scheduled once for a solution
build, including Parts with multiple generated projects.

Unmodified per-Part solutions created by the previous version are recognized
automatically. Their project files, filters, debugger settings and build wrappers
are moved into `projects` and registered in `BentleyVS.sln`; those old per-Part
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
  is inferred from reference assembly paths, or supplied with `--framework v4.8`.
  Input can be an existing `.csproj` or direct C# compilation from a `.mke`
  using `ASSEMBLY_SOURCE_LIST` and `mkcsharp.mki`; an input `.csproj` is not required.
  This initial version supports .NET Framework, not SDK-style .NET projects.
- One common `BentleyVS.sln` and a per-Part `.build.cmd`. If a Part contains native and managed
  compiler commands, both project types appear in the solution.

The single `FromLog|x64` configuration represents captured settings. It does
not fabricate Debug/Release configurations. C# retains its logged compiler
platform (including AnyCPU) inside that solution configuration.
Build, Rebuild and Clean invoke the real BentleyBuild Python entry point
through its existing virtual environment, after `env.bat OpenBuildings`.
Override that environment argument using `--strategy`.
`--repository` defaults to OpenBuildings.
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
Set-Location .\ConvertBB2VS
python -B -m unittest -v
```
