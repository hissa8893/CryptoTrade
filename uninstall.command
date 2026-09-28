#!/bin/bash
# macOS: double-click to uninstall (asks before deleting your trade history).
cd "$(dirname "$0")" || exit 1
./uninstall.sh
echo
read -r -p "Press Enter to close this window..." _
