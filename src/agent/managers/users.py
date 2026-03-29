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
                "tokens_input_cost FLOAT",  # Стоимость входящих токенов за 1 млн.
                "tokens_output_cost FLOAT",  # Стоимость выходящих токенов за 1 млн.
                "llm_reasoning_eff TEXT DEFAULT 'medium'",  # low / medium / high / none
                "password_hash TEXT",
                "salt TEXT"
            ]
        )
        self._migrate_llm_tokens()
        self._init_users()

    def _migrate_llm_tokens(self):
        rows = self.users_table.select_from(
            columns=["user_id", "user_name", "llm_token"],
            conditions=[("llm_token", "IS NOT", None)]
        )
        migrated = 0
        for row in rows:
            user_id, user_name, llm_token = row
            if not llm_token:
                continue
            if g.is_encrypted_token(llm_token):
                try:
                    g.decrypt_token(llm_token)
                except Exception as e:
                    log.warn("Некорректный зашифрованный llm_token для user_id=%d (%s): %s", user_id, user_name, str(e))
                continue
            enc = g.encrypt_token(llm_token)
            if enc and enc != llm_token:
                self.users_table.update(values={"llm_token": enc}, conditions={"user_id": user_id})
                migrated += 1
        if migrated > 0:
            log.info("Миграция llm_token: зашифровано %d записей", migrated)

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
            columns=["tokens_limit", "tokens_input_cost", "tokens_output_cost"],
            conditions={"user_id": user_id}
        )
        if not row:
            log.warn("Пользователь user_id=%d не найден, используются значения по умолчанию", user_id)
            return 131072, 0.0, 0.0

        tokens_limit, tokens_input_cost, tokens_output_cost = row
        in_cost = tokens_input_cost or 0.0
        out_cost = tokens_output_cost or 0.0
        return tokens_limit or 131072, in_cost, out_cost

    def get_user_reasoning_eff(self, user_id) -> str:
        row = self.users_table.select_row(
            columns=["llm_reasoning_eff"],
            conditions={"user_id": user_id}
        )
        return row[0] if row and row[0] else "none"