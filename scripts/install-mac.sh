#!/usr/bin/env bash
# =====================================================================
# install-mac.sh — One-shot dependency install for StockAnalyser on macOS
#
# Target: macOS (Intel default; Apple Silicon also auto-detected).
# Assumes: project source already copied to local disk; you ran this from
#          the project root: `bash scripts/install-mac.sh`.
# Excludes: graphiti, Neo4j, existing SQLite DB transfers.
#
# What this does:
#   1. Install Homebrew (if missing) and wire up brew shellenv for the
#      current shell + future logins (.zprofile / .bash_profile).
#   2. brew install python@3.11 node@22 uv git sqlite (+ link node@22).
#   3. Create .venv with Python 3.11; install requirements.txt with
#      graphiti / neo4j lines stripped; force GRAPHITI_ENABLED=false.
#   4. npm ci in apps/dsa-web.
#   5. Initialize data/ logs/ Sequoia-X/data/ .cache/candidate_experts_v2/.
#   6. Copy .env.example -> .env if missing.
#   7. (Optional) download Sequoia candidate DB if SEQUOIA_DB_URL set.
#
# Usage:
#   cd ~/Projects/StockAnalyser
#   bash scripts/install-mac.sh
#
# Env overrides:
#   SKIP_NPM=1                 -> skip npm ci
#   SKIP_DESKTOP=1             -> skip apps/dsa-desktop (default skip)
#   SEQUOIA_DB_URL=<url>       -> curl -o Sequoia-X/data/sequoia_v2.db
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
# 0. Sanity check: must be macOS
# ---------------------------------------------------------------------
if [[ "$(uname -s)" != "Darwin" ]]; then
    die "This script must run on macOS (Darwin), not $(uname -s)."
fi

if [[ ! -f requirements.txt ]]; then
    die "requirements.txt not found. Run from the project root."
fi

ARCH="$(uname -m)"
case "$ARCH" in
    x86_64) BREW_PREFIX_DEFAULT="/usr/local" ;;
    arm64)  BREW_PREFIX_DEFAULT="/opt/homebrew" ;;
    *)      die "Unsupported macOS arch: $ARCH" ;;
esac
log "Detected arch: $ARCH (brew prefix default: $BREW_PREFIX_DEFAULT)"

# Detect the user's login shell rc file so brew shellenv survives across sessions.
CURRENT_SHELL="$(basename "${SHELL:-/bin/zsh}")"
case "$CURRENT_SHELL" in
    zsh)  SHELL_RC="$HOME/.zprofile" ;;
    bash) SHELL_RC="$HOME/.bash_profile" ;;
    *)    SHELL_RC="$HOME/.profile" ;;
esac

# ---------------------------------------------------------------------
# 1. Xcode Command Line Tools (provides clang, git, make)
# ---------------------------------------------------------------------
log "Step 1/9: Xcode Command Line Tools"
if ! xcode-select -p >/dev/null 2>&1; then
    warn "Xcode CLT not found. Triggering installer GUI — accept the prompt,"
    warn "wait for it to finish, then re-run this script."
    xcode-select --install || true
    die "Re-run scripts/install-mac.sh after Xcode CLT install completes."
fi
log "Xcode CLT: $(xcode-select -p)"

# ---------------------------------------------------------------------
# 2. Homebrew
# ---------------------------------------------------------------------
log "Step 2/9: Homebrew"
if ! command -v brew >/dev/null 2>&1; then
    # Official non-interactive installer.
    NONINTERACTIVE=1 /bin/bash -c \
        "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
fi

# Make brew usable in this shell + future logins.
if [[ -x "$BREW_PREFIX_DEFAULT/bin/brew" ]]; then
    eval "$("$BREW_PREFIX_DEFAULT/bin/brew" shellenv)"
elif command -v brew >/dev/null 2>&1; then
    eval "$(brew shellenv)"
else
    die "brew install appeared to succeed but 'brew' is still not on PATH."
fi

BREW_LINE="eval \"\$(\"$(command -v brew)\" shellenv)\""
if [[ -f "$SHELL_RC" ]] && grep -Fq "$BREW_LINE" "$SHELL_RC"; then
    :
