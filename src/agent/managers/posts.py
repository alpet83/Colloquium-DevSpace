# /agent/managers/posts.py, updated 2025-07-14 19:20 EEST
import time
import logging
import re
from managers.db import Database
import globals

class PostManager:
    def __init__(self, user_manager):
        self.user_manager = user_manager
        self.db = Database()
        self.init_db()

    def init_db(self):
        try:
            self.db.execute('''
                CREATE TABLE IF NOT EXISTS posts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER,
                    user_id INTEGER,
                    message TEXT,
                    timestamp INTEGER,
                    FOREIGN KEY (chat_id) REFERENCES chats(id),
                    FOREIGN KEY (user_id) REFERENCES users(id)
                )
            ''')
            logging.info("#INFO: Инициализирована БД: sqlite:////app/data/multichat.db")
            logging.info("#INFO: Создание таблицы posts")
        except Exception as e:
            logging.error(f"#ERROR: Ошибка инициализации БД posts: {str(e)}")
            raise

    def add_message(self, chat_id, user_id, message):
        try:
            timestamp = int(time.time())
            self.db.execute(
                'INSERT INTO posts (chat_id, user_id, message, timestamp) VALUES (:chat_id, :user_id, :message, :timestamp)',
                {'chat_id': chat_id, 'user_id': user_id, 'message': message, 'timestamp': timestamp}
            )
            post_id = self.db.fetch_one('SELECT last_insert_rowid()')[0]
            logging.debug(f"#DEBUG: Добавлено сообщение post_id={post_id}, chat_id={chat_id}, user_id={user_id}")
            return {"status": "ok", "post_id": post_id}
        except Exception as e:
            logging.error(f"#ERROR: Ошибка добавления сообщения для chat_id={chat_id}, user_id={user_id}: {str(e)}")
            return {"error": str(e)}

    def get_history(self, chat_id):
        try:
            hierarchy = globals.chat_manager.get_chat_hierarchy(chat_id)
            history = []
            for c_id in hierarchy:
                posts = self.db.fetch_all('''
                    SELECT p.id, p.chat_id, p.user_id, p.message, p.timestamp, u.user_name
                    FROM posts p
                    JOIN users u ON p.user_id = u.user_id
                    WHERE p.chat_id = :chat_id
                    ORDER BY p.timestamp
                ''', {'chat_id': c_id})
                for post in posts:
                    message = post[3]
                    file_ids = re.findall(r'@attach#(\d+)', message)
                    file_names = []
                    for file_id in file_ids:
                        file_data = self.db.fetch_one(
                            'SELECT file_name, ts FROM attached_files WHERE id = :file_id',
                            {'file_id': file_id}
                        )
                        if file_data:
                            file_names.append({"file_id": int(file_id), "file_name": file_data[0], "ts": file_data[1]})
                    history.append({
                        "id": post[0],
                        "chat_id": post[1],
                        "user_id": post[2],
                        "message": message,
                        "timestamp": post[4],
                        "file_names": file_names,
                        "user_name": post[5]
                    })
            logging.debug(f"#DEBUG: Получена история для chat_id={chat_id}: {len(history)} сообщений")
            return history
        except Exception as e:
            logging.error(f"#ERROR: Ошибка получения истории для chat_id={chat_id}: {str(e)}")
            return {"error": str(e)}

    def delete_post(self, post_id, user_id):
        try:
            post = self.db.fetch_one('SELECT user_id FROM posts WHERE id = :post_id', {'post_id': post_id})
            if not post:
                logging.info(f"#INFO: Сообщение post_id={post_id} не найдено")
                return {"error": "Post not found"}
            if post[0] != user_id:
                logging.info(f"#INFO: Пользователь user_id={user_id} не имеет прав для удаления post_id={post_id}")
                return {"error": "Permission denied"}
            self.db.execute('DELETE FROM posts WHERE id = :post_id', {'post_id': post_id})
            logging.debug(f"#DEBUG: Удалено сообщение post_id={post_id} для user_id={user_id}")
            return {"status": "ok"}
        except Exception as e:
            logging.error(f"#ERROR: Ошибка удаления сообщения post_id={post_id}: {str(e)}")
            return {"error": str(e)}
