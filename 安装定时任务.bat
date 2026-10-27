@echo off
chcp 65001 >nul
setlocal
set "HERE=%~dp0"

echo.
echo   Installing Windows scheduled tasks for Intel Radar...
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%HERE%tools\setup_tasks.ps1"
set "RC=%ERRORLEVEL%"

if not "%RC%"=="0" (
  echo.
  echo   [X] Setup did not finish successfully. Read the messages above.
)

echo.
pause
endlocal
