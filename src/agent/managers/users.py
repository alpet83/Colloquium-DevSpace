# /agent/managers/users.py, updated 2025-07-18 14:28 EEST
import hashlib
import binascii
import os
from .db import Database
from lib.basic_logger import BasicLogger
import globals

log = globals.get_logger("userman")

class UserManager:
    def __init__(self):
        self.db = Database.get_database()
        self._create_tables()
        self._init_admin_user()

    def _create_tables(self):
        log.info("Создание таблицы users")
        self.db.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_name TEXT,
                llm_class TEXT,
                llm_token TEXT,
                password_hash TEXT,
                salt TEXT
            )
        ''')

    def _init_admin_user(self):
        count = self.db.fetch_one('SELECT COUNT(*) FROM users WHERE user_name = :username', {'username': 'admin'})
        if count[0] == 0:
            salt = os.urandom(16)
            password = "colloquium"
            server_hash = hashlib.sha256(salt + password.encode()).hexdigest()
            salt_hex = binascii.hexlify(salt).decode()
            self.db.execute(
                'INSERT INTO users (user_name, llm_class, llm_token, password_hash, salt) VALUES (:user_name, NULL, NULL, :password_hash, :salt)',
                {'user_name': 'admin', 'password_hash': server_hash, 'salt': salt_hex}
            )
            log.info("Создан пользователь admin с паролем %s", "colloquium")
        count = self.db.fetch_one('SELECT COUNT(*) FROM users WHERE user_name = :username', {'username': 'agent'})
        if count[0] == 0:
            self.db.execute(
                'INSERT INTO users (user_name, llm_class, llm_token, password_hash, salt) VALUES (:user_name, NULL, NULL, NULL, NULL)',
                {'user_name': 'agent'}
            )
            log.info("Создан системный пользователь %s", "agent")
        count = self.db.fetch_one('SELECT COUNT(*) FROM users WHERE user_name = :username', {'username': 'grok'})
        if count[0] == 0:
            self.db.execute(
                'INSERT INTO users (user_name, llm_class, llm_token, password_hash, salt) VALUES (:user_name, :llm_class, :llm_token, NULL, NULL)',
                {'user_name': 'grok', 'llm_class': 'grok-3', 'llm_token': '<your_xai_api_key>'}
            )
            log.info("Создан пользователь %s с llm_class=%s", "grok", "grok-3")

    def check_auth(self, username, password):
        row = self.db.fetch_one(
            'SELECT user_id, password_hash, salt FROM users WHERE user_name = :username',
            {'username': username}
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
        row = self.db.fetch_one('SELECT user_name FROM users WHERE user_id = :user_id', {'user_id': user_id})
        return row[0] if row else 'Unknown'

    def get_user_role(self, user_id):
        row = self.db.fetch_one('SELECT user_name FROM users WHERE user_id = :user_id', {'user_id': user_id})
        if not row:
            return None
        username = row[0]
        if username == 'admin':
            return 'admin'
        elif username == 'agent':
            return 'mcp'
        elif username == 'grok':
            return 'assistant'
        return 'developer'

    def get_user_id_by_name(self, user_name):
        row = self.db.fetch_one('SELECT user_id FROM users WHERE user_name = :user_name', {'user_name': user_name})
        return row[0] if row else None

    def is_llm_user(self, user_id):
        row = self.db.fetch_one('SELECT llm_class FROM users WHERE user_id = :user_id', {'user_id': user_id})
        return row[0] is not None