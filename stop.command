#!/bin/bash
# macOS: double-click to stop the paper trader.
cd "$(dirname "$0")" || exit 1
./stop.sh
echo
read -r -p "Press Enter to close this window..." _
