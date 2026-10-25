@echo off
chcp 65001 >nul 2>&1
title Yandex.Disk Backup
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

REM uv version and SHA-256 of its official Windows archives (github.com/astral-sh/uv/releases).
REM uv.exe is unpacked into the .uv folder next to this file, only if the archive matches.
REM Nothing downloaded is run as a script; PATH and the registry are not touched.
set "UV_VERSION=0.13.4"
set "UV_ZIP_SHA256_X64=ccec7508a07d9933d7cb1261880633efcf1f30d6e6eddab21823f1dae6f23225"
set "UV_ZIP_SHA256_ARM64=c4bc5a9534b2bb6a12fe29ea748445e1a6f9cb7bb275211c4fb74c52ecf317a5"
set "UV_ZIP_SHA256_X86=b450f623b34bf85322bbc656d5879915acd818dc5b05fc2d33e343da26799e37"

echo ============================================================
echo   Yandex.Disk Backup
echo ============================================================
echo.

REM uv manages Python and all dependencies (pyproject.toml + uv.lock); --no-dev skips test tools.
REM Use the uv already on this computer, else the one in .uv, else download it once.
REM Paths below are relative to this folder (cd above): curl and tar get no non-ASCII names.
set "UV=uv"
where uv >nul 2>&1
if not errorlevel 1 goto :run
set "UV=%USERPROFILE%\.local\bin\uv.exe"
if exist "%UV%" goto :run
set "UV=.uv\uv.exe"
if exist "%UV%" goto :run

echo [setup] uv not found. Downloading uv %UV_VERSION% into the .uv folder - runs ONLY ONCE...
echo.
REM curl and tar come with Windows 10 since version 1803
where curl.exe >nul 2>&1
if errorlevel 1 goto :uvold
where tar.exe >nul 2>&1
if errorlevel 1 goto :uvold
set "UV_ARCH=x86_64"
set "UV_ZIP_SHA256=%UV_ZIP_SHA256_X64%"
if /i "%PROCESSOR_ARCHITECTURE%"=="x86" if not defined PROCESSOR_ARCHITEW6432 set "UV_ARCH=i686"
if /i "%PROCESSOR_ARCHITECTURE%"=="ARM64" set "UV_ARCH=aarch64"
if /i "%PROCESSOR_ARCHITEW6432%"=="ARM64" set "UV_ARCH=aarch64"
if "%UV_ARCH%"=="i686" set "UV_ZIP_SHA256=%UV_ZIP_SHA256_X86%"
if "%UV_ARCH%"=="aarch64" set "UV_ZIP_SHA256=%UV_ZIP_SHA256_ARM64%"
set "UV_ZIP=.uv\uv-%UV_VERSION%.zip"
set "UV_ZIP_URL=https://github.com/astral-sh/uv/releases/download/%UV_VERSION%/uv-%UV_ARCH%-pc-windows-msvc.zip"
if not exist .uv mkdir .uv
curl.exe --fail --location --retry 3 --progress-bar --output "%UV_ZIP%" "%UV_ZIP_URL%"
if errorlevel 1 goto :uvfail
powershell -NoProfile -Command "if ((Get-FileHash -Algorithm SHA256 -LiteralPath $env:UV_ZIP).Hash -ne $env:UV_ZIP_SHA256) { exit 1 }"
if errorlevel 1 goto :uvbad
tar -xf "%UV_ZIP%" -C .uv uv.exe
if errorlevel 1 goto :uvfail
del "%UV_ZIP%" >nul 2>&1
if not exist "%UV%" goto :uvfail
echo.
echo [OK] uv installed.
echo.

:run
echo [*] Starting via uv (first run downloads Python and dependencies, 1-2 min)...
echo.
"%UV%" run --no-dev webui.py
goto :done

:uvbad
del "%UV_ZIP%" >nul 2>&1
echo.
echo [!] The downloaded uv archive does not match the expected checksum - not using it.
echo     Run this file again later, or install uv manually:
echo     https://docs.astral.sh/uv/getting-started/installation/
echo.
pause
exit /b 1

:uvold
echo.
echo [!] This Windows is too old to download uv by itself (no curl or tar: Windows 10 before 1803).
echo     Install uv manually and run this file again:
echo     https://docs.astral.sh/uv/getting-started/installation/
echo.
start "" "https://docs.astral.sh/uv/getting-started/installation/"
pause
exit /b 1

:uvfail
del "%UV_ZIP%" >nul 2>&1
echo.
echo [!] Could not install uv automatically.
echo     Check the internet connection and run this file again, or install uv manually:
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
