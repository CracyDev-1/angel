#!/usr/bin/env bash
# Kill any uvicorn / vite still bound to the project's ports.
set -euo pipefail

PORTS=("${DASHBOARD_PORT:-9812}" "5173")
for P in "${PORTS[@]}"; do
  PIDS=$(lsof -nP -iTCP:"$P" -sTCP:LISTEN 2>/dev/null | awk 'NR>1{print $2}' | sort -u || true)
  if [ -n "${PIDS:-}" ]; then
    printf "Killing PID(s) on port %s: %s\n" "$P" "$PIDS"
    kill -9 $PIDS 2>/dev/null || true
  else
    printf "Port %s is free.\n" "$P"
  fi
done
