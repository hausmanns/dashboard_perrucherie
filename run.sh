#!/usr/bin/env bash
# Launch the Dashboard de la Perrucherie on the local network.
set -euo pipefail
cd "$(dirname "$0")"

PYTHON_BIN="${PYTHON_BIN:-python3}"

if [ ! -d ".venv" ]; then
  echo "==> Creating virtual environment (.venv)..."
  "$PYTHON_BIN" -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate

echo "==> Installing dependencies..."
pip install -q -r requirements.txt

PORT="${PORT:-8000}"

IP=$(ipconfig getifaddr en0 2>/dev/null || hostname -I 2>/dev/null | awk '{print $1}' || echo "localhost")

echo ""
echo "  🦜 Dashboard de la Perrucherie"
echo "  -------------------------------------------"
echo "  Local:   http://localhost:${PORT}"
echo "  Réseau:  http://${IP}:${PORT}   <-- ouvrez ceci depuis vos autres appareils"
echo "  API doc: http://${IP}:${PORT}/docs"
echo ""

exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT}"
