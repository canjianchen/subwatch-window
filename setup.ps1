# SubWatch one-shot setup for Windows. Run from PowerShell:
#     powershell -ExecutionPolicy Bypass -File .\setup.ps1
#
# Mirrors setup.sh (the macOS installer). Installs Python packages, fetches the
# self-hosted UI libraries the panel needs, and prints what's left to do.
$ErrorActionPreference = "Stop"
$ROOT = $PSScriptRoot

function Step($m) { Write-Host "`n> $m" -ForegroundColor Cyan }
function Ok($m)   { Write-Host "  [ok] $m" -ForegroundColor Green }
function Warn($m) { Write-Host "  [!] $m"  -ForegroundColor Yellow }

Write-Host "=== SubWatch setup (Windows) ==="

# Resolve a Python launcher: prefer `python`, else `py`.
$PY = $null
foreach ($cand in @("python", "py")) {
    if (Get-Command $cand -ErrorAction SilentlyContinue) { $PY = $cand; break }
}
if (-not $PY) {
    Warn "Python not found. Install Python 3.9+ from python.org (tick 'Add to PATH'), then re-run."
    exit 1
}
Ok "using '$PY' ($(& $PY --version 2>&1))"

Step "1/3  Python packages"
# requirements.txt uses platform markers, so pip installs only the Windows +
# cross-platform deps here (mss, winrt, sounddevice, numpy, whisper).
$venv = Join-Path $ROOT ".venv"
& $PY -m venv $venv
if ($LASTEXITCODE -ne 0) { throw "Could not create the Python virtual environment." }
$VENV_PY = Join-Path $venv "Scripts\python.exe"
& $VENV_PY -m pip install --no-cache-dir -r (Join-Path $ROOT "requirements.txt")
if ($LASTEXITCODE -ne 0) { throw "Python package installation failed." }
Ok "packages installed"

Step "2/3  Self-hosted UI libraries (the panel needs these)"
$staticDir = Join-Path $ROOT "src\static"
New-Item -ItemType Directory -Force -Path $staticDir | Out-Null
$libs = @{
    "alpine.min.js" = "https://unpkg.com/alpinejs@3/dist/cdn.min.js"
    "tailwind.js"   = "https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"
    "lucide.js"     = "https://unpkg.com/lucide@latest"
}
foreach ($name in $libs.Keys) {
    $dest = Join-Path $staticDir $name
    try {
        Invoke-WebRequest -Uri $libs[$name] -OutFile $dest -TimeoutSec 40 -UseBasicParsing
        Ok $name
    } catch {
        Warn "could not fetch $name - the panel will render unstyled. Re-run when online."
    }
}

Step "3/3  What you still need to do"
@"
  - OCR language pack (subtitle mode): Settings > Time & Language > Language,
    add 'Chinese (Simplified)', then add the optional feature 'Optical character
    recognition' under Language options. English works out of the box.
  - Codex login (optional - smart word-grading + meeting AI):
    run 'codex login' once. Without a login, SubWatch falls back to local-only:
    OCR capture, review, notes, and the panel still work.
  - Audio mode (subwatch listen) is heavier on Windows: it needs a virtual audio
    device to hear the video (e.g. VB-CABLE) plus the whisper install above.
    Subtitle mode needs none of that.

Then start it:   .\subwatch.bat panel      (opens the dashboard at http://127.0.0.1:8770)
"@ | Write-Host
Write-Host ""
