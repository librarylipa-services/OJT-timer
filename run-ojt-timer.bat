@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"
title Time Track OJT - LibSys

rem --- Bind/port for this app instance (override by editing below)
set "OJT_HOST=127.0.0.2"
set "OJT_PORT=5002"

set "_OJT_PY=python"
python --version >nul 2>&1
if errorlevel 1 (
  py -3 --version >nul 2>&1
  if errorlevel 1 (
    echo [ERROR] Python was not found.
    echo Install Python from https://www.python.org/downloads/
    echo and enable "Add python.exe to PATH", then run this file again.
    echo You can also try: py -3 app.py from this folder.
    pause
    exit /b 1
  )
  set "_OJT_PY=py -3"
)

%_OJT_PY% -c "import flask" >nul 2>&1
if errorlevel 1 (
  echo Installing dependencies from requirements.txt...
  %_OJT_PY% -m pip install -r requirements.txt
  if errorlevel 1 (
    echo [ERROR] Could not install dependencies.
    pause
    exit /b 1
  )
)

rem --- Windows Firewall: allow inbound TCP on OJT port (needs Administrator for first-time rule)
netsh advfirewall firewall show rule name="Time Track OJT - LibSys" >nul 2>&1
if errorlevel 1 (
  netsh advfirewall firewall add rule name="Time Track OJT - LibSys" dir=in action=allow protocol=TCP localport=%OJT_PORT% >nul 2>&1
  if errorlevel 1 (
    echo [Note] Could not add firewall rule. Run this batch as Administrator once, or open port %OJT_PORT% manually for other devices.
  )
)

rem --- After startup: hide this console window (wait ~7s AND wait for port to listen)
rem --- This avoids hiding instantly when Python fails on startup.
set "OJT_LOG=%TEMP%\ojt-timer-%OJT_PORT%.log"
(
echo $ErrorActionPreference = 'SilentlyContinue'
echo Start-Sleep -Seconds 7
echo $deadline = ^(Get-Date^).AddSeconds^(25^)
echo while ^((Get-Date^) -lt $deadline^) {
echo ^  $hit = netstat -ano ^| Select-String -Pattern ':$env:OJT_PORT\\s+.*LISTENING'
echo ^  if ^($hit^) { break }
echo ^  Start-Sleep -Milliseconds 300
echo }
echo $p = Get-Process ^| Where-Object { $_.MainWindowTitle -eq 'Time Track OJT - LibSys' } ^| Select-Object -First 1
echo if ^($p^) {
echo ^  Add-Type @"
echo using System;
echo using System.Runtime.InteropServices;
echo public static class Win32 {
echo   [DllImport^("user32.dll"^)] public static extern bool ShowWindowAsync^(IntPtr hWnd, int nCmdShow^);
echo }
echo "@
echo ^  [Win32]::ShowWindowAsync^($p.MainWindowHandle, 0^) ^| Out-Null
echo }
) > "%TEMP%\_ojt_hide_console.ps1"
start "" powershell -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "%TEMP%\_ojt_hide_console.ps1"

rem --- Open this PC's browser shortly after the server starts
start "" powershell -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Seconds 2; Start-Process 'http://%OJT_HOST%:%OJT_PORT%/'"

echo.
echo  Time Track OJT - LibSys
echo  ------------------------
echo  This PC:  http://%OJT_HOST%:%OJT_PORT%/
echo  Same Wi-Fi/LAN:  http://[this-computer-IP]:%OJT_PORT%/
echo.
for /f "usebackq tokens=*" %%i in (`powershell -NoProfile -Command "try { (Get-NetIPAddress -AddressFamily IPv4 ^| Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' } ^| Select-Object -First 1).IPAddress } catch { '' }"`) do set "OJT_LAN=%%i"
if defined OJT_LAN (
  echo  Detected LAN IP: !OJT_LAN!  —  http://!OJT_LAN!:%OJT_PORT%/
  echo.
)
echo  This window will hide ~7s after startup. Logs: %OJT_LOG%
echo  To stop the server, run stop-ojt-timer.bat.
echo.
%_OJT_PY% app.py >> "%OJT_LOG%" 2>&1
if errorlevel 1 (
  echo.
  echo The server stopped with an error.
  pause
)

endlocal
