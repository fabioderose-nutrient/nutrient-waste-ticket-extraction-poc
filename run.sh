#!/usr/bin/env bash
# Starts the waste ticket reader locally on http://localhost:${PORT:-8080}.
set -euo pipefail
cd "$(dirname "$0")"
[ -f .env ] || { echo "Missing .env: run cp .env.example .env first (empty keys = evaluation mode)" >&2; exit 1; }
if [ ! -d .venv ]; then
  python3 -m venv .venv
  ./.venv/bin/pip install --quiet --upgrade pip
  ./.venv/bin/pip install --quiet -r backend/requirements.txt
fi
set -a; . ./.env; set +a
export PORT="${PORT:-8080}"
echo "Waste ticket reader on http://localhost:${PORT}"
cd backend
exec ../.venv/bin/uvicorn app:app --host 127.0.0.1 --port "${PORT}"
