#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_DIR="$ROOT_DIR/data/run"

stop_process() {
  local name="$1"
  local pid_file="$2"

  if [[ ! -f "$pid_file" ]]; then
    printf '%s is not tracked.\n' "$name"
    return 0
  fi

  local pid
  pid="$(cat "$pid_file" 2>/dev/null || true)"
  if [[ -z "$pid" ]]; then
    rm -f "$pid_file"
    printf '%s pid file was empty and has been removed.\n' "$name"
    return 0
  fi

  if ! kill -0 "$pid" >/dev/null 2>&1; then
    rm -f "$pid_file"
    printf '%s was not running. Removed stale pid file.\n' "$name"
    return 0
  fi

  printf 'Stopping %s, pid=%s ...\n' "$name" "$pid"
  kill "$pid" >/dev/null 2>&1 || true

  for _ in $(seq 1 20); do
    if ! kill -0 "$pid" >/dev/null 2>&1; then
      rm -f "$pid_file"
      printf '%s stopped.\n' "$name"
      return 0
    fi
    sleep 0.5
  done

  printf '%s did not exit, sending SIGKILL ...\n' "$name"
  kill -9 "$pid" >/dev/null 2>&1 || true
  rm -f "$pid_file"
  printf '%s stopped.\n' "$name"
}

stop_process "Frontend" "$RUN_DIR/frontend.pid"
stop_process "Backend" "$RUN_DIR/backend.pid"
