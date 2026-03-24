#!/usr/bin/env python3
"""Сделать чат видимым для всех пользователей (user_list='all').

Использование:
    python3 /app/agent/set_chat_public.py <chat_id|chat_description>
    python3 /app/agent/set_chat_public.py --list
"""
import sqlite3, sys

CHAT_DB = "/app/data/multichat.db"


def list_chats(db_path=CHAT_DB):
    c = sqlite3.connect(db_path)
    rows = c.execute("SELECT chat_id, chat_description, user_list FROM chats ORDER BY chat_id").fetchall()
    c.close()
    for cid, desc, ul in rows:
        print(f"  [{cid}] {desc!r:30s}  user_list={ul!r}")


def set_public(key, db_path=CHAT_DB):
    c = sqlite3.connect(db_path)
    # пробуем по числовому id, иначе по описанию
    try:
        cid = int(key)
        c.execute("UPDATE chats SET user_list='all' WHERE chat_id=?", (cid,))
    except ValueError:
        c.execute("UPDATE chats SET user_list='all' WHERE chat_description=?", (key,))
    affected = c.execute("SELECT changes()").fetchone()[0]
    c.commit()
    c.close()
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
