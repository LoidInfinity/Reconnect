@echo off
rem ============================================================
rem  Enable WLAN Adapter - Run as Administrator
rem  Fixes: Wi-Fi card stuck Disabled (CM_PROB_DISABLED),
rem  shows "Not Present" / cannot connect after reboot.
rem  Usage: Right-click this file -> "Run as administrator"
rem ============================================================
cd /d "%~dp0"

echo.
echo [1/3] Checking current WLAN adapter state...
powershell -NoProfile -Command "Get-NetAdapter -Name 'WLAN' | Select-Object Name,Status | Format-List"

echo.
echo [2/3] Enabling WLAN adapter...
powershell -NoProfile -Command "Enable-NetAdapter -Name 'WLAN' -Confirm:$false"
if %errorlevel%==0 (
    echo       [OK] Enable command sent.
) else (
    echo       [FAIL] Enable failed. Are you running as Administrator?
)

echo.
echo [3/3] Verifying...
powershell -NoProfile -Command "Start-Sleep -Seconds 5; Get-NetAdapter -Name 'WLAN' | Select-Object Name,Status | Format-List; Write-Output ('SSID: ' + (netsh wlan show interfaces | Select-String 'SSID'))"

echo.
echo Done. If Status shows Up, Wi-Fi should work now.
pause
