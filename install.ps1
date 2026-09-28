# Installer for Windows (PowerShell). Idempotent: safe to re-run.
# UNTESTED: written to mirror install.sh (tested on Linux, used on macOS) but never run on Windows.
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot
function Step($m) { Write-Host "`n==> $m" }

Step "Checking for Python 3.11+"
$py = $null
foreach ($cand in @(@("py", "-3.12"), @("py", "-3.13"), @("py", "-3.11"), @("python"))) {
  try {
    $exe = $cand[0]; $args0 = @($cand | Select-Object -Skip 1)  # (a [1..n] slice breaks on 1 item)
    & $exe @args0 -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" 2>$null
    if ($LASTEXITCODE -eq 0) { $py = $cand; break }
  } catch {}
}
if (-not $py) {
  Write-Host "Python 3.11 or newer was not found. Install it with:"
  Write-Host "    winget install Python.Python.3.12"
  Write-Host "or download it from https://www.python.org/downloads/ (tick 'Add python.exe to PATH')."
  exit 1
}
$exe = $py[0]; $pyargs = @($py | Select-Object -Skip 1)

Step "Creating virtual environment (.venv)"
if (-not (Test-Path ".venv\Scripts\python.exe")) { & $exe @pyargs -m venv .venv }
else { Write-Host ".venv already exists - reusing it" }
$vpy = ".venv\Scripts\python.exe"

Step "Installing pinned dependencies (requirements.txt)"
& $vpy -m pip install --upgrade --quiet pip
& $vpy -m pip install --quiet -r requirements.txt
if ($LASTEXITCODE -ne 0) { Write-Host "dependency install failed (see errors above)"; exit 1 }
& $vpy -m pip install --quiet --no-deps -e .
if (Test-Path "requirements-llm.txt") {
  Step "Installing the optional AI analyst packages (requirements-llm.txt)"
  & $vpy -m pip install --quiet -r requirements-llm.txt
  if ($LASTEXITCODE -ne 0) { Write-Host "optional AI packages did not install; the trader works without them" }
}

Step "Creating data\, logs\, run\, reports\ and config files"
& $vpy -m trader init
# owner-only access to the secrets file
icacls .env /inheritance:r /grant:r "$($env:USERNAME):(R,W)" | Out-Null

Step "Running database migrations"
& $vpy -m trader db migrate

Step "Downloading price history"
& $vpy -m trader data fetch
if ($LASTEXITCODE -ne 0) { Write-Host "price download did not fully succeed - see the health check below" }

Step "Health check"
& $vpy -m trader doctor
Write-Host "`nNext:"
Write-Host "  1. Start the trader:     double-click start.bat"
Write-Host "  2. Open the dashboard:   http://127.0.0.1:8765"
Write-Host "  3. Optional, start it at every login:   .venv\Scripts\trader service install"
Write-Host "  Uninstall: uninstall.bat - Guide: README.md"
