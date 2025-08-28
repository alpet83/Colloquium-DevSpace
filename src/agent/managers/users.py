# /agent/managers/users.py, updated 2025-07-26 10:04 EEST
import hashlib
import binascii
import os
from .db import Database, DataTable
from lib.basic_logger import BasicLogger
import globals as g
import secrets, string

log = g.get_logger("userman")

class UserManager:
    def __init__(self):
        self.db = Database.get_database()
        self.users_table = DataTable(
            table_name="users",
            template=[
                "user_id INTEGER PRIMARY KEY AUTOINCREMENT",
                "user_name TEXT",
                "llm_class TEXT",
                "llm_token TEXT",
                "tokens_limit INTEGER DEFAULT 131072",
                "tokens_cost FLOAT",  # Стоимость за 1 млн. токенов
                "password_hash TEXT",
                "salt TEXT"
            ]
        )
        self._init_users()

    def _init_users(self):
        count = self.users_table.select_row(
            columns=["COUNT(*)"],
            conditions={"user_name": "admin"}
        )[0]
        if count == 0:
            salt = os.urandom(16)
            alphabet = string.ascii_letters + string.digits
            password = "!" + ''.join(secrets.choice(alphabet) for i in range(10))
            server_hash = hashlib.sha256(salt + password.encode()).hexdigest()
            salt_hex = binascii.hexlify(salt).decode()
            self.users_table.insert_into({
                "user_name": "admin",
                "llm_class": None,
                "llm_token": None,
                "password_hash": server_hash,
                "salt": salt_hex
            })
            log.warn("Создан пользователь admin с временным паролем %s", password)
        count = self.users_table.select_row(
            columns=["COUNT(*)"],
            conditions={"user_name": "agent"}
        )[0]
        if count == 0:
            self.users_table.insert_into({
                "user_name": "agent",
                "llm_class": None,
                "llm_token": None,
                "password_hash": None,
                "salt": None
            })
            log.info("Создан системный пользователь %s", "agent")

    def check_auth(self, username, password):
        row = self.users_table.select_row(
            columns=["user_id", "password_hash", "salt"],
            conditions={"user_name": username}
        )
        if not row:
            log.info("Неверное имя пользователя: %s", username)
            return None
        user_id, stored_hash, salt_hex = row
        salt = binascii.unhexlify(salt_hex)
        server_hash = hashlib.sha256(salt + password.encode()).hexdigest()
        if server_hash != stored_hash:
            log.info("Неверный пароль для username=%s", username)
            return None
        return user_id

    def get_user_name(self, user_id):
        row = self.users_table.select_row(
            columns=["user_name"],
            conditions={"user_id": user_id}
        )
        return row[0] if row else "Unknown"

    def get_user_role(self, user_id):
        row = self.users_table.select_row(
            columns=["user_name"],
            conditions={"user_id": user_id}
        )
        if not row:
            return None
        username = row[0]
        if username == "admin":
            return "admin"
        elif username == "agent":
            return "mcp"
        elif username == "grok":
            return "assistant"
        return "developer"

    def get_user_id_by_name(self, user_name):
        row = self.users_table.select_row(
            columns=["user_id"],
            conditions={"user_name": user_name}
        )
        return row[0] if row else None

    def is_llm_user(self, user_id):
        row = self.users_table.select_row(
            columns=["llm_class"],
            conditions={"user_id": user_id}
        )
        return row[0] is not None

    def get_user_token_limits(self, user_id):
        row = self.users_table.select_row(
            columns=["tokens_limit", "tokens_cost"],
            conditions={"user_id": user_id}
        )
        if not row:
            log.warn("Пользователь user_id=%d не найден, используются значения по умолчанию", user_id)
            return 131072, 0.0
        tokens_limit, tokens_cost = row
        return tokens_limit or 131072, tokens_cost or 0.0