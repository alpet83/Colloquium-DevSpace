#!/usr/bin/env python3
"""Сделать чат видимым для всех пользователей (user_list='all').

Использование:
    python3 /app/agent/set_chat_public.py <chat_id|chat_description>
    python3 /app/agent/set_chat_public.py --list
"""
import sys
from managers.db import Database


def _db():
    return Database.get_database()


def list_chats():
    rows = _db().fetch_all("SELECT chat_id, chat_description, user_list FROM chats ORDER BY chat_id")
    for cid, desc, ul in rows:
        print(f"  [{cid}] {desc!r:30s}  user_list={ul!r}")


def set_public(key):
    db = _db()
    # пробуем по числовому id, иначе по описанию
    try:
        cid = int(key)
        result = db.execute("UPDATE chats SET user_list = 'all' WHERE chat_id = :chat_id", {"chat_id": cid})
    except ValueError:
        result = db.execute(
            "UPDATE chats SET user_list = 'all' WHERE chat_description = :chat_description",
            {"chat_description": key},
        )
    affected = result.rowcount
    if affected:
        print(f"OK: чат {key!r} теперь public (user_list='all')")
    else:
        print(f"WARN: чат {key!r} не найден")


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args or args[0] == "--list":
        list_chats()
    else:
        set_public(args[0])
