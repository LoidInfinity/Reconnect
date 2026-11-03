@echo off
rem ============================================================
rem  Network Guard - Administrator Mode Auto-Start Installer
rem  Usage: Right-click this file -> "Run as administrator"
rem  What it does:
rem    1. Stop any running Network Guard worker
rem    2. Create an elevated (HIGHEST) scheduled task "NetworkGuard"
rem       so adapter reset and proxy cleanup are allowed at logon
rem    3. Remove the legacy normal-startup script to avoid duplicates
rem    4. Start the background worker now
rem ============================================================
cd /d "%~dp0"

net session >nul 2>&1
if errorlevel 1 (
    echo [FAIL] Administrator rights are required.
    echo        Right-click this file and choose "Run as administrator".
    pause
    exit /b 1
)

rem Prefer the bundled interpreter, fall back to pythonw on PATH.
set "PYW=%~dp0.conda\pythonw.exe"
if not exist "%PYW%" set "PYW=pythonw.exe"

set "APP=%~dp0gui.py"
set "CFG=%~dp0network_guard_config.json"

echo.
echo [1/4] Stopping any running Network Guard worker...
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter 'Name=''pythonw.exe''' | Where-Object { $_.CommandLine -match 'gui\.py' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter 'Name=''python.exe''' | Where-Object { $_.CommandLine -match 'gui\.py' -and $_.CommandLine -match '--worker' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
echo       Done.

echo.
echo [2/4] Creating elevated scheduled task "NetworkGuard"...
schtasks /Create /TN "NetworkGuard" /TR "\"%PYW%\" \"%APP%\" --worker --config \"%CFG%\"" /SC ONLOGON /RL HIGHEST /F
if %errorlevel%==0 (
    echo       [OK] Task created. Will run at logon with highest privileges.
) else (
    echo       [FAIL] Task creation failed.
)

echo.
echo [3/4] Removing legacy normal-startup script (avoid duplicate run)...
if exist "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\start_network_guard.bat" (
    del "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\start_network_guard.bat"
    echo       [OK] Removed start_network_guard.bat
) else (
    echo       - No legacy startup script found, skipped.
)

echo.
echo [4/4] Starting the worker now (same privilege as this window)...
if exist "%APP%" (
    start "" /min "%PYW%" "%APP%" --worker --config "%CFG%"
    echo       [OK] Started. See network_guard.log for output.
) else (
    echo       [FAIL] gui.py not found next to this script.
)

echo.
echo Done. Network Guard will auto-start at next logon.
pause
