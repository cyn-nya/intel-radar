@echo off
chcp 65001 >nul
title Intel Radar - Dashboard
cd /d "%~dp0"

rem ASCII-only on purpose: cmd parses .bat with the system ANSI codepage.
rem All Chinese output comes from Python (run.py), which prints UTF-8 correctly
rem under chcp 65001.

if not exist ".venv\Scripts\python.exe" (
  echo.
  echo   [1/2] First run: creating venv and installing dependencies.
  echo         Takes 1-2 minutes and happens only once.
  echo.
  python -m venv .venv || goto :fail
  ".venv\Scripts\python.exe" -m pip install --upgrade pip -q
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt -q || goto :fail
  echo   [2/2] Dependencies ready.
)

echo.
".venv\Scripts\python.exe" run.py serve
echo.
echo   Server stopped.
pause
exit /b 0

:fail
echo.
echo   [X] Setup failed. Need Python 3.10+ and a working internet connection.
echo.
pause
exit /b 1
