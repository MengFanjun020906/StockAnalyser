#!/usr/bin/env bash
# =====================================================================
# start-backend.command — Double-clickable backend launcher for macOS.
#
# Finder treats .command files as executable shell scripts; double-click
# opens Terminal, cd's to the script's directory, and runs it. This
# wrapper just delegates to scripts/start-backend.sh so the actual launch
# logic stays in one place.
# =====================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT_DIR"

# Load brew shellenv if available so uv/python/node are on PATH when
# launched from Finder (where the GUI env is minimal).
if [[ -x /opt/homebrew/bin/brew ]]; then
    eval "$(/opt/homebrew/bin/brew shellenv)"
elif [[ -x /usr/local/bin/brew ]]; then
    eval "$(/usr/local/bin/brew shellenv)"
fi

exec bash "$SCRIPT_DIR/start-backend.sh" "$@"
