#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_DIR="$ROOT_DIR/data/run"
LOG_DIR="$ROOT_DIR/logs/dev"

BACKEND_HOST="${BACKEND_HOST:-0.0.0.0}"
BACKEND_PORT="${BACKEND_PORT:-8000}"
FRONTEND_HOST="${FRONTEND_HOST:-0.0.0.0}"
FRONTEND_PORT="${FRONTEND_PORT:-5173}"

BACKEND_PID_FILE="$RUN_DIR/backend.pid"
FRONTEND_PID_FILE="$RUN_DIR/frontend.pid"
BACKEND_LOG="$LOG_DIR/backend.log"
FRONTEND_LOG="$LOG_DIR/frontend.log"

mkdir -p "$RUN_DIR" "$LOG_DIR"

find_python() {
  if [[ -x "$ROOT_DIR/.venv/bin/python" ]]; then
    printf '%s\n' "$ROOT_DIR/.venv/bin/python"
  elif command -v python3 >/dev/null 2>&1; then
    command -v python3
  else
    command -v python
  fi
}

is_pid_running() {
  local pid_file="$1"
  [[ -f "$pid_file" ]] || return 1
  local pid
  pid="$(cat "$pid_file" 2>/dev/null || true)"
  [[ -n "$pid" ]] || return 1
  kill -0 "$pid" >/dev/null 2>&1
}

is_port_busy() {
  local port="$1"
  if command -v lsof >/dev/null 2>&1; then
    lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1
  else
    return 1
  fi
}

wait_for_url() {
  local name="$1"
  local url="$2"
  local attempts="${3:-30}"
  if ! command -v curl >/dev/null 2>&1; then
    return 0
  fi
  for _ in $(seq 1 "$attempts"); do
    if curl -fsS "$url" >/dev/null 2>&1; then
      printf '%s is ready: %s\n' "$name" "$url"
      return 0
    fi
    sleep 1
  done
  printf '%s did not become ready in time: %s\n' "$name" "$url"
  return 1
}

ensure_tracked_process_running() {
  local name="$1"
  local pid_file="$2"
  local log_file="$3"

  if is_pid_running "$pid_file"; then
    return 0
  fi

  printf '%s process is not running. Check log: %s\n' "$name" "$log_file"
  if [[ -f "$log_file" ]]; then
    tail -40 "$log_file" || true
  fi
  return 1
}

start_backend() {
  if is_pid_running "$BACKEND_PID_FILE"; then
    printf 'Backend already running, pid=%s\n' "$(cat "$BACKEND_PID_FILE")"
    return 0
  fi
  if is_port_busy "$BACKEND_PORT"; then
    printf 'Backend port %s is already in use. Skip starting backend.\n' "$BACKEND_PORT"
    return 0
  fi

  local python_bin
  python_bin="$(find_python)"
  printf 'Starting backend on http://127.0.0.1:%s ...\n' "$BACKEND_PORT"
  (
    cd "$ROOT_DIR"
    nohup env \
      AGENT_MODE="${AGENT_MODE:-true}" \
      AGENT_ANALYSIS_MODE="${AGENT_ANALYSIS_MODE:-planning_execute}" \
      "$python_bin" main.py --serve-only --host "$BACKEND_HOST" --port "$BACKEND_PORT" \
      >"$BACKEND_LOG" 2>&1 &
    printf '%s\n' "$!" > "$BACKEND_PID_FILE"
  )
}

start_frontend() {
  if is_pid_running "$FRONTEND_PID_FILE"; then
    printf 'Frontend already running, pid=%s\n' "$(cat "$FRONTEND_PID_FILE")"
    return 0
  fi
  if is_port_busy "$FRONTEND_PORT"; then
    printf 'Frontend port %s is already in use. Skip starting frontend.\n' "$FRONTEND_PORT"
    return 0
  fi
  if [[ ! -d "$ROOT_DIR/apps/dsa-web/node_modules" ]]; then
    printf 'Missing apps/dsa-web/node_modules. Run npm install in apps/dsa-web first.\n'
    return 1
  fi
  if [[ ! -x "$ROOT_DIR/apps/dsa-web/node_modules/.bin/vite" ]]; then
    printf 'Missing apps/dsa-web/node_modules/.bin/vite. Run npm install in apps/dsa-web first.\n'
    return 1
  fi

  printf 'Starting frontend on http://127.0.0.1:%s ...\n' "$FRONTEND_PORT"
  (
    cd "$ROOT_DIR/apps/dsa-web"
    nohup ./node_modules/.bin/vite --host "$FRONTEND_HOST" --port "$FRONTEND_PORT" >"$FRONTEND_LOG" 2>&1 &
    printf '%s\n' "$!" > "$FRONTEND_PID_FILE"
  )
}

start_backend
start_frontend

ensure_tracked_process_running "Backend" "$BACKEND_PID_FILE" "$BACKEND_LOG"
ensure_tracked_process_running "Frontend" "$FRONTEND_PID_FILE" "$FRONTEND_LOG"
wait_for_url "Backend" "http://127.0.0.1:$BACKEND_PORT/api/health" 45
wait_for_url "Frontend" "http://127.0.0.1:$FRONTEND_PORT" 45
ensure_tracked_process_running "Backend" "$BACKEND_PID_FILE" "$BACKEND_LOG"
ensure_tracked_process_running "Frontend" "$FRONTEND_PID_FILE" "$FRONTEND_LOG"

printf '\nStarted services:\n'
printf '  Backend:  http://127.0.0.1:%s  log=%s\n' "$BACKEND_PORT" "$BACKEND_LOG"
printf '  Frontend: http://127.0.0.1:%s  log=%s\n' "$FRONTEND_PORT" "$FRONTEND_LOG"
printf '\nStop with: ./stop_all.sh\n'
