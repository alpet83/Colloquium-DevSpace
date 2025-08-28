#!/bin/sh

chmod +x frontend/*.sh
cd frontend
./init.sh
cd ..

# Построить образы, если нужно
docker compose build

# Запуск всей системы
docker compose up -d
