#!/bin/bash
# macOS: double-click to logs the paper trader.
cd "$(dirname "$0")" || exit 1
./logs.sh -f
echo
read -r -p "Press Enter to close this window..." _
