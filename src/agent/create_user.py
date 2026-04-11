#!/usr/bin/env python3
"""Управление пользователями Colloquium-DevSpace из командной строки.

Использует ту же логику хеширования, что agent/managers/users.py (_init_users).

Запуск внутри контейнера:
    python3 /app/agent/create_user.py copilot devspace
    python3 /app/agent/create_user.py --delete copilot

Запуск с хоста (предпочтительно по имени сервиса compose):
    docker compose exec colloquium-core python3 /app/agent/create_user.py copilot devspace
    docker compose exec colloquium-core python3 /app/agent/create_user.py --delete copilot
    # либо docker exec <имя_контейнера> … (по умолчанию cqds-core, см. container_name в docker-compose.yml)
"""

import hashlib
import binascii
import os
import sys
from managers.db import Database


def _db():
    return Database.get_database()


def create_user(username: str, password: str) -> bool:
    salt = os.urandom(16)
    salt_hex = binascii.hexlify(salt).decode()
    server_hash = hashlib.sha256(salt + password.encode()).hexdigest()

    db = _db()
    exists = db.fetch_one("SELECT user_id FROM users WHERE user_name = :user_name", {"user_name": username})
    if exists:
        print(f"WARN: Пользователь '{username}' уже существует, пропуск")
        return False

    db.execute(
        "INSERT INTO users (user_name, password_hash, salt) VALUES (:user_name, :password_hash, :salt)",
        {
            "user_name": username,
            "password_hash": server_hash,
            "salt": salt_hex,
        },
    )
    print(f"OK: Создан пользователь '{username}'")
    return True


def delete_user(username: str) -> bool:
    db = _db()
    result = db.execute("DELETE FROM users WHERE user_name = :user_name", {"user_name": username})
    deleted = result.rowcount
    if deleted:
        print(f"OK: Пользователь '{username}' удалён")
        return True
    else:
        print(f"WARN: Пользователь '{username}' не найден")
        return False


def list_users() -> None:
    db = _db()
    rows = db.fetch_all("SELECT user_id, user_name FROM users ORDER BY user_id")
    if not rows:
        print("Пользователей нет")
        return
    for uid, uname in rows:
        print(f"  [{uid}] {uname}")


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(1)

    if args[0] == "--delete":
        if len(args) < 2:
            print("Usage: create_user.py --delete <username>")
            sys.exit(1)
        delete_user(args[1])

    elif args[0] == "--list":
        list_users()

    else:
        username = args[0]
        password = args[1] if len(args) > 1 else "devspace"
        create_user(username, password)
