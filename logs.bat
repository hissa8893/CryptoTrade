@echo off
REM logs the paper trader on Windows. UNTESTED: generated, never run on Windows.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Not installed yet - run install.bat first
  pause
  exit /b 1
)
.venv\Scripts\python.exe -m trader logs %*
if "%~1"=="" pause
