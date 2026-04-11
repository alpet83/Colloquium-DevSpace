#!/bin/sh
# Имя контейнера ядра: ${COMPOSE_PROJECT_NAME:-cqds}-core (или CQDS_CORE_CONTAINER).
_proj="${COMPOSE_PROJECT_NAME:-cqds}"
_core="${CQDS_CORE_CONTAINER:-${_proj}-core}"
docker exec -it "${_core}" sqlite3 /app/data/multichat.db $1 $2 $3