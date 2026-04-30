#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# Angel Trader — single-command bootstrap.
#
# This script:
#   1. Creates the Python venv if missing and installs backend deps
#   2. Installs frontend deps if missing
#   3. Builds the React frontend into frontend/dist
#   4. Frees port DASHBOARD_PORT (default 9812)
#   5. Starts uvicorn (FastAPI dashboard) which serves BOTH the API and the
#      built frontend from the same origin → http://127.0.0.1:9812/
#
# Use `yarn start`. Override the port with `DASHBOARD_PORT=9000 yarn start`.
# -----------------------------------------------------------------------------
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$(pwd)"

PORT="${DASHBOARD_PORT:-9812}"
HOST="${DASHBOARD_HOST:-127.0.0.1}"

cyan()  { printf "\033[36m%s\033[0m\n" "$*"; }
green() { printf "\033[32m%s\033[0m\n" "$*"; }
red()   { printf "\033[31m%s\033[0m\n" "$*"; }

# ---- 1. Python venv + backend deps -----------------------------------------
if [ ! -x ".venv/bin/python" ]; then
  cyan "→ Creating Python venv (.venv)…"
  python3 -m venv .venv
fi

# Idempotent: pip install -e is fast when nothing changed.
if [ ! -f ".venv/.bootstrapped" ] || [ "pyproject.toml" -nt ".venv/.bootstrapped" ]; then
  cyan "→ Installing backend dependencies…"
  .venv/bin/pip install --quiet --upgrade pip
  .venv/bin/pip install --quiet -e ".[dev]"
  touch .venv/.bootstrapped
fi

# ---- 2. Frontend deps -------------------------------------------------------
if [ ! -d "frontend/node_modules" ]; then
  cyan "→ Installing frontend dependencies (yarn install)…"
  (cd frontend && yarn install --silent)
fi

# ---- 3. Build frontend ------------------------------------------------------
# Skip the rebuild when nothing under frontend/src changed since last build.
NEEDS_BUILD=1
if [ -f "frontend/dist/index.html" ]; then
  if [ -z "$(find frontend/src frontend/index.html frontend/vite.config.ts frontend/tailwind.config.js frontend/postcss.config.js -newer frontend/dist/index.html 2>/dev/null | head -n 1)" ]; then
    NEEDS_BUILD=0
  fi
fi
if [ "$NEEDS_BUILD" = "1" ]; then
  cyan "→ Building frontend (vite build)…"
  (cd frontend && yarn build)
else
  cyan "→ Frontend dist is up to date — skipping rebuild."
fi

# ---- 4. Free the port -------------------------------------------------------
PIDS=$(lsof -nP -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | awk 'NR>1{print $2}' | sort -u || true)
if [ -n "${PIDS:-}" ]; then
  cyan "→ Port $PORT is in use by PID(s): $PIDS — stopping them."
  kill -9 $PIDS 2>/dev/null || true
  sleep 1
fi

# ---- 5. Run uvicorn (foreground, Ctrl+C to stop) ---------------------------
green "→ Starting Angel Trader on http://$HOST:$PORT/  (Ctrl+C to stop)"
export PYTHONPATH="${PYTHONPATH:-}${PYTHONPATH:+:}src"
exec .venv/bin/python -m uvicorn angel_bot.dashboard.app:create_app \
  --factory \
  --host "$HOST" \
  --port "$PORT" \
  --log-level info
