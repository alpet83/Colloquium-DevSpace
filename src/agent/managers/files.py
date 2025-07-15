import time
import logging
import json
from .db import Database

class FileManager:
    def __init__(self):
        self.db = Database()
        self._create_tables()

    def _create_tables(self):
        logging.info(f"#INFO: Создание таблицы attached_files")
        self.db.execute('''
            CREATE TABLE IF NOT EXISTS attached_files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content BLOB,
                file_id INTEGER,
                ts INTEGER,
                file_name TEXT
            )
        ''')

    def upload_file(self, chat_id, user_id, content, file_name):
        timestamp = int(time.time())
        file_id = self.db.fetch_one('SELECT last_insert_rowid()')[0] + 1
        self.db.execute(
            'INSERT INTO attached_files (content, file_id, ts, file_name) VALUES (:content, :file_id, :ts, :file_name)',
            {'content': content, 'file_id': file_id, 'ts': timestamp, 'file_name': file_name}
        )
        file_id = self.db.fetch_one('SELECT last_insert_rowid()')[0]
        logging.info(f"#INFO: Загружен файл {file_id} для chat_id {chat_id} пользователем {user_id}: {file_name}")
        return {"file_id": file_id}

    def update_file(self, file_id, user_id, content, file_name):
        result = self.db.execute(
            'UPDATE attached_files SET content = :content, file_name = :file_name, ts = :ts WHERE id = :file_id',
            {'content': content, 'file_name': file_name, 'ts': int(time.time()), 'file_id': file_id}
        )
        if result.rowcount == 0:
            logging.info(f"#INFO: Файл {file_id} не найден")
            return {"error": "File not found"}
        logging.info(f"#INFO: Обновлён файл {file_id} пользователем {user_id}: {file_name}")
        return {"status": "ok"}

    def delete_file(self, file_id, user_id):
        count = self.db.fetch_one(
            'SELECT COUNT(*) FROM posts WHERE message LIKE :pattern',
            {'pattern': f'%@attach#{file_id}%'}
        )
        if count[0] > 0:
            logging.info(f"#INFO: Нельзя удалить файл {file_id}, так как он используется в сообщениях")
            return {"error": "Cannot delete file used in posts"}
        result = self.db.execute(
            'DELETE FROM attached_files WHERE id = :file_id',
            {'file_id': file_id}
        )
        if result.rowcount == 0:
            logging.info(f"#INFO: Файл {file_id} не найден")
            return {"error": "File not found"}
        logging.info(f"#INFO: Удалён файл {file_id} пользователем {user_id}")
        return {"status": "ok"}

    def list_files(self, user_id):
        files = self.db.fetch_all('SELECT id, file_id, ts, file_name FROM attached_files')
        return [{"id": row[0], "file_id": row[1], "ts": row[2], "file_name": row[3]} for row in files]

    def get_file(self, file_id):
        row = self.db.fetch_one(
            'SELECT id, file_id, ts, file_name FROM attached_files WHERE id = :file_id',
            {'file_id': file_id}
        )
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
