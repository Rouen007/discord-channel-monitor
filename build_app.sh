#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

PYTHON_BIN="/Users/rouen/.gemini/antigravity/scratch/discord_order_monitor/venv/bin/python"
if [ ! -f "$PYTHON_BIN" ]; then
    PYTHON_BIN="python3"
fi

"$PYTHON_BIN" -m PyInstaller --noconfirm DiscordChannelMonitor.spec

# Inject LSUIElement = 1 into Info.plist to allow the window to float across all Spaces and over Full Screen apps
/usr/libexec/PlistBuddy -c "Add :LSUIElement string 1" "dist/DiscordChannelMonitor.app/Contents/Info.plist" 2>/dev/null || \
/usr/libexec/PlistBuddy -c "Set :LSUIElement 1" "dist/DiscordChannelMonitor.app/Contents/Info.plist"

codesign --force --deep --sign - "dist/DiscordChannelMonitor.app"
echo "✅ Build complete: dist/DiscordChannelMonitor.app"
