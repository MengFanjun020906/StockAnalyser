#!/usr/bin/env bash
# =====================================================================
# bootstrap-wsl.sh — One-shot dependency install for StockAnalyser inside WSL2
#
# Target: Ubuntu (WSL2). Tested on Ubuntu 22.04 / 24.04.
# Assumes: project source already copied into WSL filesystem.
# Excludes: graphiti, Neo4j, existing SQLite DB transfers.
#
# What this does:
#   1. apt update + base build tools, sqlite3, git, curl
#   2. Install Python 3.11 (via deadsnakes PPA if needed)
#   3. Install uv (fast Python package manager)
#   4. Install Node.js 22 (via NodeSource)
#   5. Create .venv with Python 3.11, install requirements.txt (sans graphiti)
#   6. npm ci in apps/dsa-web
#   7. Initialize empty data/ and logs/ directories
#   8. Copy .env.example -> .env if .env missing
#   9. (Optional) download Sequoia candidate DB if URL provided
#
# Usage:
#   cd ~/code/StockAnalyser
#   bash scripts/bootstrap-wsl.sh
#
# Env overrides:
#   SKIP_NPM=1                 -> skip npm ci
#   SKIP_DESKTOP=1             -> skip apps/dsa-desktop (default skip)
#   SEQUOIA_DB_URL=<url>       -> if set, curl -o Sequoia-X/data/sequoia_v2.db
# =====================================================================
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

SKIP_NPM="${SKIP_NPM:-0}"
SKIP_DESKTOP="${SKIP_DESKTOP:-1}"
SEQUOIA_DB_URL="${SEQUOIA_DB_URL:-}"

log()  { printf '\n\033[36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33m[warn] %s\033[0m\n' "$*"; }
die()  { printf '\033[31m[error] %s\033[0m\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------------
# 0. Sanity check: must be Linux (WSL is fine)
# ---------------------------------------------------------------------
if [[ "$(uname -s)" != "Linux" ]]; then
    die "This script must run inside WSL/Linux, not $(uname -s)."
fi

if [[ ! -f requirements.txt ]]; then
    die "requirements.txt not found. Run from the project root."
fi

# ---------------------------------------------------------------------
# 1. apt base
# ---------------------------------------------------------------------
log "Step 1/9: apt update + base packages"
sudo apt-get update -y
sudo apt-get install -y --no-install-recommends \
    build-essential ca-certificates curl wget git \
    software-properties-common gnupg lsb-release \
    sqlite3 libsqlite3-dev \
    pkg-config libssl-dev libffi-dev

# ---------------------------------------------------------------------
# 2. Python 3.11
# ---------------------------------------------------------------------
log "Step 2/9: Python 3.11"
if ! command -v python3.11 >/dev/null 2>&1; then
    UBUNTU_VER="$(lsb_release -rs || echo unknown)"
    case "$UBUNTU_VER" in
        22.04|20.04)
            sudo add-apt-repository -y ppa:deadsnakes/ppa
            sudo apt-get update -y
            sudo apt-get install -y python3.11 python3.11-venv python3.11-dev
            ;;
        24.04)
            # 24.04 ships python3.12 by default; deadsnakes still has 3.11
            sudo add-apt-repository -y ppa:deadsnakes/ppa || true
            sudo apt-get update -y
            sudo apt-get install -y python3.11 python3.11-venv python3.11-dev \
                || sudo apt-get install -y python3 python3-venv python3-dev
            ;;
        *)
            warn "Unrecognized Ubuntu $UBUNTU_VER; falling back to system python3"
            sudo apt-get install -y python3 python3-venv python3-dev
            ;;
    esac
fi

PYTHON_BIN="$(command -v python3.11 || command -v python3)"
log "Using Python: $PYTHON_BIN ($($PYTHON_BIN --version))"

# ---------------------------------------------------------------------
# 3. uv (fast pip replacement)
# ---------------------------------------------------------------------
log "Step 3/9: uv"
if ! command -v uv >/dev/null 2>&1; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
    # uv installs to ~/.local/bin
    export PATH="$HOME/.local/bin:$PATH"
fi
uv --version || warn "uv not on PATH; you may need to re-login or 'source ~/.bashrc'"

