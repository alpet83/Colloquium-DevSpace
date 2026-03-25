#!/bin/sh
set -eu

TS="$(date +%Y%m%d_%H%M%S)"
BACKUP_DIR="${BACKUP_DIR:-/app/data/backups/pg}"
DB_HOST="${PGHOST:-postgres}"
DB_PORT="${PGPORT:-5432}"
DB_NAME="${PGDATABASE:-cqds}"
DB_USER="${PGUSER:-cqds}"

if [ -z "${PGPASSWORD:-}" ] && [ -n "${PGPASSWORD_FILE:-}" ] && [ -f "$PGPASSWORD_FILE" ]; then
  export PGPASSWORD="$(cat "$PGPASSWORD_FILE")"
fi

if [ -z "${PGPASSWORD:-}" ]; then
  echo "PGPASSWORD is empty. Set PGPASSWORD or mount PGPASSWORD_FILE (default /run/secrets/cqds_db_password)." >&2
  exit 1
fi

mkdir -p "$BACKUP_DIR"
OUT_FILE="$BACKUP_DIR/${DB_NAME}_${TS}.dump"

pg_dump \
  --host "$DB_HOST" \
  --port "$DB_PORT" \
  --username "$DB_USER" \
  --dbname "$DB_NAME" \
  --format=custom \
  --no-owner \
  --no-privileges \
  --file "$OUT_FILE"

# optional retention, days
if [ -n "${RETENTION_DAYS:-}" ]; then
  find "$BACKUP_DIR" -type f -name "${DB_NAME}_*.dump" -mtime "+$RETENTION_DAYS" -delete
fi

echo "Backup created: $OUT_FILE"
pg_restore --list "$OUT_FILE" >/dev/null

echo "Backup verified: pg_restore --list OK"
