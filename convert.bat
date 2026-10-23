@echo off
setlocal DisableDelayedExpansion
if "%~1"=="" goto usage
if "%~2"=="" goto usage
if not "%~3"=="" goto usage
python "%~dp0project_maker.py" "%~1" "%~2"
exit /b %errorlevel%

:usage
echo Usage: convert.bat projectFolder partName 1>&2
exit /b 2
