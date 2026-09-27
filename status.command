#!/bin/bash
# macOS: double-click to status the paper trader.
cd "$(dirname "$0")" || exit 1
./status.sh
echo
read -r -p "Press Enter to close this window..." _
