import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

import project_maker as pm


@unittest.skipUnless(os.name == "nt", "Windows batch launcher requires cmd.exe")
class WindowsLauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.launcher_dir = self.root / "converter with spaces"
        self.launcher_dir.mkdir()
        self.launcher = self.launcher_dir / "convert.bat"
        shutil.copyfile(Path(__file__).parent / "convert.bat", self.launcher)
        (self.launcher_dir / "project_maker.py").write_text(
            "import json, os, sys\n"
            "print(json.dumps({'args': sys.argv[1:], 'cwd': os.getcwd()}))\n"
            "sys.exit(17)\n", encoding="utf-8",
        )
        self.cwd = self.root / "repository with spaces"
        self.cwd.mkdir()

    def run_launcher(self, arguments):
        env = os.environ.copy()
        env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
        comspec = env.get("ComSpec", "cmd.exe")
        return subprocess.run(
            f'"{comspec}" /d /c call "{self.launcher}" {arguments}',
            cwd=self.cwd, env=env, capture_output=True, text=True,
        )

    def test_forwards_part_preserves_repository_cwd_and_exit_code(self):
        result = self.run_launcher('"OBD Projects\\Open Buildings" "tfdvchecker"')
        self.assertEqual(result.returncode, 17, result.stderr)
        self.assertEqual(json.loads(result.stdout),
                         {"args": ["OBD Projects\\Open Buildings", "tfdvchecker"],
                          "cwd": str(self.cwd)})

    def test_missing_arguments_print_usage_without_starting_converter(self):
        result = self.run_launcher("")
        self.assertEqual(result.returncode, 2)
        self.assertIn("Usage: convert.bat projectFolder partName", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_missing_part_prints_usage_without_starting_converter(self):
        result = self.run_launcher('"OBD.26.Escalations\\OpenBuildings"')
        self.assertEqual(result.returncode, 2)
        self.assertIn("Usage: convert.bat projectFolder partName", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_extra_parameter_is_rejected_without_starting_converter(self):
        result = self.run_launcher('"OBD.26.Escalations\\OpenBuildings" tfdvchecker extra')
        self.assertEqual(result.returncode, 2)
        self.assertIn("Usage: convert.bat projectFolder partName", result.stderr)
        self.assertEqual(result.stdout, "")


@unittest.skipUnless(os.name == "nt" and (shutil.which("powershell") or shutil.which("pwsh")),
                     "PowerShell launcher requires Windows PowerShell")
class PowerShellLauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.launcher_dir = self.root / "converter with spaces"
        self.launcher_dir.mkdir()
        self.launcher = self.launcher_dir / "convert.ps1"
        shutil.copyfile(Path(__file__).parent / "convert.ps1", self.launcher)
        (self.launcher_dir / "project_maker.py").write_text(
            "import json, os, sys\n"
            "print(json.dumps({'args': sys.argv[1:], 'cwd': os.getcwd()}))\n"
            "sys.exit(17)\n", encoding="utf-8",
        )
        self.cwd = self.root / "repository with spaces"
        self.cwd.mkdir()
        self.powershell = shutil.which("powershell") or shutil.which("pwsh")

    def run_launcher(self, *arguments):
        env = os.environ.copy()
        env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
        return subprocess.run(
            [self.powershell, "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", str(self.launcher), *arguments],
            cwd=self.cwd, env=env, capture_output=True, text=True,
        )

    def test_forwards_arguments_preserves_cwd_and_exit_code(self):
        result = self.run_launcher("OBD Projects\\Open Buildings", "tfdvchecker")
        self.assertEqual(result.returncode, 17, result.stderr)
        self.assertEqual(json.loads(result.stdout),
                         {"args": ["OBD Projects\\Open Buildings", "tfdvchecker"],
                          "cwd": str(self.cwd)})

    def test_missing_arguments_print_usage_without_starting_converter(self):
        result = self.run_launcher()
        self.assertEqual(result.returncode, 2)
        self.assertIn("Usage: .\\convert.ps1 projectFolder partName", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_extra_argument_is_rejected_without_starting_converter(self):
        result = self.run_launcher("OBD.26.Escalations\\OpenBuildings", "tfdvchecker", "extra")
        self.assertEqual(result.returncode, 2)
        self.assertIn("Usage: .\\convert.ps1 projectFolder partName", result.stderr)
        self.assertEqual(result.stdout, "")


class ProjectMakerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def write(self, relative, text=""):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def test_windows_args_embedded_quotes_and_backslashes(self):
        self.assertEqual(
            pm.windows_args(r'/I"C:\Program Files\SDK\\" /DNAME=\"text\" /D "VALUE=a b" a.cpp'),
            ["/IC:\\Program Files\\SDK\\", '/DNAME="text"', "/D", "VALUE=a b", "a.cpp"],
        )
        with self.assertRaises(pm.GenerationError):
            pm.windows_args('/I"unfinished')

    def test_native_flags_and_per_file_settings(self):
        unit = pm.parse_compilation("cl", pm.windows_args(
            r'/I"include dir" -D FOO=1 /UOLD /FIforced.h /std:c++20 /Zc:__cplusplus a.cpp b.cpp'
        ), self.root)
        other = pm.parse_compilation("cl", ["/DBAR", "c.cpp"], self.root)
        files = pm.cpp_project("test", [unit, other], self.root, 'call "$(ProjectDir)test.build.cmd"', None)
        root = ET.fromstring(files["test.vcxproj"])
        ns = {"m": pm.NS}
        self.assertEqual(root.find(".//m:NMakeReBuildCommandLine", ns).text,
                         'call "$(ProjectDir)test.build.cmd" rebuild')
        items = root.findall(".//m:ClCompile", ns)
        self.assertEqual(len(items), 3)
        self.assertEqual(items[0].find("m:PreprocessorDefinitions", ns).text, "FOO=1")
        self.assertEqual(items[2].find("m:PreprocessorDefinitions", ns).text, "BAR")
        self.assertEqual(items[0].find("m:LanguageStandard", ns).text, "stdcpp20")

    def test_response_files_and_cycles(self):
        self.write("inner.rsp", '/DREAL /I"include dir" a.cpp')
        self.write("outer.rsp", "@inner.rsp")
        expanded = pm.expand_response(["@outer.rsp"], self.root)
        self.assertEqual(expanded, ["/DREAL", "/Iinclude dir", "a.cpp"])
        self.write("inner.rsp", "@outer.rsp")
        with self.assertRaisesRegex(pm.GenerationError, "Recursive"):
            pm.expand_response(["@outer.rsp"], self.root)
        with self.assertRaisesRegex(pm.GenerationError, "unavailable"):
            pm.expand_response(["@missing.rsp"], self.root)

    def test_log_discovery_exact_case_insensitive_and_ambiguity(self):
        first = self.write("logs/one/TFELEMENT_build.log")
        self.write("logs/one/tfelement_provenance.log")
        self.write("logs/one/not-tfelement_build.log")
        self.assertEqual(pm.find_log(self.root / "logs", "tfelement", False), first)
        second = self.write("logs/two/tfelement_build.log")
        with self.assertRaisesRegex(pm.GenerationError, "Ambiguous"):
            pm.find_log(self.root / "logs", "tfelement", False)
        with patch("builtins.input", side_effect=["invalid", "2"]), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pm.find_log(self.root / "logs", "tfelement", True), second)

    def test_empty_incremental_log_is_not_success(self):
        path = self.write("tfelement_build.log", "Bentley Systems Make Utility\nNothing to build")
        with self.assertRaisesRegex(pm.GenerationError, "full rebuild"):
            pm.parse_log(path, self.root)

    def test_part_lookup_does_not_parse_unrelated_broken_xml(self):
        self.write("unrelated.PartFile.xml", '<BuildContext><Part Name="unrelated"></BuildContext>')
        expected = self.write("test.PartFile.xml",
            '<BuildContext><Part Name="test" BMakeFile="test.mke"/></BuildContext>')
        self.write("test.mke")
        path, _, _ = pm.part_definition(self.root, "test", None, False)
        self.assertEqual(path, expected)

    def test_inline_response_and_working_directory(self):
        cwd = self.root / "source dir"
        path = self.write("native.log", f'cd "{cwd}" & bmake.exe test.mke\ncl.exe @<<\n/DREAL a.cpp\n<<\n')
        unit = pm.parse_log(path, self.root)[0]
        self.assertEqual(unit.sources, [cwd / "a.cpp"])
        self.assertEqual(unit.defines, ["REAL"])

    def test_csharp_framework_references_and_bb_build_targets(self):
        project = self.root / "managed" / "app.csproj"
        log = self.write("managed.log",
            f'Project "{project}" on node 1 (Build target(s)).\n'
            'C:\\Program Files\\VS\\Roslyn\\csc.exe /define:DEBUG;TRACE /platform:x64 '
            '/reference:"C:\\Reference Assemblies\\.NETFramework\\v4.8\\System.dll" '
            '/reference:alias=relative.dll /langversion:7.3 /out:app.dll /target:library '
            '/unsafe+ Source.cs\n')
        unit = pm.parse_log(log, self.root)[0]
        self.assertEqual(unit.sources, [project.parent / "Source.cs"])
        self.assertEqual(pm.framework_from_log(unit), "v4.8")
        root = ET.fromstring(pm.cs_project("app", unit, project.parent, "call app.build.cmd", None, None)["app.csproj"])
        ns = {"m": pm.NS}
        self.assertEqual(root.find(".//m:DefineConstants", ns).text, "DEBUG;TRACE")
        self.assertEqual(root.find(".//m:AllowUnsafeBlocks", ns).text, "true")
        self.assertEqual(root.find(".//m:Aliases", ns).text, "alias")
        children = list(root)
        self.assertTrue(children[-4].tag.endswith("Import"))
        self.assertEqual([element.get("Name") for element in children[-3:]], ["Build", "Rebuild", "Clean"])
        self.assertEqual(children[-3][0].get("Command"), "call app.build.cmd build")

    def test_conflicting_native_settings_fail(self):
        first = pm.parse_compilation("cl", ["/DFIRST", "same.cpp"], self.root)
        second = pm.parse_compilation("cl", ["/DSECOND", "same.cpp"], self.root)
        with self.assertRaisesRegex(pm.GenerationError, "Conflicting"):
            pm.cpp_project("test", [first, second], self.root, "bb", None)

    def test_direct_csharp_reference_list_target_and_generated_framework(self):
        source = self.write("AssemblyInfo.cs",
            '[assembly:System.Runtime.Versioning.TargetFrameworkAttribute('
            '".NETFramework,Version=v4.8", FrameworkDisplayName = ".NET Framework 4.8")]')
        unit = pm.parse_compilation("csc", [
            "-r:first.dll;alias=second.dll;third.dll", "-t:exe", str(source)
        ], self.root)
        self.assertEqual(unit.references, [
            ("", str(self.root / "first.dll")),
            ("alias", str(self.root / "second.dll")),
            ("", str(self.root / "third.dll")),
        ])
        self.assertEqual(pm.framework_from_log(unit), "v4.8")
        root = ET.fromstring(pm.cs_project("direct", unit, self.root, "bb", None, None)["direct.csproj"])
        self.assertEqual(root.find(f".//{{{pm.NS}}}OutputType").text, "Exe")
        unit.references.append(("", r"C:\Ref\.NETFramework\v4.7.2\System.dll"))
        with self.assertRaisesRegex(pm.GenerationError, "single .NET Framework"):
            pm.framework_from_log(unit)

    def test_c_sources_compiled_as_cpp_are_preserved(self):
        unit = pm.parse_compilation("cl", ["/TP", "/std:c++20", "a.c", "/Tcother.cpp"], self.root)
        root = ET.fromstring(pm.cpp_project("test", [unit], self.root, "bb", None)["test.vcxproj"])
        ns = {"m": pm.NS}
        items = root.findall(".//m:ClCompile", ns)
        self.assertEqual(items[0].find("m:CompileAs", ns).text, "CompileAsCpp")
        self.assertEqual(items[1].find("m:CompileAs", ns).text, "CompileAsC")

    def test_solution_schedules_part_only_once(self):
        text = pm.solution([("native", "native.vcxproj", pm.CPP_GUID),
                            ("managed", "managed.csproj", pm.CS_GUID)])
        self.assertEqual(text.count(".Build.0"), 1)
        self.assertEqual(text.count(".ActiveCfg"), 2)

    def test_end_to_end_generation_no_build_and_no_implicit_overwrite(self):
        bentley_root = self.root
        repo = bentley_root / "repo"
        src = repo / "bb" / "src"
        self.write("repo/env.bat", "@echo off\n")
        self.write("repo/bb/src/BentleyBuild/.venv/Scripts/python.exe")
        self.write("repo/bb/src/BentleyBuild/BentleyBuild.py")
        part_file = self.write("repo/Triforma/Triforma.PartFile.xml",
            '<BuildContext><Part Name="tfelement" BMakeFile="src/tfelement/tfelement.mke"/></BuildContext>')
        makefile = self.write("repo/Triforma/src/tfelement/tfelement.mke",
            "baseDir=$(_MakeFilePath)\n$(o)a$(oext): $(baseDir)a.cpp\n")
        source = self.write("repo/Triforma/src/tfelement/a.cpp")
        self.write("repo/Triforma/src/tfelement/a.h")
        log = self.write("repo/bb/out/Winx64/LogFiles/Triforma/tfelement_build.log",
            f"Building Part: {part_file} <tfelement>\n"
            f'cd "{makefile.parent}" & bmake.exe tfelement.mke\n'
            f'cl.exe /DREAL /I"{makefile.parent}" "{source}"\n')
        output = self.root / "generated"
        self.write("generated/legacy.sln",
                   pm.solution([("legacy", "legacy.csproj", pm.CS_GUID)]))
        self.write("generated/legacy.csproj", "<Project/>")
        self.write("generated/legacy.build.cmd", "@echo off\n")
        args = pm.parser().parse_args([str(repo), "tfelement",
            "--src-root", str(src), "--non-interactive"])
        with patch("project_maker.workspace_roots", return_value=(bentley_root, output)), \
                contextlib.redirect_stdout(io.StringIO()):
            paths = pm.generate(args)
        self.assertEqual(len(paths), 4)
        self.assertTrue((output / "MyBentley.sln").is_file())
        args.output = output
        self.assertFalse((output / "tfelement.sln").exists())
        self.assertFalse((output / "legacy.sln").exists())
        self.assertFalse((output / "legacy.csproj").exists())
        self.assertTrue((output / "projects" / "legacy.csproj").is_file())
        solution_text = pm.read_text(output / "MyBentley.sln")
        self.assertIn(r"projects\legacy.csproj", solution_text)
        self.assertIn(r"projects\tfelement.vcxproj", solution_text)
        folder_blocks = [block for block in pm.SOLUTION_PROJECT.finditer(solution_text.replace("\r\n", "\n"))
                         if block["kind"] == pm.SOLUTION_FOLDER_GUID]
        self.assertEqual([block["name"] for block in folder_blocks], [repo.parent.name, repo.name])
        self.assertIn(f'{folder_blocks[1]["guid"]} = {folder_blocks[0]["guid"]}', solution_text)
        self.assertIn(f'{pm.guid("tfelement")} = {folder_blocks[1]["guid"]}', solution_text)
        self.assertNotIn(f'{pm.guid("legacy")} = ', solution_text)
        for path in paths:
            if path.suffix in (".vcxproj", ".filters"):
                ET.parse(path)
        wrapper = pm.read_text(output / "projects" / "tfelement.build.cmd")
        self.assertIn('"--partName" "tfelement"', wrapper)
        self.assertIn('call "' + str(repo / "env.bat") + '" "repo"', wrapper)
        self.assertIn('"--noSubParts"', wrapper)
        self.assertIn('"--outputroot"', wrapper)
        self.assertIn('if errorlevel 1 exit /b %errorlevel%', wrapper)
        self.assertNotIn("DOSKEY", wrapper)
        self.assertEqual(pm.find_log(repo / "bb/out/Winx64/LogFiles", "tfelement", False), log)
        with self.assertRaisesRegex(pm.GenerationError, "already exist"):
            pm.generate(args)
        self.write("repo/Managed/Managed.PartFile.xml",
            '<BuildContext><Part Name="managed" BMakeFile="app.mke"/></BuildContext>')
        self.write("repo/Managed/app.mke",
            "baseDir=$(_MakeFilePath)\nASSEMBLY_SOURCE_LIST=$(baseDir)app.cs\n%include mkcsharp.mki\n")
        managed_source = self.write("repo/Managed/app.cs")
        self.write("repo/bb/out/Winx64/LogFiles/Managed/managed_build.log",
            f'csc.exe /reference:"C:\\Ref\\.NETFramework\\v4.8\\System.dll" "{managed_source}"\n')
        managed_args = pm.parser().parse_args([str(repo), "managed",
            "--src-root", str(src), "--output", str(output), "--non-interactive"])
        with contextlib.redirect_stdout(io.StringIO()):
            pm.generate(managed_args)
        shared = pm.read_text(output / "MyBentley.sln")
        self.assertIn(r"projects\legacy.csproj", shared)
        self.assertIn(r"projects\tfelement.vcxproj", shared)
        self.assertIn(r"projects\managed.csproj", shared)
        managed_args.force = True
        with contextlib.redirect_stdout(io.StringIO()):
            pm.generate(managed_args)
        self.assertEqual(pm.read_text(output / "MyBentley.sln"), shared)
        args.force = True
        makefile.write_text(makefile.read_text() + "$(o)missing$(oext): $(baseDir)missing.cpp\n")
        before = {path: path.read_bytes() for path in paths}
        with self.assertRaisesRegex(pm.GenerationError, "full rebuild"):
            pm.generate(args)
        self.assertEqual(before, {path: path.read_bytes() for path in paths})

    def test_msbuild_literals_are_escaped(self):
        self.assertEqual(pm.mslist(["A;B", "X%Y", "$(literal)"]), "A%3BB;X%25Y;%24(literal)")

    def manifest(self, makefile):
        return pm.SourceManifest(makefile, self.root / "test.PartFile.xml",
                                 self.root / "bb" / "src", self.root / "bb" / "out")

    def test_partial_native_log_requests_rebuild_without_file_list(self):
        makefile = self.write("src/test.mke",
            "baseDir=$(_MakeFilePath)\n"
            "$(o)first$(oext): $(baseDir)first.cpp\n"
            "#$(o)commented$(oext): $(baseDir)commented.cpp\n"
            "$(o)second$(oext): $(baseDir)second.cpp\n")
        first = self.write("src/first.cpp")
        self.write("src/second.cpp")
        unit = pm.Compilation("cpp", self.root, sources=[first])
        with self.assertRaisesRegex(pm.GenerationError, "full rebuild") as error:
            self.manifest(makefile).verify([unit])
        self.assertNotIn("second.cpp", str(error.exception))
        self.assertNotIn("commented.cpp", str(error.exception))

    def test_same_filename_in_different_directories_is_not_coverage(self):
        makefile = self.write("src/test.mke",
            "baseDir=$(_MakeFilePath)\n$(o)a$(oext): $(baseDir)one/same.cpp\n"
            "$(o)b$(oext): $(baseDir)two/same.cpp\n")
        first = self.write("src/one/same.cpp")
        second = self.write("src/two/same.cpp")
        unit = pm.Compilation("cpp", self.root, sources=[first])
        with self.assertRaisesRegex(pm.GenerationError, "incomplete"):
            self.manifest(makefile).verify([unit])
        unit.sources.append(second)
        self.assertEqual(self.manifest(makefile).verify([unit]), 2)

    def test_source_list_macro_and_local_include_are_checked(self):
        makefile = self.write("src/test.mke",
            "baseDir=$(_MakeFilePath)\n%include sources.mki\n")
        self.write("src/sources.mki",
            'Sources="$(baseDir)one.cpp" "$(baseDir)two.cpp"\n$(o)all$(oext): $(Sources)\n')
        one = self.write("src/one.cpp")
        two = self.write("src/two.cpp")
        unit = pm.Compilation("cpp", self.root, sources=[one, two])
        self.assertEqual(self.manifest(makefile).verify([unit]), 2)
        unit.sources.pop()
        with self.assertRaisesRegex(pm.GenerationError, "full rebuild"):
            self.manifest(makefile).verify([unit])

    def test_unknown_source_macro_is_not_guessed(self):
        makefile = self.write("src/test.mke", "$(o)a$(oext): $(unknown)first.cpp\n")
        unit = pm.Compilation("cpp", self.root, sources=[self.root / "first.cpp"])
        with self.assertRaisesRegex(pm.GenerationError, "undefined macro unknown"):
            self.manifest(makefile).verify([unit])

    def test_conditional_source_macro_cannot_hide_missing_files(self):
        makefile = self.write("src/test.mke",
            'Sources=first.cpp\n%if defined (SECOND)\nSources=second.cpp\n%endif\n'
            '$(o)all$(oext): $(Sources)\n')
        unit = pm.Compilation("cpp", self.root, sources=[self.root / "second.cpp"])
        with self.assertRaisesRegex(pm.GenerationError, "Cannot verify"):
            self.manifest(makefile).verify([unit])

    def test_repeated_include_checks_each_source_list(self):
        makefile = self.write("src/test.mke",
            'Source=first.cpp\n%include rules.mki\nSource=second.cpp\n%include rules.mki\n')
        self.write("src/rules.mki", '$(o)all${oext}: $(Source)\n')
        unit = pm.Compilation("cpp", self.root, sources=[self.root / "first.cpp"])
        with self.assertRaisesRegex(pm.GenerationError, "full rebuild"):
            self.manifest(makefile).verify([unit])

    def test_managed_partial_log_checks_original_project(self):
        project = self.write("managed/app.csproj",
            '<Project><ItemGroup><Compile Include="first.cs"/><Compile Include="second.cs"/></ItemGroup></Project>')
        makefile = self.write("managed/app.mke",
            "MSBUILD_PROJECT_PATH=$(_MakeFilePath)app.csproj\n")
        first = self.write("managed/first.cs")
        second = self.write("managed/second.cs")
        unit = pm.Compilation("cs", project.parent, sources=[first], project=project)
        with self.assertRaisesRegex(pm.GenerationError, "full rebuild"):
            self.manifest(makefile).verify([unit])
        unit.sources.append(second)
        self.assertEqual(self.manifest(makefile).verify([unit]), 2)

    def test_csharp_without_csproj_checks_assembly_source_list(self):
        makefile = self.write("managed/app.mke",
            "baseDir=$(_MakeFilePath)\n"
            "Files=$(baseDir)first.cs\n"
            "ASSEMBLY_SOURCE_LIST = $(Files) \\\n"
            '    "$(baseDir)second source.cs"\n'
            "%include mkcsharp.mki\n")
        first = self.write("managed/first.cs")
        second = self.write("managed/second source.cs")
        unit = pm.Compilation("cs", self.root, sources=[first])
        self.assertIsNone(unit.project)
        with self.assertRaisesRegex(pm.GenerationError, "full rebuild") as error:
            self.manifest(makefile).verify([unit])
        self.assertNotIn(".cs", str(error.exception))
        unit.sources.append(second)
        self.assertEqual(self.manifest(makefile).verify([unit]), 2)

    def test_shared_solution_adds_each_part_without_duplicates(self):
        first = [("native", r"projects\native.vcxproj", pm.CPP_GUID)]
        second = [("managed", r"projects\managed.csproj", pm.CS_GUID)]
        text = pm.merge_solution(None, first)
        text = text.replace("EndGlobal\n", "\tGlobalSection(SolutionProperties) = preSolution\n"
                            "\t\tHideSolutionNode = FALSE\n\tEndGlobalSection\nEndGlobal\n")
        merged = pm.merge_solution(text, second)
        self.assertIn(r"projects\native.vcxproj", merged)
        self.assertIn(r"projects\managed.csproj", merged)
        self.assertIn("HideSolutionNode = FALSE", merged)
        self.assertEqual(merged.count(".Build.0"), 2)
        self.assertEqual(pm.merge_solution(merged, second), merged)

    def test_csharp_source_list_from_local_include(self):
        makefile = self.write("managed/app.mke",
            "baseDir=$(_MakeFilePath)\n%include sources.mki\n%include mkcsharp.mki\n")
        self.write("managed/sources.mki", "ASSEMBLY_SOURCE_LIST=$(baseDir)first.cs\n")
        source = self.write("managed/first.cs")
        unit = pm.Compilation("cs", self.root, sources=[source])
        self.assertEqual(self.manifest(makefile).verify([unit]), 1)

    def test_solution_folders_reused_for_native_and_managed_projects(self):
        folders = ("OBD.26.Escalations", "OpenBuildings")
        first = [("part", r"projects\part.vcxproj", pm.CPP_GUID),
                 ("part.Managed1", r"projects\part.Managed1.csproj", pm.CS_GUID)]
        text = pm.merge_solution(None, first, folders)
        second = [("other", r"projects\other.csproj", pm.CS_GUID)]
        text = pm.merge_solution(text, second, tuple(name.lower() for name in folders))
        blocks = list(pm.SOLUTION_PROJECT.finditer(text))
        folder_blocks = [block for block in blocks if block["kind"] == pm.SOLUTION_FOLDER_GUID]
        self.assertEqual([block["name"] for block in folder_blocks], list(folders))
        parent, child = [block["guid"] for block in folder_blocks]
        self.assertIn(f"{child} = {parent}", text)
        for name, _, _ in first + second:
            self.assertIn(f"{pm.guid(name)} = {child}", text)
        self.assertEqual(text.count("GlobalSection(NestedProjects)"), 1)
        self.assertEqual(text.count(".Build.0"), 2)
        self.assertEqual(text.count(".ActiveCfg"), 3)
        self.assertEqual(pm.merge_solution(text, first, folders), text)

    def test_solution_folders_distinguish_same_name_under_different_parents(self):
        first = [("first", r"projects\first.csproj", pm.CS_GUID)]
        second = [("second", r"projects\second.csproj", pm.CS_GUID)]
        text = pm.merge_solution(None, first, ("OBD.26.Escalations", "OpenBuildings"))
        text = pm.merge_solution(text, second, ("OBD.27", "OpenBuildings"))
        folders = [block for block in pm.SOLUTION_PROJECT.finditer(text)
                   if block["kind"] == pm.SOLUTION_FOLDER_GUID]
        self.assertEqual(len(folders), 4)
        self.assertEqual(len({block["guid"] for block in folders}), 4)
        for project, (parent, child) in zip((first, second), (folders[:2], folders[2:])):
            self.assertIn(f'{child["guid"]} = {parent["guid"]}', text)
            self.assertIn(f'{pm.guid(project[0][0])} = {child["guid"]}', text)

    def test_solution_folders_reuse_manual_folders_and_preserve_other_nesting(self):
        parent, child = pm.guid("manual parent"), pm.guid("manual child")
        existing = pm.solution([("old", r"projects\old.csproj", pm.CS_GUID)])
        existing = existing.replace("\nGlobal\n", (
            f'\nProject("{pm.SOLUTION_FOLDER_GUID}") = "Release", "Release", "{parent}"\n'
            'EndProject\n'
            f'Project("{pm.SOLUTION_FOLDER_GUID}") = "Repo", "Repo", "{child}"\n'
            '\tProjectSection(SolutionItems) = preProject\n'
            '\t\treadme.txt = readme.txt\n\tEndProjectSection\nEndProject\nGlobal\n'))
        existing = existing.replace("EndGlobal\n", (
            '\tGlobalSection(NestedProjects) = preSolution\n'
            f'\t\t{child} = {parent}\n\t\t{pm.guid("old")} = {parent}\n'
            '\tEndGlobalSection\nEndGlobal\n'))
        projects = [("Repo", r"projects\Repo.csproj", pm.CS_GUID)]
        text = pm.merge_solution(existing, projects, ("Release", "Repo"))
        self.assertEqual(text.count(f'Project("{pm.SOLUTION_FOLDER_GUID}")'), 2)
        self.assertIn("readme.txt = readme.txt", text)
        self.assertIn(f'{pm.guid("old")} = {parent}', text)
        self.assertIn(f'{pm.guid("Repo")} = {child}', text)
        self.assertEqual(pm.merge_solution(text, projects, ("Release", "Repo")), text)

    def test_solution_folders_do_not_move_existing_projects(self):
        projects = [("old", r"projects\old.csproj", pm.CS_GUID)]
        text = pm.merge_solution(None, projects)
        self.assertEqual(pm.merge_solution(text, projects, ("Release", "Repo")), text)

    def test_solution_folders_reject_ambiguous_existing_folders(self):
        text = pm.solution([])
        for value in ("first folder", "second folder"):
            text = text.replace("\nGlobal\n", (
                f'\nProject("{pm.SOLUTION_FOLDER_GUID}") = "Repo", "Repo", "{pm.guid(value)}"\n'
                'EndProject\nGlobal\n'))
        with self.assertRaisesRegex(pm.GenerationError, "Ambiguous solution folder"):
            pm.merge_solution(text, [("part", r"projects\part.csproj", pm.CS_GUID)], ("Repo",))

    def test_shared_solution_builds_each_mixed_part_only_once(self):
        text = pm.merge_solution(None, [
            ("part", r"projects\part.vcxproj", pm.CPP_GUID),
            ("part.Managed1", r"projects\part.Managed1.csproj", pm.CS_GUID)])
        text = pm.merge_solution(text, [("other", r"projects\other.csproj", pm.CS_GUID)])
        self.assertEqual(text.count(".Build.0"), 2)
        self.assertEqual(text.count(".ActiveCfg"), 3)

    def test_shared_solution_rejects_conflicting_identity(self):
        text = pm.merge_solution(None, [("native", r"projects\native.vcxproj", pm.CPP_GUID)])
        with self.assertRaisesRegex(pm.GenerationError, "Conflicting"):
            pm.merge_solution(text, [("native", r"elsewhere\native.vcxproj", pm.CPP_GUID)])

    def test_workspace_roots_match_requested_directory_layout(self):
        bentley_root, solution_root = pm.workspace_roots()
        self.assertEqual(bentley_root, Path(__file__).resolve().parent.parent.parent)
        self.assertEqual(solution_root, Path(__file__).resolve().parent.parent)
        self.assertIsNone(pm.parser().parse_args(["OBD.26.Escalations\\OpenBuildings", "tfelement"]).output)

    def test_project_folder_is_absolute_or_relative_to_cwd(self):
        repo = self.root / "project with spaces"
        repo.mkdir()
        cwd = self.root / "working folder"
        cwd.mkdir()
        cases = ((cwd, str(repo)), (cwd, str(Path("..") / repo.name)), (repo, "."))
        for working_directory, folder in cases:
            with self.subTest(folder=folder):
                args = pm.parser().parse_args([folder, "tfelement"])
                with patch("os.getcwd", return_value=str(working_directory)), \
                        patch("project_maker.static_environment",
                              side_effect=pm.GenerationError("resolved project")) as environment:
                    with self.assertRaisesRegex(pm.GenerationError, "resolved project"):
                        pm.generate(args)
                environment.assert_called_once_with(repo.resolve())

    def test_missing_project_folder_reports_resolved_path(self):
        missing = self.root / "missing"
        args = pm.parser().parse_args([str(missing), "tfelement"])
        with self.assertRaises(pm.GenerationError) as error:
            pm.generate(args)
        self.assertIn(f"Repository directory does not exist: {missing}", str(error.exception))

    def test_legacy_solution_recognizes_generated_files_only(self):
        records = [("old", "old.csproj", pm.CS_GUID)]
        solution = self.write("old.sln", pm.solution(records))
        self.write("old.csproj", "<Project/>")
        self.write("old.build.cmd", "@echo off")
        self.write("user.sln", "Do not touch")
        migrations = pm.legacy_projects(self.root)
        self.assertEqual(len(migrations), 1)
        self.assertEqual(migrations[0][0], solution)
        self.assertEqual(migrations[0][1], records)
        self.assertEqual({p.name for p in migrations[0][2]}, {"old.csproj", "old.build.cmd"})


if __name__ == "__main__":
    unittest.main()
