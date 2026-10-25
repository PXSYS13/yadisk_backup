@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
cd /d "%~dp0"
REM The same uv as webui.bat: installed on this computer, or the one it put into .uv
set "UV=uv"
where uv >nul 2>&1
if not errorlevel 1 goto :run
set "UV=%USERPROFILE%\.local\bin\uv.exe"
if exist "%UV%" goto :run
set "UV=.uv\uv.exe"
if exist "%UV%" goto :run
echo [!] uv not found. Run webui.bat once - it installs uv automatically.
pause
exit /b 1

:run
"%UV%" run --no-dev watch.py
echo.
pause
