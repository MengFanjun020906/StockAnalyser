#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_DIR="$ROOT_DIR/data/run"
STOP_NEO4J="${STOP_NEO4J:-true}"
DOCKER_COMPOSE_FILE="$ROOT_DIR/docker/docker-compose.yml"

find_docker_compose() {
  if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    DOCKER_COMPOSE_CMD=(docker compose)
  elif command -v docker-compose >/dev/null 2>&1; then
    DOCKER_COMPOSE_CMD=(docker-compose)
  else
    printf 'Docker Compose is not available. Skip stopping Neo4j.\n'
    return 1
  fi
}

ensure_docker_daemon_running() {
  if ! command -v docker >/dev/null 2>&1; then
    return 0
  fi
  if ! docker info >/dev/null 2>&1; then
    printf 'Docker daemon is not running. Skip stopping Neo4j.\n'
    return 1
  fi
}

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

stop_neo4j() {
  if [[ "$STOP_NEO4J" != "true" ]]; then
    printf 'Skipping Neo4j stop because STOP_NEO4J=%s.\n' "$STOP_NEO4J"
    return 0
  fi

  local -a DOCKER_COMPOSE_CMD
  find_docker_compose || return 0
  ensure_docker_daemon_running || return 0
  printf 'Stopping Neo4j container ...\n'
  (
    cd "$ROOT_DIR"
    "${DOCKER_COMPOSE_CMD[@]}" --profile graphiti -f "$DOCKER_COMPOSE_FILE" stop neo4j
  )
}

stop_process "Frontend" "$RUN_DIR/frontend.pid"
stop_process "Backend" "$RUN_DIR/backend.pid"
stop_neo4j
