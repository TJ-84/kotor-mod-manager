#!/usr/bin/env bash
# Launch the .2da web editor at http://localhost:8765
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
  python3 -m venv .venv
  ./.venv/bin/pip install -q -r requirements.txt
fi

exec ./.venv/bin/uvicorn app:app \
  --app-dir server \
  --host 127.0.0.1 \
  --port 8765 \
  --reload
