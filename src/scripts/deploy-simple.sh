#!/bin/sh
set -eu

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname "$0")" && pwd)"
ROOT_DIR="$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT_DIR"

COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.yml}"
DEPLOY_ENV_FILE="${DEPLOY_ENV_FILE:-.env}"

log() {
  printf '%s\n' "$*"
}

fail() {
  log "#ERROR: $*"
  exit 1
}

generate_password() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -base64 36 | tr -dc 'A-Za-z0-9' | head -c 28
    return
  fi

  if [ -r /dev/urandom ]; then
    tr -dc 'A-Za-z0-9' </dev/urandom | head -c 28
    return
  fi

  date +%s | tr -dc '0-9'
}

generate_mcp_token() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 24
    return
  fi
  printf '%s%s' "$(generate_password)" "$(generate_password)"
}

set_env_value() {
  file="$1"
  key="$2"
  value="$3"

  if [ ! -f "$file" ]; then
    : >"$file"
  fi

  if grep -q "^${key}=" "$file"; then
    escaped_value="$(printf '%s' "$value" | sed 's/[\\/&]/\\\\&/g')"
    sed -i "s|^${key}=.*$|${key}=${escaped_value}|" "$file"
  else
    printf '%s=%s\n' "$key" "$value" >>"$file"
  fi
}

ensure_cqds_db_password_file() {
  secrets_dir="$ROOT_DIR/secrets"
  f="$secrets_dir/cqds_db_password"
  mkdir -p "$secrets_dir"
  if [ -s "$f" ]; then
    log "#INFO: $f already present, leaving unchanged"
    return 0
  fi
  pw="$(generate_password)"
  (
    umask 077
    printf '%s' "$pw" >"$f"
  )
  chmod 600 "$f" 2>/dev/null || true
  log "#INFO: created $f (random password for PostgreSQL init and role cqds)"
}

ensure_mcp_auth_token_envfile() {
  if [ -n "${MCP_AUTH_TOKEN:-}" ]; then
    log "#INFO: MCP_AUTH_TOKEN is set in the environment, skip $DEPLOY_ENV_FILE"
    return 0
  fi
  if [ -f "$DEPLOY_ENV_FILE" ] && grep -q '^MCP_AUTH_TOKEN=.' "$DEPLOY_ENV_FILE" 2>/dev/null; then
    log "#INFO: $DEPLOY_ENV_FILE already defines MCP_AUTH_TOKEN"
    return 0
  fi
  tok="$(generate_mcp_token)"
  set_env_value "$DEPLOY_ENV_FILE" "MCP_AUTH_TOKEN" "$tok"
  log "#INFO: wrote MCP_AUTH_TOKEN to $DEPLOY_ENV_FILE (required by docker-compose.yml)"
}

compose_cmd() {
  if docker compose version >/dev/null 2>&1; then
    echo "docker compose"
    return
  fi
  fail "need Docker Compose v2 (docker compose)"
}

main() {
  command -v docker >/dev/null 2>&1 || fail "docker not found"

  log "#STEP 1/3: secrets (PostgreSQL file + MCP token for compose)"
  ensure_cqds_db_password_file
  ensure_mcp_auth_token_envfile

  dc="$(compose_cmd)"
  log "#STEP 2/3: build ($COMPOSE_FILE)"
  $dc -f "$COMPOSE_FILE" build

  log "#STEP 3/3: up -d"
  $dc -f "$COMPOSE_FILE" up -d

  log "#SUCCESS: stack started (see docker compose ps)"
}

main "$@"
