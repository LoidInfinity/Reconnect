@echo off
cd /d "%~dp0"
if exist "%~dp0.conda\pythonw.exe" (
    start "" "%~dp0.conda\pythonw.exe" "%~dp0gui.py"
) else (
    python "%~dp0gui.py"
    if errorlevel 1 pause
)
