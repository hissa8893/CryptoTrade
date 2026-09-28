@echo off
REM Windows double-click wrapper for install.ps1. UNTESTED: never run on Windows.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"
pause
