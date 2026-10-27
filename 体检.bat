@echo off
chcp 65001 >nul
title Intel Radar - Diagnose
cd /d "%~dp0"

rem ASCII-only on purpose. See the dashboard launcher for the reason.

if not exist ".venv\Scripts\python.exe" (
  echo   [!] Not installed yet. Run the dashboard launcher first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" run.py diagnose
echo.
pause
