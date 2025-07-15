#!/bin/sh
cd /opt/docker/mcp-server

# Построить образы, если нужно
docker compose build

# Запуск всей системы
docker compose up -d

# Опционально: Передача project_name в env (если укажите как $1)
if [ ! -z "$1" ]; then
  docker compose exec mcp-sandbox env PROJECT_NAME="$1" python3 /app/projects/mcp_server.py
fi

echo "Система запущена. Журналы в ./logs, данные в ./data и ./projects."