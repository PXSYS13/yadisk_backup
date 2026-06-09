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

REM ==== 1) Check Python ====
where python >nul 2>&1
if errorlevel 1 (
    echo [!] Python is not installed.
    echo.
    echo Opening Python download page...
    echo During install, CHECK the "Add Python to PATH" box!
    echo.
    start "" "https://www.python.org/downloads/"
    echo.
    echo After installing Python, run this file again.
    pause
    exit /b 1
)

REM ==== 2) Check Python version (need 3.10+) ====
python -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" >nul 2>&1
if errorlevel 1 (
    echo [!] Python is too old. Need 3.10 or newer.
    python --version
    pause
    exit /b 1
)

REM ==== 3) Make sure pip is available ====
python -m pip --version >nul 2>&1
if errorlevel 1 (
    echo [setup] Installing pip...
    python -m ensurepip --upgrade
    if errorlevel 1 (
        echo [!] Could not install pip. Try reinstalling Python.
        pause
        exit /b 1
    )
)

REM ==== 4) Check if all required packages are installed ====
python -c "import fastapi, uvicorn, yadisk, tqdm, dotenv, tenacity, requests" >nul 2>&1
if errorlevel 1 (
    echo [setup] Installing dependencies. This runs ONLY ONCE, takes 1-2 min...
    echo.
    python -m pip install --upgrade pip --quiet --disable-pip-version-check >nul 2>&1
    python -m pip install -r requirements.txt --disable-pip-version-check
    if errorlevel 1 (
        echo.
        echo [!] Failed to install dependencies.
        echo Check your internet connection.
        echo Or run manually: pip install -r requirements.txt
        pause
        exit /b 1
    )
    echo.
    echo [OK] All dependencies installed!
    echo.
)

REM ==== 5) Start the server ====
python webui.py

echo.
echo ============================================================
echo   Server stopped.
echo ============================================================
pause
