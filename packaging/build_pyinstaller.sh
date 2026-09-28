#!/usr/bin/env bash
# OPTIONAL: build a self-contained dist/trader/ folder with PyInstaller (runs without Python).
# The normal ./install.sh route is the supported one; this is for people who prefer a binary.
# Build on the same kind of machine you will run it on: a Mac build must be made on a Mac
# (and separately for Apple Silicon and Intel). The result is unsigned (see README).
set -euo pipefail
cd "$(dirname "$0")/.."
PY="${PYTHON:-python3}"
"$PY" -m venv build/pyi-venv
build/pyi-venv/bin/pip install --quiet -r requirements.txt pyinstaller==6.22.3
build/pyi-venv/bin/pip install --quiet --no-deps .
build/pyi-venv/bin/pyinstaller --noconfirm --clean --onedir --name trader \
  --distpath dist --workpath build/pyi-work --specpath build \
  --collect-data trader --collect-submodules trader --collect-submodules uvicorn \
  "$PWD/packaging/pyinstaller_entry.py"
cp config.example.yaml .env.example requirements.txt dist/trader/
echo "✅ built dist/trader/  — run: dist/trader/trader init   (data, logs and config live in that folder)"
