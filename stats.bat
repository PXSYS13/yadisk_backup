@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
cd /d "%~dp0"
where uv >nul 2>&1
if errorlevel 1 (
    echo [!] uv not found. Run webui.bat once - it installs uv automatically.
    pause
    exit /b 1
)
uv run main.py stats
echo.
pause
