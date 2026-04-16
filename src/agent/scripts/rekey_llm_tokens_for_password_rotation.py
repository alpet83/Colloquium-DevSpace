#!/usr/bin/env python3
"""
Два JSON-запроса на stdin (по одному запуску процесса на фазу):

Фаза pull:
  {"phase": "pull", "old_pw": "<текущий пароль cqds из файла>"}

Ответ в stdout (JSON): {"ok": true, "rows": [{"user_id": int, "plain": str|null}, ...]}
plain — расшифрованный API-ключ или исходная строка, если не enc:v1.

Фаза push:
  {"phase": "push", "new_pw": "<новый пароль БД>", "rows": [ ... как от pull ]}

Подключается к postgres:5432 как cqds. Запускать внутри контейнера colloquium-core
(PYTHONPATH=/app/agent, зависимости из образа).
"""
from __future__ import annotations

import json
import sys

import psycopg2

from lib.token_crypto import decrypt_token_with_secret, encrypt_token_with_secret, is_encrypted_token


def _connect(password: str):
    return psycopg2.connect(
        host="postgres",
        port=5432,
        user="cqds",
        password=password,
        dbname="cqds",
    )


def _pull(old_pw: str) -> dict:
    conn = _connect(old_pw)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT user_id, llm_token
            FROM users
            WHERE llm_token IS NOT NULL AND btrim(llm_token) <> ''
            """
        )
        rows_out: list[dict] = []
        for user_id, tok in cur.fetchall():
            raw = (tok or "").strip()
            if not raw:
                continue
            try:
                if is_encrypted_token(raw):
                    plain = decrypt_token_with_secret(raw, old_pw)
                else:
                    plain = raw
            except ValueError as e:
                return {"ok": False, "error": str(e), "user_id": int(user_id)}
            rows_out.append({"user_id": int(user_id), "plain": plain})
        cur.close()
        return {"ok": True, "rows": rows_out}
    finally:
        conn.close()


def _push(new_pw: str, rows: list) -> dict:
    conn = _connect(new_pw)
    try:
        conn.autocommit = False
        cur = conn.cursor()
        for item in rows:
            uid = int(item["user_id"])
            plain = item.get("plain")
            if plain is None or plain == "":
                enc = None
            else:
                enc = encrypt_token_with_secret(str(plain), new_pw)
            cur.execute(
                "UPDATE users SET llm_token = %s WHERE user_id = %s",
                (enc, uid),
            )
        cur.close()
        conn.commit()
        return {"ok": True, "updated": len(rows)}
    except Exception as e:
        conn.rollback()
        return {"ok": False, "error": str(e)}
    finally:
        conn.close()


def main() -> int:
    try:
        cfg = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        print(json.dumps({"ok": False, "error": f"stdin JSON: {e}"}))
        return 1

    phase = cfg.get("phase")
    if phase == "pull":
        old_pw = cfg.get("old_pw")
        if not old_pw or not isinstance(old_pw, str):
            print(json.dumps({"ok": False, "error": "missing old_pw"}))
            return 1
        out = _pull(old_pw.strip())
        print(json.dumps(out))
        return 0 if out.get("ok") else 1

    if phase == "push":
        new_pw = cfg.get("new_pw")
        rows = cfg.get("rows")
        if not new_pw or not isinstance(new_pw, str):
            print(json.dumps({"ok": False, "error": "missing new_pw"}))
            return 1
        if not isinstance(rows, list):
            print(json.dumps({"ok": False, "error": "rows must be a list"}))
            return 1
        out = _push(new_pw.strip(), rows)
        print(json.dumps(out))
        return 0 if out.get("ok") else 1

    print(json.dumps({"ok": False, "error": "unknown phase"}))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
