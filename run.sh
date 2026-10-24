#!/bin/sh
# Universal Video Studio launcher for Linux (like run.bat). Creates a local virtual environment on first run.
# Needs Python 3.11+ with venv:  sudo apt install python3 python3-venv   (Debian/Ubuntu)
cd "$(dirname "$0")" || exit 1
PY="$(command -v python3.12 || command -v python3.11 || command -v python3)"
[ -n "$PY" ] || { echo "Python 3.11 or newer was not found. Install python3 and python3-venv."; exit 1; }
if [ ! -x .venv/bin/python ]; then
  echo "[first run] Creating virtual environment in .venv ..."
  "$PY" -m venv .venv || { echo "Could not create the virtual environment (install python3-venv)."; exit 1; }
  echo "[first run] Installing dependencies (about 250 MB, needs internet) ..."
  .venv/bin/python -m pip install --upgrade pip
  .venv/bin/python -m pip install -r requirements.txt || { echo "Dependency installation failed."; exit 1; }
fi
.venv/bin/python tools/ensure_deps.py || { echo "Dependency update failed."; exit 1; }
exec .venv/bin/python main.py "$@"