# ---------------------------------------------------------------------
# 4. Node.js 22 (matches .nvmrc)
# ---------------------------------------------------------------------
log "Step 4/9: Node.js 22"
NODE_REQUIRED="22"
NODE_INSTALL=1
if command -v node >/dev/null 2>&1; then
    CUR="$(node -v | sed 's/v//;s/\..*//')"
    if [[ "$CUR" == "$NODE_REQUIRED" ]]; then
        NODE_INSTALL=0
        log "Node $CUR already installed"
    fi
fi
if [[ "$NODE_INSTALL" == "1" ]]; then
    curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
    sudo apt-get install -y nodejs
fi
log "Node: $(node -v) / npm: $(npm -v)"

# ---------------------------------------------------------------------
# 5. Python venv + requirements (sans graphiti)
# ---------------------------------------------------------------------
log "Step 5/9: .venv + Python deps"
if [[ ! -d .venv ]]; then
    "$PYTHON_BIN" -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate

# Strip graphiti-related lines from requirements before install
REQ_TMP="$(mktemp)"
grep -viE '^(graphiti|neo4j)' requirements.txt > "$REQ_TMP"

if command -v uv >/dev/null 2>&1; then
    uv pip install --python "$(command -v python)" -r "$REQ_TMP"
else
    python -m pip install --upgrade pip
    python -m pip install -r "$REQ_TMP"
fi
rm -f "$REQ_TMP"

# ---------------------------------------------------------------------
# 6. Web frontend
# ---------------------------------------------------------------------
if [[ "$SKIP_NPM" == "0" ]]; then
    log "Step 6/9: apps/dsa-web npm ci"
    (cd apps/dsa-web && npm ci)
else
    warn "Step 6/9: skipped (SKIP_NPM=1)"
fi

if [[ "$SKIP_DESKTOP" == "0" ]]; then
    log "       apps/dsa-desktop npm install (optional)"
    (cd apps/dsa-desktop && npm install)
else
    warn "       desktop app skipped (set SKIP_DESKTOP=0 to install)"
fi

# ---------------------------------------------------------------------
# 7. Local data dirs
# ---------------------------------------------------------------------
log "Step 7/9: data/ logs/ dirs"
mkdir -p data logs Sequoia-X/data .cache/candidate_experts_v2

# ---------------------------------------------------------------------
# 8. .env
# ---------------------------------------------------------------------
log "Step 8/9: .env"
if [[ -f .env ]]; then
    log ".env already exists, leaving it alone"
else
    cp .env.example .env
    warn ".env created from .env.example — fill in TUSHARE_TOKEN / LLM keys before running"
fi

# Force-disable graphiti regardless of inherited config
if ! grep -q '^GRAPHITI_ENABLED=' .env 2>/dev/null; then
    echo 'GRAPHITI_ENABLED=false' >> .env
fi

# ---------------------------------------------------------------------
# 9. Optional: Sequoia candidate DB
# ---------------------------------------------------------------------
log "Step 9/9: Sequoia DB (optional)"
if [[ -n "$SEQUOIA_DB_URL" ]]; then
    DEST="Sequoia-X/data/sequoia_v2.db"
    if [[ -f "$DEST" ]]; then
        log "$DEST already exists, skipping download"
    else
        log "Downloading $SEQUOIA_DB_URL -> $DEST"
        curl -fL --retry 3 -o "$DEST" "$SEQUOIA_DB_URL"
    fi
else
    warn "SEQUOIA_DB_URL not set; skip Sequoia DB download."
    warn "You can regenerate it locally later with:"
    warn "  python scripts/update_sequoia_candidates.py --help"
fi

# ---------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------
log "Bootstrap complete."
cat <<EOF

Next steps:
  1. Edit .env and fill in your API keys (TUSHARE_TOKEN, DEEPSEEK_API_KEY, ...).
  2. Start the backend:
       source .venv/bin/activate
       bash scripts/start-backend.sh
  3. In another shell, start the web dev server:
       bash scripts/start-web.sh
  4. Visit http://localhost:5173

If you prefer the existing combined launcher (it tries Neo4j by default —
we disable that here):
       START_NEO4J=false bash start_all.sh
EOF
