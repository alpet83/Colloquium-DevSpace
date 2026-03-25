#!/usr/bin/env bash
set -euo pipefail

TARGET_DIR="${TARGET_DIR:-/opt/docker/cqds.test}"
MAIN_REPO="${MAIN_REPO:-$HOME/GitHub/Colloquium-DevSpace}"
SANDWICH_REPO="${SANDWICH_REPO:-$HOME/GitHub/Sandwich-pack}"
NON_INTERACTIVE="${NON_INTERACTIVE:-0}"
STOP_EXISTING="${STOP_EXISTING:-auto}"
GENERATE_PASSWORD="${GENERATE_PASSWORD:-auto}"
DB_PASSWORD="${DB_PASSWORD:-}"
SKIP_BUILD="${SKIP_BUILD:-0}"
SKIP_UP="${SKIP_UP:-0}"
RESTORE_LATEST_BACKUP="${RESTORE_LATEST_BACKUP:-auto}"

log() { printf '[INFO] %s\n' "$*"; }
warn() { printf '[WARN] %s\n' "$*"; }

yes_no() {
  local prompt="$1"
  local default_yes="$2"
  if [[ "$NON_INTERACTIVE" == "1" ]]; then
    [[ "$default_yes" == "1" ]] && return 0 || return 1
  fi
  local suffix="[y/N]"
  [[ "$default_yes" == "1" ]] && suffix="[Y/n]"
  read -r -p "$prompt $suffix " ans
  if [[ -z "$ans" ]]; then
    [[ "$default_yes" == "1" ]] && return 0 || return 1
  fi
  case "${ans,,}" in
    y|yes) return 0 ;;
    *) return 1 ;;
  esac
}

random_password() {
  LC_ALL=C head -c 128 /dev/urandom | tr -dc 'A-Za-z0-9!@#$%^&*()-_=+' | cut -c1-24
}

