@echo off
rem Windows (cmd) entry point: tb <group> <command> [args]   (list: tb help)
rem Thin wrapper over src\tb\launcher.py: the Bybit key file and proxy removal are done there, same as ./tb on macOS.
rem If args contain cmd special characters (& | ^ %%), use tb.ps1 (PowerShell) instead.
rem Custom interpreter: set TB_PYTHON=C:\path\to\python.exe
setlocal
set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
cd /d "%ROOT%" || exit /b 1
if defined PYTHONPATH (set "PYTHONPATH=%ROOT%\src;%PYTHONPATH%") else (set "PYTHONPATH=%ROOT%\src")
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
if defined TB_PYTHON goto run
set "TB_PYTHON=python"
where py >nul 2>nul && set "TB_PYTHON=py -3"
:run
%TB_PYTHON% -m tb.launcher %*
exit /b %ERRORLEVEL%
