@echo off
rem Suwan v2 - GUI (ASCII only, codepage-safe)
cd /d "%~dp0"
chcp 65001 >nul
set "PYTHONPATH=%~dp0src"
where python >nul 2>nul
if errorlevel 1 (
  echo [error] python not found in PATH. Install Python or add it to PATH.
  pause
  exit /b 1
)
python -X utf8 -m suwan.frontends.tkapp %*
if errorlevel 1 pause
