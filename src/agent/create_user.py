#!/usr/bin/env python3
"""Управление пользователями Colloquium-DevSpace из командной строки.

Использует ту же логику хеширования, что agent/managers/users.py (_init_users).

Запуск внутри контейнера:
    python3 /app/agent/create_user.py copilot devspace
    python3 /app/agent/create_user.py --delete copilot

Запуск через docker exec (из PowerShell на хосте):
    docker exec colloquium-core python3 /app/agent/create_user.py copilot devspace
    docker exec colloquium-core python3 /app/agent/create_user.py --delete copilot
"""

import hashlib
import binascii
import os
import sys
import sqlite3

CHAT_DB = "/app/data/multichat.db"


def create_user(username: str, password: str, db_path: str = CHAT_DB) -> bool:
    salt = os.urandom(16)
    salt_hex = binascii.hexlify(salt).decode()
    server_hash = hashlib.sha256(salt + password.encode()).hexdigest()

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM users WHERE user_name = ?", (username,))
    if cur.fetchone():
        print(f"WARN: Пользователь '{username}' уже существует, пропуск")
        conn.close()
        return False
    cur.execute(
        "INSERT INTO users (user_name, password_hash, salt) VALUES (?, ?, ?)",
        (username, server_hash, salt_hex),
    )
    conn.commit()
    print(f"OK: Создан пользователь '{username}'")
    conn.close()
    return True


def delete_user(username: str, db_path: str = CHAT_DB) -> bool:
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("DELETE FROM users WHERE user_name = ?", (username,))
    deleted = cur.rowcount
    conn.commit()
    conn.close()
    if deleted:
        print(f"OK: Пользователь '{username}' удалён")
        return True
    else:
        print(f"WARN: Пользователь '{username}' не найден")
        return False


def list_users(db_path: str = CHAT_DB) -> None:
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("SELECT user_id, user_name FROM users ORDER BY user_id")
    rows = cur.fetchall()
    conn.close()
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
