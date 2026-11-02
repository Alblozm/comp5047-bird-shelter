@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY (
  echo Python 3 is required: https://www.python.org/downloads/
  echo Tick "Add python.exe to PATH" during installation.
  pause
  exit /b 1
)
%PY% shelter_console.py %*
pause
