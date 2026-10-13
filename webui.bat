@echo off
chcp 65001 >nul 2>&1
title Yandex.Disk Backup
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

echo ============================================================
echo   Yandex.Disk Backup
echo ============================================================
echo.

REM uv manages Python and all dependencies (pyproject.toml + uv.lock).
REM If it is missing, install it once (official installer, ~10 seconds).
where uv >nul 2>&1
if errorlevel 1 (
    echo [setup] uv not found. Installing uv - runs ONLY ONCE...
    echo.
    powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex"
    if errorlevel 1 goto :uvfail
    set "PATH=%USERPROFILE%\.local\bin;%PATH%"
    where uv >nul 2>&1
    if errorlevel 1 goto :uvfail
    echo.
    echo [OK] uv installed.
    echo.
)

echo [*] Starting via uv (first run downloads Python and dependencies, 1-2 min)...
echo.
uv run webui.py
goto :done

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
