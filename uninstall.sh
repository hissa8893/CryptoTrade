#!/usr/bin/env bash
# Uninstall (macOS/Linux): remove the auto-start service, stop the trader, remove .venv.
# Asks before deleting data/ (your trade history). Pass --delete-data or --keep-data to skip the question.
cd "$(dirname "$0")" || exit 1
export TRADER_HOME="${TRADER_HOME:-$PWD}"  # this folder's install, never another one
if [ -x .venv/bin/python ]; then
  .venv/bin/python -m trader uninstall "$@" || { echo "❌ uninstall step failed; .venv left in place"; exit 1; }
else
  echo "(.venv not found: nothing to stop; skipping service removal)"
fi
rm -rf .venv trader.egg-info
echo "✅ removed .venv. The project folder is still here; delete it yourself if you want everything gone."
