#!/usr/bin/env bash
# Yandex.Disk Backup — Linux/macOS launcher. uv manages Python and dependencies.

set -e
cd "$(dirname "$0")"

export PYTHONIOENCODING=utf-8
export PYTHONUTF8=1

echo "============================================================"
echo "  Yandex.Disk Backup"
echo "============================================================"
echo

# Версия uv и SHA-256 его официального установщика (github.com/astral-sh/uv/releases):
# установщик запускается, только если скачанный файл совпал с этой суммой.
UV_VERSION="0.13.4"
UV_INSTALLER_SHA256="1f5390d273b0158bd5eaa3527ae049f3667baa881abb1e7c883dd637d84432f9"

sha256_of() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | cut -d' ' -f1
    else
        shasum -a 256 "$1" | cut -d' ' -f1
    fi
}

if ! command -v uv >/dev/null 2>&1; then
    echo "[setup] uv not found. Installing uv $UV_VERSION - runs ONLY ONCE..."
    echo
    url="https://github.com/astral-sh/uv/releases/download/$UV_VERSION/uv-installer.sh"
    installer="$(mktemp "${TMPDIR:-/tmp}/uv-installer.XXXXXX")"
    if command -v curl >/dev/null 2>&1; then
        curl -LsSf "$url" -o "$installer" || { rm -f "$installer"; echo "[!] Could not download the uv installer."; exit 1; }
    elif command -v wget >/dev/null 2>&1; then
        wget -qO "$installer" "$url" || { rm -f "$installer"; echo "[!] Could not download the uv installer."; exit 1; }
    else
        rm -f "$installer"
        echo "[!] Neither curl nor wget found. Install uv manually:"
        echo "    https://docs.astral.sh/uv/getting-started/installation/"
        exit 1
    fi
    if [ "$(sha256_of "$installer")" != "$UV_INSTALLER_SHA256" ]; then
        rm -f "$installer"
        echo "[!] The downloaded uv installer does not match the expected checksum - not running it."
        echo "    Run this file again later, or install uv manually:"
        echo "    https://docs.astral.sh/uv/getting-started/installation/"
        exit 1
    fi
    sh "$installer" || { rm -f "$installer"; echo "[!] The uv installer failed."; exit 1; }
    rm -f "$installer"
    export PATH="$HOME/.local/bin:$PATH"
    if ! command -v uv >/dev/null 2>&1; then
        echo "[!] Could not install uv automatically. Install it manually:"
        echo "    https://docs.astral.sh/uv/getting-started/installation/"
        exit 1
    fi
    echo
    echo "[OK] uv installed."
    echo
fi

echo "[*] Starting via uv (first run downloads Python and dependencies, 1-2 min)..."
echo
exec uv run --no-dev webui.py
