#!/usr/bin/env bash
# FIB Hybrid Bot — one-command launcher.
set -e
cd "$(dirname "$0")"

PYBIN="${PYTHON:-python3}"

if [ ! -d ".venv" ]; then
  echo "→ membuat virtualenv…"
  "$PYBIN" -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate

echo "→ menginstal dependensi…"
pip install -q --upgrade pip
pip install -q -r requirements.txt

PORT="${PORT:-8000}"
echo "→ menjalankan bot di http://localhost:${PORT}"
exec uvicorn backend.main:app --host 0.0.0.0 --port "${PORT}"
