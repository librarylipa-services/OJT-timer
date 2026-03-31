@echo off
setlocal EnableDelayedExpansion

rem Stops the OJT Timer server started on port 5002.
set "OJT_PORT=5002"

echo.
echo  Stopping OJT Timer on port %OJT_PORT%...
echo.

set "FOUND="
for /f "tokens=5" %%P in ('netstat -ano ^| findstr /R /C:":%OJT_PORT% .*LISTENING"') do (
  set "FOUND=1"
  echo  Killing PID %%P (LISTENING on :%OJT_PORT%)...
  taskkill /PID %%P /F >nul 2>&1
)

if not defined FOUND (
  echo  No LISTENING process found on port %OJT_PORT%.
) else (
  echo  Done.
)

echo.
endlocal
