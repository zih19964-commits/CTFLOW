@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0build-run.ps1" %*
set "code=%ERRORLEVEL%"
if not "%code%"=="0" pause
endlocal & exit /b %code%
