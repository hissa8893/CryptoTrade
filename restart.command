#!/bin/bash
# macOS: double-click to restart the paper trader.
cd "$(dirname "$0")" || exit 1
./restart.sh
echo
read -r -p "Press Enter to close this window..." _
