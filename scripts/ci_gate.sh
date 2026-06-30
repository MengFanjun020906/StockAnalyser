#!/usr/bin/env bash

set -euo pipefail

PYTHON_BIN="${PYTHON_BIN:-python}"
if [[ "${PYTHON_BIN}" == "python" && -x ".venv/bin/python" ]]; then
  PYTHON_BIN=".venv/bin/python"
fi

syntax_check() {
  echo "==> backend-gate: Python syntax check"
  "$PYTHON_BIN" -m py_compile main.py src/config.py src/auth.py src/analyzer.py src/notification.py
  "$PYTHON_BIN" -m py_compile src/storage.py src/scheduler.py src/search_service.py
  "$PYTHON_BIN" -m py_compile src/market_analyzer.py src/stock_analyzer.py
  "$PYTHON_BIN" -m py_compile data_provider/*.py
}

flake8_checks() {
  echo "==> backend-gate: flake8 critical checks"
  local flake8_cmd=(flake8)
  if "$PYTHON_BIN" -m flake8 --version >/dev/null 2>&1; then
    flake8_cmd=("$PYTHON_BIN" -m flake8)
  fi
  "${flake8_cmd[@]}" . \
      --exclude=.git,.venv,venv,__pycache__,node_modules,static,openInvest,graphiti,Sequoia-X,alphasift \
      --count --select=E9,F63,F7,F82 --show-source --statistics
}

deterministic_checks() {
  echo "==> backend-gate: local deterministic checks"
  ./test.sh code
  ./test.sh yfinance
}

offline_test_suite() {
  echo "==> backend-gate: offline test suite"
  "$PYTHON_BIN" -m pytest -m "not network" \
    --ignore=openInvest --ignore=graphiti --ignore=Sequoia-X --ignore=alphasift
}

run_all() {
  syntax_check
  flake8_checks
  deterministic_checks
  offline_test_suite
  echo "==> backend-gate: all checks passed"
}

phase="${1:-all}"

case "$phase" in
  all)
    run_all
    ;;
  syntax)
    syntax_check
    ;;
  flake8)
    flake8_checks
    ;;
  deterministic)
    deterministic_checks
    ;;
  offline-tests)
    offline_test_suite
    ;;
  *)
    echo "Usage: $0 [all|syntax|flake8|deterministic|offline-tests]" >&2
    exit 2
    ;;
esac
