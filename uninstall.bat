@echo off
REM Uninstall on Windows. UNTESTED: generated, never run on Windows.
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  .venv\Scripts\python.exe -m trader uninstall %*
  if errorlevel 1 ( echo Uninstall step failed; .venv left in place & pause & exit /b 1 )
)
rmdir /s /q .venv 2>nul
rmdir /s /q trader.egg-info 2>nul
echo Removed .venv. The project folder is still here.
pause
