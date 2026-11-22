@echo off
rem ============================================================
rem  Network Guard - CLI (terminal) mode launcher
rem  Runs the monitor loop inside this console window.
rem  Press Ctrl+C to stop. Logs also go to data\logs\network_guard.log.
rem
rem  Usage:
rem    start_cli.bat                        (use config in this folder)
rem    start_cli.bat --config other.json    (custom config)
rem    start_cli.bat --verbose              (debug output in console)
rem    start_cli.bat --no-wifi-recovery     (wired-only environments)
rem ============================================================
cd /d "%~dp0"

set "PY=%~dp0.conda\python.exe"
if not exist "%PY%" set "PY=python"

echo Network Guard (CLI mode). Press Ctrl+C to stop.
echo.
"%PY%" "%~dp0src\main.py" %*
set "CODE=%errorlevel%"

echo.
echo [exit] main.py exited with code %CODE%
pause
