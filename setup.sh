#!/bin/bash
# SubWatch one-shot setup for a fresh machine (macOS). Run: ./setup.sh
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
step(){ printf "\n\033[1;36m▶ %s\033[0m\n" "$1"; }
ok(){ printf "  \033[32m✓ %s\033[0m\n" "$1"; }
warn(){ printf "  \033[33m! %s\033[0m\n" "$1"; }

echo "=== SubWatch setup ==="
[ "$(uname)" = "Darwin" ] || { warn "This installer is for macOS. On Windows, OCR + overlays need porting (see README)."; }

step "1/5  Python packages"
python3 -m pip install --user -r "$ROOT/requirements.txt" 2>&1 | tail -2
ok "core packages installed"

step "2/5  Command-line tools (Homebrew)"
if command -v brew >/dev/null; then
  brew list switchaudio-osx >/dev/null 2>&1 || brew install switchaudio-osx
  brew list blackhole-2ch >/dev/null 2>&1 || { warn "Installing BlackHole (audio mode; needs password)…"; brew install blackhole-2ch; }
  command -v ffmpeg >/dev/null || brew install ffmpeg
  ok "brew tools ready"
else
  warn "Homebrew not found — install it (brew.sh) for audio mode. OCR mode works without it."
fi

step "3/5  Self-hosted UI libraries"
mkdir -p "$ROOT/src/static"
fetch(){ curl -sL --max-time 40 "$1" -o "$2" && ok "$(basename "$2")"; }
fetch "https://unpkg.com/alpinejs@3/dist/cdn.min.js"          "$ROOT/src/static/alpine.min.js"
fetch "https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"   "$ROOT/src/static/tailwind.js"
fetch "https://unpkg.com/lucide@latest"                        "$ROOT/src/static/lucide.js"

step "4/5  Build the OCR helper (Swift Vision)"
if command -v swiftc >/dev/null; then
  swiftc -O "$ROOT/src/ocr_helper.swift" -o "$ROOT/bin/ocr_helper" && ok "ocr_helper built for this Mac"
else
  warn "swiftc not found — install Xcode command-line tools: xcode-select --install"
fi

step "5/5  What you still need to do"
cat <<EOF
  • Codex login (for smart capture and meeting AI): run 'codex login' once.
    Without it, OCR capture + local Whisper and word lists still work.
  • Permissions: System Settings → Privacy & Security →
       Screen Recording  → enable your terminal   (for subtitle capture)
       Microphone        → enable your terminal   (for audio mode)
  • Audio mode routing: run ./setup_audio.sh to create the BlackHole Multi-Output device.

Then start it:   ./subwatch panel      (opens the dashboard)
EOF
echo
