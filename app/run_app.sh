#!/usr/bin/env bash
# Start the CADSmith web application.
#
#   ./app/run_app.sh              # serve on http://127.0.0.1:8000
#   PORT=9000 ./app/run_app.sh    # somewhere else
#   ./app/run_app.sh --reload     # reload on source changes, for development
#
# Expects a virtualenv at .venv in the repository root; see app/README.md.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PORT="${PORT:-8000}"
HOST="${HOST:-127.0.0.1}"
PYTHON="${PYTHON:-$ROOT/.venv/bin/python}"

if [ ! -x "$PYTHON" ]; then
  echo "No interpreter at $PYTHON" >&2
  echo "Create one with:" >&2
  echo "  python3 -m venv .venv && .venv/bin/pip install -r app/requirements-app.txt" >&2
  exit 1
fi

if ! "$PYTHON" -c "import cadquery" 2>/dev/null; then
  echo "CadQuery is not installed in $PYTHON" >&2
  echo "  .venv/bin/pip install -r app/requirements-app.txt" >&2
  exit 1
fi

# Load ANTHROPIC_API_KEY (and anything else) from .env if present. The agents
# read it through python-dotenv too, but exporting it here means the health
# check reports the truth before the first run starts.
if [ -f "$ROOT/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  . "$ROOT/.env"
  set +a
fi

echo "CADSmith → http://${HOST}:${PORT}"
exec "$PYTHON" -m uvicorn app.server.app:app \
  --host "$HOST" --port "$PORT" --log-level info "$@"
