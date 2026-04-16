#!/usr/bin/env sh
# Обёртка: ротация пароля PostgreSQL + secrets/cqds_db_password (см. rotate_cqds_db_password.py).
set -eu
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname "$0")" && pwd)"
ROOT_DIR="$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT_DIR"
if command -v python3 >/dev/null 2>&1; then
  exec python3 "$SCRIPT_DIR/rotate_cqds_db_password.py" "$@"
fi
if command -v python >/dev/null 2>&1; then
  exec python "$SCRIPT_DIR/rotate_cqds_db_password.py" "$@"
fi
echo "ERROR: need python3 on PATH" >&2
exit 1
