#!/usr/bin/env bash
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PYTHON_BIN="/Users/rouen/.gemini/antigravity/scratch/discord_order_monitor/venv/bin/python3"
if [ ! -f "$PYTHON_BIN" ]; then
    PYTHON_BIN="python3"
fi
"$PYTHON_BIN" "$DIR/ui_window.py"
