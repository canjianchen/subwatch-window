#!/bin/bash
# SubWatch audio-mode setup — guided one-run installer for live audio capture.
# Does everything that doesn't need elevated rights automatically; prompts you
# inline for the few steps macOS reserves for a human (sudo password, GUI clicks).
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
say_step() { printf "\n\033[1;36m▶ %s\033[0m\n" "$1"; }
ok()       { printf "  \033[32m✓ %s\033[0m\n" "$1"; }
warn()     { printf "  \033[33m! %s\033[0m\n" "$1"; }

echo "=== SubWatch Audio Setup ==="

# 1. BlackHole installed?
say_step "1/5  Virtual audio device (BlackHole)"
if [ -d "/Library/Audio/Plug-Ins/HAL/BlackHole2ch.driver" ]; then
  ok "BlackHole driver is installed."
else
  warn "Installing BlackHole (Homebrew may ask for your password)…"
  brew install blackhole-2ch || { warn "brew install failed — install manually: brew install blackhole-2ch"; }
fi

# 2. Is BlackHole loaded as a device?
say_step "2/5  Load the audio driver"
loaded() { python3 -c "import sounddevice as sd; print(any('blackhole' in d['name'].lower() for d in sd.query_devices()))" 2>/dev/null; }
if [ "$(loaded)" = "True" ]; then
  ok "BlackHole is loaded and visible as an audio device."
else
  warn "BlackHole isn't loaded yet. This needs to reload Core Audio (your password)."
  printf "  Run this now? It briefly mutes audio for ~1s. [y/N] "
  read -r ans
  if [ "$ans" = "y" ] || [ "$ans" = "Y" ]; then
    sudo killall coreaudiod && sleep 2
    [ "$(loaded)" = "True" ] && ok "BlackHole now loaded." || warn "Still not loaded — a logout/restart will load it."
  else
    warn "Skipped. Run 'sudo killall coreaudiod' (or restart) later, then re-run this script."
  fi
fi

# 3. Microphone/recording permission for the terminal
say_step "3/5  Recording permission"
echo "  SubWatch reads audio through Python. Your terminal app must be allowed to"
echo "  record: System Settings → Privacy & Security → Microphone → enable your"
echo "  terminal (Ghostty/Terminal/iTerm). (BlackHole audio counts as 'microphone' input.)"
printf "  Open that settings pane now? [y/N] "
read -r ans
[ "$ans" = "y" ] || [ "$ans" = "Y" ] && open "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone"

# 4. Multi-Output device so you still HEAR the video
say_step "4/5  Hear audio AND capture it (Multi-Output Device)"
echo "  To both hear the video and let SubWatch capture it, create a Multi-Output"
echo "  Device that contains BOTH your speakers/headphones AND BlackHole 2ch:"
echo "    • Audio MIDI Setup → '+' (bottom-left) → Create Multi-Output Device"
echo "    • Tick your normal output + 'BlackHole 2ch'"
echo "    • Then System Settings → Sound → Output → choose that Multi-Output Device"
printf "  Open Audio MIDI Setup now? [y/N] "
read -r ans
[ "$ans" = "y" ] || [ "$ans" = "Y" ] && open -a "Audio MIDI Setup"

# 5. Verify
say_step "5/5  Verify"
if [ "$(loaded)" = "True" ]; then
  ok "Ready. Start audio mode with:"
  echo "      cd $ROOT && ./subwatch listen --device BlackHole --model small"
  echo "  (play your video through the Multi-Output device first)"
else
  warn "BlackHole not loaded yet — finish step 2 (reload Core Audio or restart), then:"
  echo "      cd $ROOT && ./subwatch listen --list   # confirm BlackHole appears"
fi
echo
