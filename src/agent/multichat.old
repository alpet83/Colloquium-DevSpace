import sqlite3
import time
import logging
import re
import hashlib
import binascii
import os
from llm_hands import process_message
from llm_api import LLMConnection, XAIConnection, OpenAIConnection

CHAT_DB = '/app/data/multichat.db'

class ChatActor:
    def __init__(self, user_id, user_name, llm_class=None, llm_token=None):
        self.user_id = user_id
        self.user_name = user_name
        self.llm_connection = None
        if llm_class and llm_token:
            config = {"api_key": llm_token, "model": llm_class}
            if llm_class.lower() == 'super_grok':
                self.llm_connection = XAIConnection(config)
            elif llm_class.lower() == 'chatgpt':
                self.llm_connection = OpenAIConnection(config)
            else:
                self.llm_connection = LLMConnection(config)

class MultiChat:
    def __init__(self):
        os.makedirs(os.path.dirname(CHAT_DB), exist_ok=True)
        self.conn = sqlite3.connect(CHAT_DB)
        self._create_tables()
        self._init_admin_user()
        self.actors = self._load_actors()
        logging.info(f"#INFO: Мультичат инициализирован с {len(self.actors)} акторами")

    def _create_tables(self):
        logging.info(f"#INFO: Создание таблиц в {CHAT_DB}")
        self.conn.execute('CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY AUTOINCREMENT, user_name TEXT, llm_class TEXT, llm_token TEXT, password_hash TEXT, salt TEXT)')
        self.conn.execute('CREATE TABLE IF NOT EXISTS chats (chat_id INTEGER PRIMARY KEY AUTOINCREMENT, chat_description TEXT, user_list TEXT DEFAULT "all", parent_msg_id INTEGER, FOREIGN KEY(parent_msg_id) REFERENCES posts(id))')
        self.conn.execute('CREATE TABLE IF NOT EXISTS posts (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, timestamp INTEGER, user_id INTEGER, message TEXT, FOREIGN KEY(chat_id) REFERENCES chats(chat_id), FOREIGN KEY(user_id) REFERENCES users(user_id))')
        self.conn.execute('CREATE TABLE IF NOT EXISTS attached_files (id INTEGER PRIMARY KEY AUTOINCREMENT, content BLOB, file_id INTEGER, ts INTEGER, file_name TEXT)')
        self.conn.commit()

    def _init_admin_user(self):
        cur = self.conn.cursor()
        cur.execute('SELECT COUNT(*) FROM users WHERE user_name = "admin"')
        if cur.fetchone()[0] == 0:
            salt = os.urandom(16)
            password = "colloqium"
            server_hash = hashlib.sha256(salt + password.encode()).hexdigest()
            salt_hex = binascii.hexlify(salt).decode()
            self.conn.execute('INSERT INTO users (user_name, llm_class, llm_token, password_hash, salt) VALUES (?, NULL, NULL, ?, ?)', ("admin", server_hash, salt_hex))
            self.conn.commit()
            logging.info("#INFO: Создан пользователь admin с паролем colloqium")

        cur.execute('SELECT COUNT(*) FROM users WHERE user_name = "mcp"')
        if cur.fetchone()[0] == 0:
            self.conn.execute('INSERT INTO users (user_name, llm_class, llm_token, password_hash, salt) VALUES (?, NULL, NULL, NULL, NULL)', ("mcp",))
            self.conn.commit()
            logging.info("#INFO: Создан системный пользователь mcp")

    def _load_actors(self):
        actors = []
        cur = self.conn.cursor()
        cur.execute('SELECT user_id, user_name, llm_class, llm_token FROM users')
        for row in cur.fetchall():
            actors.append(ChatActor(row[0], row[1], row[2], row[3]))
        return actors

    def check_auth(self, username, password):
        cur = self.conn.cursor()
        cur.execute('SELECT user_id, password_hash, salt FROM users WHERE user_name = ?', (username,))
        row = cur.fetchone()
        if not row:
            logging.info(f"#INFO: Неверное имя пользователя: {username}")
            return None
        user_id, stored_hash, salt_hex = row
        salt = binascii.unhexlify(salt_hex)
        server_hash = hashlib.sha256(salt + password.encode()).hexdigest()
        if server_hash != stored_hash:
            logging.info(f"#INFO: Неверный пароль для username={username}")
            return None
        return user_id

    def add_message(self, chat_id, user_id, message):
        timestamp = int(time.time())
        self.conn.execute('INSERT INTO posts (chat_id, timestamp, user_id, message) VALUES (?, ?, ?, ?)', (chat_id, timestamp, user_id, message))
        self.conn.commit()
        logging.info(f"#INFO: Добавлено сообщение в chat_id {chat_id} от user_id {user_id}: #post_{timestamp}")
        response = process_message(message, timestamp, self.get_user_name(user_id))
        if response:
            mcp_user_id = self.get_user_id_by_name('mcp')
            self.conn.execute('INSERT INTO posts (chat_id, timestamp, user_id, message) VALUES (?, ?, ?, ?)', (chat_id, int(time.time()), mcp_user_id, response))
            self.conn.commit()
        llm_actors = [actor for actor in self.actors if actor.llm_connection]
        exclude_source = self.is_llm_user(user_id)
        self.replicate_to_llm(chat_id, llm_actors, exclude_source_id=user_id if exclude_source else None)

    def delete_post(self, post_id, user_id):
        cur = self.conn.cursor()
        cur.execute('SELECT COUNT(*) FROM chats WHERE parent_msg_id = ?', (post_id,))
        if cur.fetchone()[0] > 0:
            logging.info(f"#INFO: Нельзя удалить сообщение {post_id}, так как оно имеет подчаты")
            return {"error": "Cannot delete post with sub-chats"}
        cur.execute('DELETE FROM posts WHERE id = ? AND user_id = ?', (post_id, user_id))
        if cur.rowcount == 0:
            logging.info(f"#INFO: Сообщение {post_id} не найдено или пользователь {user_id} не имеет прав")
            return {"error": "Post not found or unauthorized"}
        self.conn.commit()
        logging.info(f"#INFO: Удалено сообщение {post_id} пользователем {user_id}")
        return {"status": "ok"}

    def delete_chat(self, chat_id, user_id):
        cur = self.conn.cursor()
        cur.execute('SELECT COUNT(*) FROM chats WHERE parent_msg_id IN (SELECT id FROM posts WHERE chat_id = ?)', (chat_id,))
        if cur.fetchone()[0] > 0:
            logging.info(f"#INFO: Нельзя удалить чат {chat_id}, так как он имеет подчаты")
            return {"error": "Cannot delete chat with sub-chats"}
        cur.execute('DELETE FROM posts WHERE chat_id = ?', (chat_id,))
        cur.execute('DELETE FROM chats WHERE chat_id = ? AND user_list LIKE ?', (chat_id, f'%{user_id}%'))
        if cur.rowcount == 0:
            logging.info(f"#INFO: Чат {chat_id} не найден или пользователь {user_id} не имеет прав")
            return {"error": "Chat not found or unauthorized"}
        self.conn.commit()
        logging.info(f"#INFO: Удалён чат {chat_id} пользователем {user_id}")
        return {"status": "ok"}

    def upload_file(self, chat_id, user_id, content, file_name):
        timestamp = int(time.time())
        cur = self.conn.cursor()
        cur.execute('INSERT INTO attached_files (content, file_id, ts, file_name) VALUES (?, ?, ?, ?)', (content, cur.execute('SELECT last_insert_rowid()').fetchone()[0] + 1, timestamp, file_name))
        self.conn.commit()
        cur.execute('SELECT last_insert_rowid()')
        file_id = cur.fetchone()[0]
        logging.info(f"#INFO: Загружен файл {file_id} для chat_id {chat_id} пользователем {user_id}: {file_name}")
        return {"file_id": file_id}

    def update_file(self, file_id, user_id, content, file_name):
        cur = self.conn.cursor()
        cur.execute('UPDATE attached_files SET content = ?, file_name = ?, ts = ? WHERE id = ?', (content, file_name, int(time.time()), file_id))
        if cur.rowcount == 0:
            logging.info(f"#INFO: Файл {file_id} не найден")
            return {"error": "File not found"}
        self.conn.commit()
        logging.info(f"#INFO: Обновлён файл {file_id} пользователем {user_id}: {file_name}")
        return {"status": "ok"}

    def delete_file(self, file_id, user_id):
        cur = self.conn.cursor()
        cur.execute('SELECT COUNT(*) FROM posts WHERE message LIKE ?', (f'%@attach#{file_id}%',))
        if cur.fetchone()[0] > 0:
            logging.info(f"#INFO: Нельзя удалить файл {file_id}, так как он используется в сообщениях")
            return {"error": "Cannot delete file used in posts"}
        cur.execute('DELETE FROM attached_files WHERE id = ?', (file_id,))
        if cur.rowcount == 0:
            logging.info(f"#INFO: Файл {file_id} не найден")
            return {"error": "File not found"}
        self.conn.commit()
        logging.info(f"#INFO: Удалён файл {file_id} пользователем {user_id}")
        return {"status": "ok"}

    def list_files(self, user_id):
        cur = self.conn.cursor()
        cur.execute('SELECT id, file_id, ts, file_name FROM attached_files')
        files = cur.fetchall()
        return [{"id": row[0], "file_id": row[1], "ts": row[2], "file_name": row[3]} for row in files]

    def get_file(self, file_id):
        cur = self.conn.cursor()
        cur.execute('SELECT id, file_id, ts, file_name FROM attached_files WHERE id = ?', (file_id,))
        row = cur.fetchone()
        if not row:
            return None
        return {"id": row[0], "file_id": row[1], "ts": row[2], "file_name": row[3]}

    def get_sandwiches_index(self):
        try:
            with open('/opt/docker/mcp-server/sandwiches_index.json', 'r') as f:
                index = json.load(f)
            return {"files": [entry["file_name"] for entry in index]}
        except FileNotFoundError:
            logging.error("#ERROR: Файл sandwiches_index.json не найден")
            return {"error": "Sandwiches index not found"}
        except json.JSONDecodeError:
            logging.error("#ERROR: Ошибка декодирования sandwiches_index.json")
            return {"error": "Invalid sandwiches index format"}

    def is_llm_user(self, user_id):
        cur = self.conn.cursor()
        cur.execute('SELECT llm_class FROM users WHERE user_id = ?', (user_id,))
        row = cur.fetchone()
        return row[0] is not None

    def get_user_name(self, user_id):
        cur = self.conn.cursor()
        cur.execute('SELECT user_name FROM users WHERE user_id = ?', (user_id,))
        row = cur.fetchone()
        return row[0] if row else 'Unknown'

    def get_user_id_by_name(self, user_name):
        cur = self.conn.cursor()
        cur.execute('SELECT user_id FROM users WHERE user_name = ?', (user_name,))
        row = cur.fetchone()
        return row[0] if row else None

    def get_history(self, chat_id, limit=50):
        hierarchy = self.get_chat_hierarchy(chat_id)
        all_history = []
        cur = self.conn.cursor()
        for cid in hierarchy:
            cur.execute('SELECT parent_msg_id FROM chats WHERE chat_id = ?', (cid,))
            parent_msg_id = cur.fetchone()[0]
            if parent_msg_id:
                cur.execute('SELECT id, chat_id, timestamp, user_id, message, NULL as file_name, NULL as ts FROM posts WHERE id = ?', (parent_msg_id,))
                parent_msg = cur.fetchone()
                if parent_msg:
                    all_history.append({
                        "id": parent_msg[0],
                        "chat_id": parent_msg[1],
                        "timestamp": parent_msg[2],
                        "user_id": parent_msg[3],
                        "message": parent_msg[4],
                        "file_name": parent_msg[5],
                        "ts": parent_msg[6]
                    })
            cur.execute('SELECT p.id, p.chat_id, p.timestamp, p.user_id, p.message, f.file_name, f.ts FROM posts p LEFT JOIN attached_files f ON p.message LIKE ? AND f.id = CAST(SUBSTR(p.message, 9) AS INTEGER) WHERE p.chat_id = ? ORDER BY p.timestamp ASC LIMIT ?', (f'@attach#%', cid, limit))
            history = cur.fetchall()
            all_history.extend([{
                "id": row[0],
                "chat_id": row[1],
                "timestamp": row[2],
                "user_id": row[3],
                "message": row[4],
                "file_name": row[5],
                "ts": row[6]
            } for row in history])
        return all_history

    def list_chats(self, user_id):
        cur = self.conn.cursor()
        cur.execute('SELECT chat_id, chat_description, user_list, parent_msg_id FROM chats WHERE user_list = "all" OR user_list LIKE ?', (f'%{user_id}%',))
        chats = cur.fetchall()
        return [{"chat_id": chat[0], "description": chat[1], "user_list": chat[2], "parent_msg_id": chat[3]} for chat in chats]

    def create_chat(self, description, user_id, parent_msg_id=None):
        cur = self.conn.cursor()
        cur.execute('INSERT INTO chats (chat_description, user_list, parent_msg_id) VALUES (?, ?, ?)', (description, str(user_id), parent_msg_id))
        self.conn.commit()
        cur.execute('SELECT last_insert_rowid()')
        return cur.fetchone()[0]

    def get_chat_hierarchy(self, chat_id):
        hierarchy = []
        cur = self.conn.cursor()
        while chat_id is not None:
            cur.execute('SELECT chat_id, parent_msg_id FROM chats WHERE chat_id = ?', (chat_id,))
            row = cur.fetchone()
            if row:
                hierarchy.append(row[0])
                if row[1] is not None:
                    cur.execute('SELECT chat_id FROM posts WHERE id = ?', (row[1],))
                    parent_row = cur.fetchone()
                    chat_id = parent_row[0] if parent_row else None
                else:
                    chat_id = None
            else:
                break
        return hierarchy[::-1]

    def replicate_to_llm(self, chat_id, connections, exclude_source_id=None):
        hierarchy = self.get_chat_hierarchy(chat_id)
        context = ''
        for cid in hierarchy:
            cur = self.conn.cursor()
            cur.execute('SELECT parent_msg_id FROM chats WHERE chat_id = ?', (cid,))
            parent_msg_id = cur.fetchone()[0]
            if parent_msg_id:
                cur.execute('SELECT chat_id, timestamp, user_id, message FROM posts WHERE id = ?', (parent_msg_id,))
                parent_msg = cur.fetchone()
                if parent_msg:
                    context += f"#post_{parent_msg[1]} от user_id {parent_msg[2]}: {parent_msg[3]}\n"
            history = self.get_history(cid)
            context += '\n'.join([f"#post_{row['timestamp']} от user_id {row['user_id']}: {row['message']}" for row in history]) + '\n'
        for conn in connections:
            if exclude_source_id and conn.user_id == exclude_source_id:
                continue
            asyncio.create_task(conn.call(context))
