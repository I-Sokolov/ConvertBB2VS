@echo off
setlocal DisableDelayedExpansion
if "%~1"=="" goto usage
if not "%~2"=="" goto usage
python "%~dp0project_maker.py" "%~1"
exit /b %errorlevel%

:usage
echo Usage: conver.bat partName 1>&2
exit /b 2
