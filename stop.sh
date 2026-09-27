#!/usr/bin/env bash
# stop the paper trader (thin wrapper around: .venv/bin/python -m trader stop). macOS/Linux.
cd "$(dirname "$0")" || exit 1
if [ ! -x .venv/bin/python ]; then
  echo "❌ not installed yet — run ./install.sh first"
  exit 1
fi
exec .venv/bin/python -m trader stop "$@"
