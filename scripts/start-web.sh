#!/usr/bin/env bash
# =====================================================================
# start-web.sh — Start Vite dev server for apps/dsa-web (WSL/Linux).
#
# Assumptions:
#   - apps/dsa-web/node_modules already installed (scripts/bootstrap-wsl.sh)
#   - Backend reachable via the proxy target configured in vite.config.ts
#
# Env overrides:
#   FRONTEND_HOST   (default 0.0.0.0  — required for Windows browser access)
#   FRONTEND_PORT   (default 5173)
# =====================================================================
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WEB_DIR="$ROOT_DIR/apps/dsa-web"

if [[ ! -d "$WEB_DIR" ]]; then
    echo "[start-web] apps/dsa-web not found at $WEB_DIR" >&2
    exit 1
fi

if [[ ! -d "$WEB_DIR/node_modules" ]]; then
    echo "[start-web] node_modules missing. Run 'cd apps/dsa-web && npm ci' first." >&2
    exit 1
fi

FRONTEND_HOST="${FRONTEND_HOST:-0.0.0.0}"
FRONTEND_PORT="${FRONTEND_PORT:-5173}"

LOG_DIR="$ROOT_DIR/logs/dev"
mkdir -p "$LOG_DIR"

cd "$WEB_DIR"

echo "[start-web] vite dev server on http://${FRONTEND_HOST}:${FRONTEND_PORT}"
echo "[start-web] logs -> $LOG_DIR/frontend.log (tee)"
echo "[start-web] Ctrl+C to stop."

exec ./node_modules/.bin/vite \
    --host "$FRONTEND_HOST" --port "$FRONTEND_PORT" \
    2>&1 | tee -a "$LOG_DIR/frontend.log"
