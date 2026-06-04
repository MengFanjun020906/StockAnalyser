#!/usr/bin/env bash
# =====================================================================
# start-backend.sh — Start FastAPI backend in foreground (WSL/Linux).
#
# Assumptions:
#   - .venv exists at project root (created by scripts/bootstrap-wsl.sh)
#   - .env is populated with TUSHARE_TOKEN / LLM keys
#   - graphiti / Neo4j is disabled (GRAPHITI_ENABLED=false)
#
# Env overrides:
#   BACKEND_HOST  (default 0.0.0.0)
#   BACKEND_PORT  (default 8000)
#   AGENT_MODE                 (default true)
#   AGENT_ANALYSIS_MODE        (default planning_execute)
# =====================================================================
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

BACKEND_HOST="${BACKEND_HOST:-0.0.0.0}"
BACKEND_PORT="${BACKEND_PORT:-8000}"

LOG_DIR="$ROOT_DIR/logs/dev"
RUN_DIR="$ROOT_DIR/data/run"
mkdir -p "$LOG_DIR" "$RUN_DIR"

# Pick python: prefer project .venv
if [[ -x "$ROOT_DIR/.venv/bin/python" ]]; then
    PYTHON_BIN="$ROOT_DIR/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v python3)"
else
    PYTHON_BIN="$(command -v python)"
fi

if [[ ! -f "$ROOT_DIR/main.py" ]]; then
    echo "[start-backend] main.py not found in $ROOT_DIR" >&2
    exit 1
fi

if [[ ! -f "$ROOT_DIR/.env" ]]; then
    echo "[start-backend] WARN: .env not found; relying on shell env vars only."
fi

echo "[start-backend] python = $PYTHON_BIN"
echo "[start-backend] listening on http://${BACKEND_HOST}:${BACKEND_PORT}"
echo "[start-backend] logs -> $LOG_DIR/backend.log (tee)"
echo "[start-backend] Ctrl+C to stop."

exec env \
    AGENT_MODE="${AGENT_MODE:-true}" \
    AGENT_ANALYSIS_MODE="${AGENT_ANALYSIS_MODE:-planning_execute}" \
    GRAPHITI_ENABLED="${GRAPHITI_ENABLED:-false}" \
    "$PYTHON_BIN" main.py --serve-only \
        --host "$BACKEND_HOST" --port "$BACKEND_PORT" \
    2>&1 | tee -a "$LOG_DIR/backend.log"