sync_tree() {
  local source="$1"
  local target="$2"
  mkdir -p "$target"
  if command -v rsync >/dev/null 2>&1; then
    rsync -a --delete "$source/" "$target/"
    return
  fi

  # Fallback for environments without rsync (e.g. Windows Git Bash).
  rm -rf "$target"/* "$target"/.[!.]* "$target"/..?* 2>/dev/null || true
  cp -a "$source/." "$target/"
}

overlay_tree() {
  local source="$1"
  local target="$2"
  mkdir -p "$target"
  if command -v rsync >/dev/null 2>&1; then
    rsync -a "$source/" "$target/"
    return
  fi
  cp -a "$source/." "$target/"
}

wait_postgres_healthy() {
  local tries=30
  local i
  for i in $(seq 1 "$tries"); do
    local state
    state="$(docker inspect --format '{{.State.Health.Status}}' cqds-postgres 2>/dev/null || true)"
    if [[ "$state" == "healthy" ]]; then
      return 0
    fi
    sleep 2
  done
  echo "PostgreSQL container did not become healthy in time" >&2
  return 1
}

latest_backup_file() {
  local parent target1 target2
  parent="$(dirname "$TARGET_DIR")"
  target1="$TARGET_DIR/data/backups/pg"
  target2="$parent/cqds/data/backups/pg"

  ls -1t "$target1"/*.dump "$target2"/*.dump 2>/dev/null | head -n 1 || true
}

show_admin_password_fragment() {
  echo
  echo "==== colloquium_core.log fragment (admin password) ===="
  local pattern log_file
  pattern='Создан пользователь admin с временным паролем|temporary password|admin.+password'
  log_file="$TARGET_DIR/logs/colloquium_core.log"

  if [[ -f "$log_file" ]]; then
    local match_line
    match_line="$(grep -nE "$pattern" "$log_file" | tail -n 1 | cut -d: -f1 || true)"
    if [[ -n "$match_line" ]]; then
      local start end
      start=$((match_line - 2))
      end=$((match_line + 2))
      if (( start < 1 )); then start=1; fi
      sed -n "${start},${end}p" "$log_file"
      return
    fi
  fi

  warn "Admin temporary password line not found in $log_file"
  echo "Recent colloquium-core logs:"
  docker compose logs --no-color --tail=120 colloquium-core | grep -E "$pattern" -C 2 || true
  echo "If empty, admin user may already exist and password generation was skipped."
}

if [[ ! -d "$MAIN_REPO" ]]; then
  echo "Main repo not found: $MAIN_REPO" >&2
  exit 1
fi
if [[ ! -d "$SANDWICH_REPO" ]]; then
  echo "Sandwich repo not found: $SANDWICH_REPO" >&2
  exit 1
fi

if [[ "$NON_INTERACTIVE" != "1" ]]; then
  read -r -p "Target directory [$TARGET_DIR]: " input_target
  if [[ -n "$input_target" ]]; then
    TARGET_DIR="$input_target"
  fi
fi

mkdir -p "$TARGET_DIR" "$TARGET_DIR/data" "$TARGET_DIR/logs" "$TARGET_DIR/projects" "$TARGET_DIR/secrets"

if [[ "$STOP_EXISTING" == "auto" ]]; then
  if yes_no "Stop and remove currently running CQDS containers to avoid name/port conflicts?" 1; then
    STOP_EXISTING="1"
  else
    STOP_EXISTING="0"
  fi
fi

if [[ "$STOP_EXISTING" == "1" ]]; then
  log "Stopping existing CQDS environment"
  if [[ -f "/opt/docker/cqds/docker-compose.yml" ]]; then
    (cd /opt/docker/cqds && docker compose down --remove-orphans) || warn "compose down failed in /opt/docker/cqds"
  fi
  docker rm -f colloquium-core cqds-postgres mcp-sandbox frontend nginx-router >/dev/null 2>&1 || true
fi

log "Copying Colloquium src files"
sync_tree "$MAIN_REPO/src" "$TARGET_DIR"

log "Copying Sandwich lib"
mkdir -p "$TARGET_DIR/agent"
overlay_tree "$SANDWICH_REPO/src/lib" "$TARGET_DIR/agent/lib"
cp -f "$SANDWICH_REPO/requirements.txt" "$TARGET_DIR/agent/requirements_sandwich.txt"

log "Copying docs near deployment root"
PARENT_DIR="$(dirname "$TARGET_DIR")"
mkdir -p "$PARENT_DIR/docs"
sync_tree "$MAIN_REPO/docs" "$PARENT_DIR/docs"
if [[ -f "$SANDWICH_REPO/README.md" ]]; then
  cp -f "$SANDWICH_REPO/README.md" "$PARENT_DIR/docs/SANDWICH.md"
fi

# Source sync can remove these folders in fallback mode, ensure they exist.
mkdir -p "$TARGET_DIR/data" "$TARGET_DIR/logs" "$TARGET_DIR/projects" "$TARGET_DIR/secrets"

if [[ -z "$DB_PASSWORD" ]]; then
  if [[ "$GENERATE_PASSWORD" == "1" ]]; then
    DB_PASSWORD="$(random_password)"
  elif [[ "$GENERATE_PASSWORD" == "0" ]]; then
    if [[ "$NON_INTERACTIVE" == "1" ]]; then
      echo "DB_PASSWORD is required when GENERATE_PASSWORD=0 in non-interactive mode" >&2
      exit 1
    fi
    while [[ -z "$DB_PASSWORD" ]]; do
      read -r -p "Enter PostgreSQL password (stored in secrets/cqds_db_password): " DB_PASSWORD
    done
  else
    if yes_no "Generate random PostgreSQL root/user password?" 1; then
      DB_PASSWORD="$(random_password)"
    else
      while [[ -z "$DB_PASSWORD" ]]; do
        read -r -p "Enter PostgreSQL password (stored in secrets/cqds_db_password): " DB_PASSWORD
      done
    fi
  fi
fi

printf '%s' "$DB_PASSWORD" > "$TARGET_DIR/secrets/cqds_db_password"
log "Password file created: $TARGET_DIR/secrets/cqds_db_password"

cd "$TARGET_DIR"
export DB_ROOT_PASSWD="$DB_PASSWORD"

if [[ "$SKIP_BUILD" != "1" ]]; then
  log "Building docker images"
  docker compose build
else
  log "Build skipped"
fi

if [[ "$SKIP_UP" != "1" ]]; then
  log "Starting base services (postgres + sandbox)"
  docker compose up -d postgres mcp-sandbox
  wait_postgres_healthy

  log "Reconciling cqds database role/password"
  docker exec -u postgres cqds-postgres psql -d postgres -v ON_ERROR_STOP=1 -c "DO \$\$ BEGIN IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'cqds') THEN CREATE ROLE cqds LOGIN PASSWORD '$DB_PASSWORD'; ELSE ALTER ROLE cqds WITH LOGIN PASSWORD '$DB_PASSWORD'; END IF; END \$\$;"
  db_exists="$(docker exec -u postgres cqds-postgres psql -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname = 'cqds'" | tr -d '[:space:]')"
  if [[ "$db_exists" != "1" ]]; then
    docker exec -u postgres cqds-postgres createdb -O cqds cqds
  fi
  docker exec -u postgres cqds-postgres psql -d postgres -v ON_ERROR_STOP=1 -c "GRANT ALL PRIVILEGES ON DATABASE cqds TO cqds;"

  log "Checking bootstrap schema in cqds database"
  schema_ready="$(docker exec -e PGPASSWORD="$DB_PASSWORD" cqds-postgres sh -lc "psql -U cqds -d cqds -tAc \"SELECT to_regclass('public.users') IS NOT NULL\"" | tr -d '[:space:]')"
  if [[ "$schema_ready" != "t" ]]; then
    log "users table is missing, importing prototype schema"
    docker exec -e PGPASSWORD="$DB_PASSWORD" cqds-postgres sh -lc "psql -v ON_ERROR_STOP=1 -U cqds -d cqds -f /docker-entrypoint-initdb.d/02-cqds-schema.sql"
  else
    log "Bootstrap schema already exists"
  fi

  backup_file="$(latest_backup_file)"
  if [[ -n "$backup_file" ]]; then
    do_restore=0
    if [[ "$RESTORE_LATEST_BACKUP" == "1" ]]; then
      do_restore=1
    elif [[ "$RESTORE_LATEST_BACKUP" == "auto" ]] && [[ "$NON_INTERACTIVE" != "1" ]]; then
      if yes_no "Backup found ($backup_file). Restore it now?" 0; then
        do_restore=1
      fi
    fi

    if [[ "$do_restore" == "1" ]]; then
      log "Restoring backup: $backup_file"
      backup_dir="$(dirname "$backup_file")"
      backup_name="$(basename "$backup_file")"
      backup_mount_dir="$backup_dir"
      if command -v cygpath >/dev/null 2>&1; then
        backup_mount_dir="$(cygpath -w "$backup_dir")"
      fi
      docker run --rm --network "container:cqds-postgres" -e PGPASSWORD="$DB_PASSWORD" -v "$backup_mount_dir:/backups" postgres:17-alpine \
        sh -lc "pg_restore --clean --if-exists --no-owner --no-privileges -h 127.0.0.1 -U cqds -d cqds /backups/$backup_name || true"
    else
      log "Backup restore skipped"
    fi
  else
    log "No backup dumps found, continuing with bootstrap schema only"
  fi

  log "Starting app services"
  docker compose up -d colloquium-core frontend nginx-router
  sleep 5
  docker compose ps
  show_admin_password_fragment
else
  log "Startup skipped"
fi

log "Deployment completed: $TARGET_DIR"
