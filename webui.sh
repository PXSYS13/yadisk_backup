#!/usr/bin/env bash
# Yandex.Disk Backup — Linux/macOS launcher with auto-setup

set -e
cd "$(dirname "$0")"

export PYTHONIOENCODING=utf-8
export PYTHONUTF8=1

echo "============================================================"
echo "  Yandex.Disk Backup"
echo "============================================================"
echo

# 1) Find Python 3.10+
PY=""
for cand in python3.13 python3.12 python3.11 python3.10 python3 python; do
    if command -v "$cand" >/dev/null 2>&1; then
        if "$cand" -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" 2>/dev/null; then
            PY="$cand"
            break
        fi
    fi
done

if [ -z "$PY" ]; then
    echo "[!] Python 3.10+ not found."
    echo "Install Python first:"
    echo "  Ubuntu/Debian: sudo apt install python3 python3-pip"
    echo "  macOS:         brew install python"
    exit 1
fi

# 2) Make sure pip is available
if ! "$PY" -m pip --version >/dev/null 2>&1; then
    echo "[setup] Installing pip..."
    "$PY" -m ensurepip --upgrade
fi

# 3) Check dependencies
if ! "$PY" -c "import fastapi, uvicorn, yadisk, tqdm, dotenv, tenacity, requests" >/dev/null 2>&1; then
    echo "[setup] Installing dependencies. Runs ONLY ONCE, takes 1-2 min..."
    echo
    "$PY" -m pip install --upgrade pip --quiet 2>/dev/null || true
    "$PY" -m pip install -r requirements.txt
    echo
    echo "[OK] All dependencies installed!"
    echo
fi

# 4) Start server
"$PY" webui.py
