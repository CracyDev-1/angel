#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# Angel Trader — developer mode (hot reload for both backend & frontend).
#
# Starts:
#   - uvicorn with --reload on  http://127.0.0.1:9812  (API)
#   - vite dev server on        http://127.0.0.1:5173  (UI, /api proxied → 9812)
#
# Open http://127.0.0.1:5173 in your browser. Edits to either side reload
# automatically. Ctrl+C stops both.
# -----------------------------------------------------------------------------
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$(pwd)"

PORT="${DASHBOARD_PORT:-9812}"
HOST="${DASHBOARD_HOST:-127.0.0.1}"

cyan()  { printf "\033[36m%s\033[0m\n" "$*"; }
green() { printf "\033[32m%s\033[0m\n" "$*"; }

# ---- Ensure backend env -----------------------------------------------------
if [ ! -x ".venv/bin/python" ]; then
  cyan "→ Creating Python venv (.venv)…"
  python3 -m venv .venv
fi
if [ ! -f ".venv/.bootstrapped" ] || [ "pyproject.toml" -nt ".venv/.bootstrapped" ]; then
  cyan "→ Installing backend dependencies…"
  .venv/bin/pip install --quiet --upgrade pip
  .venv/bin/pip install --quiet -e ".[dev]"
  touch .venv/.bootstrapped
fi

# ---- Ensure frontend deps ---------------------------------------------------
if [ ! -d "frontend/node_modules" ]; then
  cyan "→ Installing frontend dependencies…"
  (cd frontend && yarn install --silent)
fi

# ---- Free the API port ------------------------------------------------------
PIDS=$(lsof -nP -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | awk 'NR>1{print $2}' | sort -u || true)
if [ -n "${PIDS:-}" ]; then
  cyan "→ Port $PORT is in use — stopping PID(s): $PIDS"
  kill -9 $PIDS 2>/dev/null || true
  sleep 1
fi

green "→ Backend (reload):  http://$HOST:$PORT/"
green "→ Frontend (vite):   http://127.0.0.1:5173/"
green "  Edits to src/ and frontend/src/ live-reload. Ctrl+C stops both."

# ---- Run both via npx concurrently -----------------------------------------
# Use yarn workspace at the root so concurrently is local; fall back to npx if
# concurrently isn't installed yet (first run).
if [ ! -d "node_modules/concurrently" ]; then
  cyan "→ Installing concurrently (one-time)…"
  yarn install --silent
fi

export PYTHONPATH="${PYTHONPATH:-}${PYTHONPATH:+:}src"

# `--kill-others-on-fail` ensures that if uvicorn dies, vite dies too, and vice
# versa — no orphan processes after Ctrl+C.
exec ./node_modules/.bin/concurrently \
  --names "API,WEB" \
  --prefix-colors "cyan,magenta" \
  --kill-others-on-fail \
  ".venv/bin/python -m uvicorn angel_bot.dashboard.app:create_app --factory --host $HOST --port $PORT --reload --reload-dir src --log-level info" \
  "yarn --cwd frontend dev --host"
