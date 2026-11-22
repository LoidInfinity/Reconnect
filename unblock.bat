@echo off
rem ============================================================
rem  Network Guard - Remove the Internet download mark
rem
rem  Windows marks files that came from a browser download and
rem  shows "unknown publisher / no valid digital signature" when
rem  you double-click a .bat. That prompt cannot be fixed with a
rem  code signature because .bat files cannot be signed.
rem
rem  Run this ONCE after extracting the ZIP. It clears the mark
rem  from every file in this folder, and the prompt is gone for
rem  good. No administrator rights required.
rem ============================================================
setlocal
cd /d "%~dp0"

echo Clearing the Internet mark under:
echo   %CD%
echo This may take a few seconds...
echo.

powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-ChildItem -LiteralPath '%CD%' -Recurse -File -Force | Unblock-File"
if errorlevel 1 (
    echo [FAIL] Some files could not be cleared.
    echo        Alternative: right-click the downloaded ZIP, open Properties,
    echo        tick "Unblock", then extract again.
) else (
    echo [OK] Done. Double-clicking start_gui.bat no longer shows the warning.
)

echo.
pause
endlocal
