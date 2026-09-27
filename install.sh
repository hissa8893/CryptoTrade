#!/usr/bin/env bash
# Installer for macOS / Linux. Idempotent: safe to re-run at any time.
# Tested on Linux. macOS uses this same script (bash 3.2 compatible).
set -u
cd "$(dirname "$0")" || exit 1
ROOT="$(pwd)"
VENV="$ROOT/.venv"
PY_MIN_MAJOR=3
PY_MIN_MINOR=11

say()  { printf '%s\n' "$*"; }
step() { printf '\n==> %s\n' "$*"; }
die()  { printf '\n❌ %s\n' "$*" >&2; exit 1; }

os_name="$(uname -s)"

python_hint() {
  case "$os_name" in
    Darwin) say "    brew install python@3.12        (Homebrew: https://brew.sh)";
            say "    or download the macOS installer from https://www.python.org/downloads/";;
    Linux)  say "    sudo apt install python3.12 python3.12-venv      (Debian/Ubuntu)";
            say "    sudo dnf install python3.12                       (Fedora)";;
    *)      say "    install Python 3.12 from https://www.python.org/downloads/";;
  esac
}

# ---------------------------------------------------------------- 1. find Python 3.11+
step "Checking for Python ${PY_MIN_MAJOR}.${PY_MIN_MINOR}+"
PY=""
for cand in python3.12 python3.13 python3.11 python3 python; do
  if command -v "$cand" >/dev/null 2>&1; then
    if "$cand" -c "import sys; sys.exit(0 if sys.version_info >= (${PY_MIN_MAJOR}, ${PY_MIN_MINOR}) else 1)" >/dev/null 2>&1; then
      PY="$(command -v "$cand")"
      break
    fi
  fi
done
if [ -z "$PY" ]; then
  say "❌ Python ${PY_MIN_MAJOR}.${PY_MIN_MINOR} or newer was not found. Install it with:"
  python_hint
  exit 1
fi
say "found $("$PY" --version 2>&1) at $PY"

# ---------------------------------------------------------------- 2. virtualenv
step "Creating virtual environment (.venv)"
if [ -x "$VENV/bin/python" ] && "$VENV/bin/python" -c "import sys; sys.exit(0 if sys.version_info >= (${PY_MIN_MAJOR}, ${PY_MIN_MINOR}) else 1)" >/dev/null 2>&1; then
  say ".venv already exists — reusing it"
else
  rm -rf "$VENV"
  if ! "$PY" -m venv "$VENV"; then
    say "❌ could not create a virtual environment."
    [ "$os_name" = "Linux" ] && say "    On Debian/Ubuntu: sudo apt install python3.12-venv  (match your Python version)"
    exit 1
  fi
fi
VPY="$VENV/bin/python"

step "Installing pinned dependencies (requirements.txt)"
"$VPY" -m pip install --upgrade --quiet pip || die "pip upgrade failed"
"$VPY" -m pip install --quiet -r requirements.txt || die "dependency install failed (see errors above)"
"$VPY" -m pip install --quiet --no-deps -e . || say "⚠️  editable install failed; use '.venv/bin/python -m trader' instead of 'trader'"

# ---------------------------------------------------------------- 3. dirs, config, DB
step "Creating data/, logs/, run/, reports/ and config files"
"$VPY" -m trader init || die "init failed"
chmod 600 .env 2>/dev/null || true

step "Running database migrations"
"$VPY" -m trader db migrate || die "database migration failed"

step "Downloading price history (first run can take a minute)"
if ! "$VPY" -m trader data fetch; then
  say "⚠️  price download did not fully succeed — the doctor below shows why."
  say "    You can retry any time with: .venv/bin/python -m trader data fetch"
fi

# ---------------------------------------------------------------- 4. doctor
step "Health check"
"$VPY" -m trader doctor
doctor_rc=$?

say ""
if [ $doctor_rc -eq 0 ]; then
  say "✅ Install complete."
else
  say "⚠️  Install finished, but the doctor reported problems above. Fix them, then re-run ./install.sh"
fi
say ""
say "Next:"
say "  1. Start the trader:     ./start.sh        (or double-click start.command on macOS)"
say "  2. Open the dashboard:   http://127.0.0.1:8765"
exit $doctor_rc
