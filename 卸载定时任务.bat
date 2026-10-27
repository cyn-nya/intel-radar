@echo off
chcp 65001 >nul
setlocal
set "HERE=%~dp0"

echo.
echo   Removing Intel Radar scheduled tasks...
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%HERE%tools\remove_tasks.ps1"
set "RC=%ERRORLEVEL%"

if not "%RC%"=="0" (
  echo.
  echo   [X] Removal did not finish successfully. Read the messages above.
)

echo.
pause
endlocal
