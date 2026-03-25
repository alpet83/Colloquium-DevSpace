#!/bin/sh
set -e

if [ -z "${POSTGRES_ROOT_PASSWORD:-}" ] && [ -n "${POSTGRES_ROOT_PASSWORD_FILE:-}" ] && [ -f "$POSTGRES_ROOT_PASSWORD_FILE" ]; then
    POSTGRES_ROOT_PASSWORD="$(cat "$POSTGRES_ROOT_PASSWORD_FILE")"
fi

if [ -z "${POSTGRES_ROOT_PASSWORD:-}" ]; then
    echo "POSTGRES_ROOT_PASSWORD is not set (and POSTGRES_ROOT_PASSWORD_FILE is missing/empty)"
    exit 1
fi

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
DO
\$\$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'cqds') THEN
        CREATE ROLE cqds LOGIN PASSWORD '${POSTGRES_ROOT_PASSWORD}';
    END IF;
END
\$\$;

SELECT 'CREATE DATABASE cqds OWNER cqds'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'cqds')\gexec

GRANT ALL PRIVILEGES ON DATABASE cqds TO cqds;
EOSQL
