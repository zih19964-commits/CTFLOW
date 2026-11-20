@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

where pwsh.exe >nul 2>nul
if "%ERRORLEVEL%"=="0" (
    pwsh.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0build-run.ps1" %*
) else (
    where powershell.exe >nul 2>nul
    if not "%ERRORLEVEL%"=="0" (
        echo [环境缺失] 未检测到 PowerShell 运行环境，请前往官方地址下载安装：
        echo   https://aka.ms/powershell
        pause
        exit /b 1
    )
    powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0build-run.ps1" %*
)

set "code=%ERRORLEVEL%"
if not "%code%"=="0" (
    echo.
    pause
)
endlocal & exit /b %code%
