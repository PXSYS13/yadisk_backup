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

if ! command -v uv >/dev/null 2>&1; then
    echo "[setup] uv not found. Installing uv - runs ONLY ONCE..."
    echo
    if command -v curl >/dev/null 2>&1; then
        curl -LsSf https://astral.sh/uv/install.sh | sh
    elif command -v wget >/dev/null 2>&1; then
        wget -qO- https://astral.sh/uv/install.sh | sh
    else
        echo "[!] Neither curl nor wget found. Install uv manually:"
        echo "    https://docs.astral.sh/uv/getting-started/installation/"
        exit 1
    fi
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
exec uv run webui.py
