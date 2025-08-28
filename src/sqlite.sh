#!/bin/sh
docker exec -it colloquium-core sqlite3 /app/data/multichat.db $1 $2 $3