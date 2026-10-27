@echo off
chcp 65001 >nul
title Intel Radar - Run Once
cd /d "%~dp0"

rem ASCII-only on purpose. See the dashboard launcher for the reason.

if not exist ".venv\Scripts\python.exe" (
  echo   [!] Not installed yet. Run the dashboard launcher first.
  pause
  exit /b 1
)

echo.
echo   Collecting now, please wait ^(about 2-4 minutes without a GitHub token^)...
echo.
".venv\Scripts\python.exe" run.py once
echo.
pause
