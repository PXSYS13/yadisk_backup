@echo off
chcp 65001 >nul 2>&1
title Yandex.Disk Backup
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

REM uv version and SHA-256 of its official installer (github.com/astral-sh/uv/releases).
REM The installer runs only if the downloaded file matches this checksum.
set "UV_VERSION=0.13.4"
set "UV_INSTALLER_SHA256=97b4489acafc6a9b2a6529eba6ef46d8d936ed53c0a7acfd9fd5eb4927932144"

echo ============================================================
echo   Yandex.Disk Backup
echo ============================================================
echo.

REM uv manages Python and all dependencies (pyproject.toml + uv.lock).
REM If it is missing, install it once (official installer, ~10 seconds).
where uv >nul 2>&1
if not errorlevel 1 goto :run

echo [setup] uv not found. Installing uv %UV_VERSION% - runs ONLY ONCE...
echo.
set "UV_INSTALLER=%TEMP%\uv-installer-%UV_VERSION%.ps1"
set "UV_INSTALLER_URL=https://github.com/astral-sh/uv/releases/download/%UV_VERSION%/uv-installer.ps1"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ProgressPreference = 'SilentlyContinue'; [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -UseBasicParsing -Uri $env:UV_INSTALLER_URL -OutFile $env:UV_INSTALLER"
if errorlevel 1 goto :uvfail
powershell -NoProfile -Command "if ((Get-FileHash -Algorithm SHA256 -LiteralPath $env:UV_INSTALLER).Hash -ne $env:UV_INSTALLER_SHA256) { exit 1 }"
if errorlevel 1 goto :uvbad
powershell -NoProfile -ExecutionPolicy Bypass -File "%UV_INSTALLER%"
if errorlevel 1 goto :uvfail
del "%UV_INSTALLER%" >nul 2>&1
set "PATH=%USERPROFILE%\.local\bin;%PATH%"
where uv >nul 2>&1
if errorlevel 1 goto :uvfail
echo.
echo [OK] uv installed.
echo.

:run
echo [*] Starting via uv (first run downloads Python and dependencies, 1-2 min)...
echo.
uv run webui.py
goto :done

:uvbad
del "%UV_INSTALLER%" >nul 2>&1
echo.
echo [!] The downloaded uv installer does not match the expected checksum - not running it.
echo     Run this file again later, or install uv manually:
echo     https://docs.astral.sh/uv/getting-started/installation/
echo.
pause
exit /b 1

:uvfail
echo.
echo [!] Could not install uv automatically.
echo     Install it manually and run this file again:
echo     https://docs.astral.sh/uv/getting-started/installation/
echo.
start "" "https://docs.astral.sh/uv/getting-started/installation/"
pause
exit /b 1

:done
echo.
echo ============================================================
echo   Server stopped.
echo ============================================================
pause
