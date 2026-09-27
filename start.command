#!/bin/bash
# macOS: double-click to start the paper trader.
cd "$(dirname "$0")" || exit 1
./start.sh
echo
read -r -p "Press Enter to close this window..." _
