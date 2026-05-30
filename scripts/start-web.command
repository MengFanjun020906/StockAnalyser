#!/usr/bin/env bash
# =====================================================================
# start-web.command — Double-clickable web dev launcher for macOS.
#
# See start-backend.command for the rationale. Delegates to
# scripts/start-web.sh so behavior stays consistent with the CLI path.
# =====================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT_DIR"

if [[ -x /opt/homebrew/bin/brew ]]; then
    eval "$(/opt/homebrew/bin/brew shellenv)"
elif [[ -x /usr/local/bin/brew ]]; then
    eval "$(/usr/local/bin/brew shellenv)"
fi

exec bash "$SCRIPT_DIR/start-web.sh" "$@"
