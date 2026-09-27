#!/bin/bash
# macOS double-click wrapper for install.sh.
cd "$(dirname "$0")" || exit 1
./install.sh
echo
read -r -p "Press Enter to close this window..." _
