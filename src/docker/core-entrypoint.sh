#!/bin/sh
# colloquium-core entrypoint: ensure apscheduler is present in venv.
set -e
PY=/app/venv/bin/python3
PIP=/app/venv/bin/pip
if ! "$PY" -c "import apscheduler" 2>/dev/null; then
  echo "cqds-core: apscheduler missing in venv, running pip install"
  if [ -f /app/agent/requirements-core.txt ]; then
    "$PIP" install --no-cache-dir -r /app/agent/requirements-core.txt
  else
    "$PIP" install --no-cache-dir "apscheduler>=3.10.4,<4"
  fi
fi
exec "$PY" "$@"