else
    printf '\n# Added by StockAnalyser/scripts/install-mac.sh\n%s\n' \
        "$BREW_LINE" >> "$SHELL_RC"
    log "Appended brew shellenv to $SHELL_RC"
fi

brew --version | head -1

# ---------------------------------------------------------------------
# 3. brew packages: python@3.11, node@22, uv, git, sqlite
# ---------------------------------------------------------------------
log "Step 3/9: brew packages"
brew update
brew install python@3.11 node@22 uv git sqlite

# node@22 is keg-only; need to link or front the PATH.
NODE_PREFIX="$(brew --prefix node@22)"
if ! command -v node >/dev/null 2>&1 || [[ "$(node -v | sed 's/v//;s/\..*//')" != "22" ]]; then
    brew link --overwrite --force node@22 || true
    export PATH="$NODE_PREFIX/bin:$PATH"
    NODE_LINE="export PATH=\"$NODE_PREFIX/bin:\$PATH\""
    if ! grep -Fq "$NODE_LINE" "$SHELL_RC" 2>/dev/null; then
        printf '%s\n' "$NODE_LINE" >> "$SHELL_RC"
        log "Appended node@22 PATH to $SHELL_RC"
    fi
fi

# python@3.11 is keg-only too; address it via brew --prefix explicitly.
PY311_PREFIX="$(brew --prefix python@3.11)"
PYTHON_BIN="$PY311_PREFIX/bin/python3.11"
if [[ ! -x "$PYTHON_BIN" ]]; then
    die "python@3.11 not installed correctly; expected $PYTHON_BIN"
fi
log "Python: $PYTHON_BIN ($($PYTHON_BIN --version))"
log "Node:   $(node -v) / npm: $(npm -v)"
log "uv:     $(uv --version)"

# ---------------------------------------------------------------------
# 4. Python venv + requirements (sans graphiti/neo4j)
# ---------------------------------------------------------------------
log "Step 4/9: .venv + Python deps"
if [[ ! -d .venv ]]; then
    "$PYTHON_BIN" -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate

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
# 5. Web frontend
# ---------------------------------------------------------------------
if [[ "$SKIP_NPM" == "0" ]]; then
    log "Step 5/9: apps/dsa-web npm ci"
    (cd apps/dsa-web && npm ci)
else
    warn "Step 5/9: skipped (SKIP_NPM=1)"
fi

if [[ "$SKIP_DESKTOP" == "0" ]]; then
    log "       apps/dsa-desktop npm install (optional)"
    (cd apps/dsa-desktop && npm install)
else
    warn "       desktop app skipped (set SKIP_DESKTOP=0 to install)"
fi

# ---------------------------------------------------------------------
# 6. Local data dirs
# ---------------------------------------------------------------------
log "Step 6/9: data/ logs/ dirs"
mkdir -p data logs Sequoia-X/data .cache/candidate_experts_v2

# ---------------------------------------------------------------------
# 7. .env
# ---------------------------------------------------------------------
log "Step 7/9: .env"
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
# 8. Optional: Sequoia candidate DB
# ---------------------------------------------------------------------
log "Step 8/9: Sequoia DB (optional)"
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
# 9. Ensure launcher scripts are executable
# ---------------------------------------------------------------------
log "Step 9/9: chmod launchers"
chmod +x scripts/start-backend.sh scripts/start-web.sh 2>/dev/null || true
chmod +x scripts/start-backend.command scripts/start-web.command 2>/dev/null || true

# ---------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------
log "Bootstrap complete."
cat <<EOF

Next steps:
  1. Edit .env and fill in your API keys (TUSHARE_TOKEN, DEEPSEEK_API_KEY, ...).
  2. Start the backend (either way works):
       source .venv/bin/activate
       bash scripts/start-backend.sh
     — or double-click scripts/start-backend.command in Finder.
  3. In another terminal, start the web dev server:
       bash scripts/start-web.sh
     — or double-click scripts/start-web.command.
  4. Visit http://localhost:5173

Notes:
  - graphiti / Neo4j are intentionally disabled on this machine.
  - If brew / node / python don't show up in a new terminal, run:
       source $SHELL_RC
EOF
