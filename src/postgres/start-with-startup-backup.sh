#!/bin/sh
set -eu

/usr/local/bin/docker-entrypoint.sh postgres &
pg_pid="$!"

wait_for_postgres() {
  host="${POSTGRES_WAIT_HOST:-127.0.0.1}"
  port="${PGPORT:-5432}"
  user="${POSTGRES_USER:-postgres}"
  db="${POSTGRES_DB:-postgres}"

  i=0
  until pg_isready -h "$host" -p "$port" -U "$user" -d "$db" >/dev/null 2>&1; do
    i=$((i + 1))
    if [ "$i" -ge 90 ]; then
      echo "Startup backup: postgres did not become ready in time" >&2
      return 1
    fi
    sleep 1
  done
}

run_startup_backup() {
  if [ ! -f /opt/cqds-postgres/backup_postgres.sh ]; then
    echo "Startup backup: backup_postgres.sh not found, skipping" >&2
    return 0
  fi

  export PGHOST="${PGHOST:-127.0.0.1}"
  export PGPORT="${PGPORT:-5432}"
  export PGUSER="${PGUSER:-cqds}"
  export PGDATABASE="${PGDATABASE:-cqds}"
  export PGPASSWORD_FILE="${PGPASSWORD_FILE:-/run/secrets/cqds_db_password}"
  export BACKUP_DIR="${BACKUP_DIR:-/backups/pg}"
  export RETENTION_DAYS="${RETENTION_DAYS:-14}"

  /bin/sh /opt/cqds-postgres/backup_postgres.sh
}

if wait_for_postgres; then
  if ! run_startup_backup; then
    echo "Startup backup: failed (postgres will keep running)" >&2
  fi
fi

wait "$pg_pid"
