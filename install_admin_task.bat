@echo off
rem ============================================================
rem  Network Guard - Administrator Mode Auto-Start Installer
rem  Usage: Right-click this file -> "Run as administrator"
rem  What it does:
rem    1. Stop any existing Network Guard instance
rem    2. Create an elevated (HIGHEST) scheduled task "NetworkGuard"
rem       so the hardware adapter reset works at logon
rem    3. Remove the normal-startup script to avoid duplicates
rem    4. Start Network Guard now
rem ============================================================
cd /d "%~dp0"

echo.
echo [1/4] Stopping any existing Network Guard instance...
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter 'Name=''python.exe''' | Where-Object { $_.CommandLine -match 'network_main' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
echo       Done.

echo.
echo [2/4] Creating elevated scheduled task "NetworkGuard"...
schtasks /Create /TN "NetworkGuard" /TR "python.exe %~dp0network_main.py" /SC ONLOGON /RL HIGHEST /F
if %errorlevel%==0 (
    echo       [OK] Task created. Will run at logon with highest privileges.
) else (
    echo       [FAIL] Task creation failed. Did you run this as Administrator?
)

echo.
echo [3/4] Removing normal-startup script (avoid duplicate run)...
if exist "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\start_network_guard.bat" (
    del "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\start_network_guard.bat"
    echo       [OK] Removed start_network_guard.bat
) else (
    echo       - No normal-startup script found, skipped.
)

echo.
echo [4/4] Starting Network Guard now (same privilege as this window)...
if exist "%~dp0network_main.py" (
    start "" /min "python.exe" "%~dp0network_main.py"
    echo       [OK] Started. See network_guard.log
) else (
    echo       [FAIL] network_main.py not found.
)

echo.
echo Done. Network Guard will auto-start at next logon.
pause
