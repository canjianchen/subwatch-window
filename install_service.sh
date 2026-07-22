#!/bin/bash
# Install SubWatch's capture loop as a launchd user agent so it runs independently
# of any terminal / Claude Code session: it starts at login, keeps running after you
# close the terminal, and restarts itself if it crashes.
#
#   ./install_service.sh          install + start
#   ./install_service.sh stop     stop + remove the agent
#
# Logs go to logs/watch.log. Uninstall leaves your data untouched.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LABEL="com.canjianchen.subwatch"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
PYTHON="$(command -v python3)"

if [[ "${1:-}" == "stop" ]]; then
    launchctl unload "$PLIST" 2>/dev/null || true
    rm -f "$PLIST"
    echo "✓ SubWatch service stopped and removed (your data is untouched)."
    exit 0
fi

mkdir -p "$HOME/Library/LaunchAgents" "$ROOT/logs"

cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PYTHON</string>
        <string>$ROOT/src/watch.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$ROOT/src</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$ROOT/logs/watch.log</string>
    <key>StandardErrorPath</key>
    <string>$ROOT/logs/watch.log</string>
</dict>
</plist>
PLISTEOF

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"

echo "✓ SubWatch service installed and started ($LABEL)."
echo "  It now runs on its own — close the terminal, it keeps capturing."
echo "  Logs:  tail -f $ROOT/logs/watch.log"
echo "  Stop:  ./install_service.sh stop"
echo ""
echo "⚠️  Screen Recording permission: launchd runs the loop as '$PYTHON', which is a"
echo "    DIFFERENT binary than your terminal. If capture shows nothing, grant Screen"
echo "    Recording to it: System Settings → Privacy & Security → Screen Recording → add"
echo "    $PYTHON  (the '+' button), then: ./install_service.sh stop && ./install_service.sh"
