import logging
from .db import Database

class ChatManager:
    def __init__(self):
        self.db = Database.get_database()
        self._create_tables()

    def _create_tables(self):
        self.db.execute('''
            CREATE TABLE IF NOT EXISTS chats (
                chat_id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_description TEXT,
                user_list TEXT DEFAULT 'all',
                parent_msg_id INTEGER,
                FOREIGN KEY(parent_msg_id) REFERENCES posts(id)
            )
        ''')

    def list_chats(self, user_id):
        chats = self.db.fetch_all(
            'SELECT chat_id, chat_description, user_list, parent_msg_id FROM chats WHERE user_list = :all OR user_list LIKE :user_id',
            {'all': 'all', 'user_id': f'%{user_id}%'}
        )
        return [{"chat_id": chat[0], "description": chat[1], "user_list": chat[2], "parent_msg_id": chat[3]} for chat in chats]

    def create_chat(self, description, user_id, parent_msg_id=None):
        result = self.db.execute(
            'INSERT INTO chats (chat_description, user_list, parent_msg_id) VALUES (:description, :user_list, :parent_msg_id)',
            {'description': description, 'user_list': str(user_id), 'parent_msg_id': parent_msg_id}
        )
        chat_id = self.db.fetch_one('SELECT last_insert_rowid()')[0]
        return chat_id

    def delete_chat(self, chat_id, user_id):
        count = self.db.fetch_one(
            'SELECT COUNT(*) FROM chats WHERE parent_msg_id IN (SELECT id FROM posts WHERE chat_id = :chat_id)',
            {'chat_id': chat_id}
        )
        if count[0] > 0:
            logging.info(f"#INFO: Нельзя удалить чат {chat_id}, так как он имеет подчаты")
            return {"error": "Cannot delete chat with sub-chats"}
        self.db.execute('DELETE FROM posts WHERE chat_id = :chat_id', {'chat_id': chat_id})
        result = self.db.execute(
            'DELETE FROM chats WHERE chat_id = :chat_id AND user_list LIKE :user_id',
            {'chat_id': chat_id, 'user_id': f'%{user_id}%'}
        )
        if result.rowcount == 0:
            logging.info(f"#INFO: Чат {chat_id} не найден или пользователь {user_id} не имеет прав")
            return {"error": "Chat not found or unauthorized"}
        logging.info(f"#INFO: Удалён чат {chat_id} пользователем {user_id}")
        return {"status": "ok"}

    def get_chat_hierarchy(self, chat_id):
        hierarchy = []
        while chat_id is not None:
            row = self.db.fetch_one(
                'SELECT chat_id, parent_msg_id FROM chats WHERE chat_id = :chat_id',
                {'chat_id': chat_id}
            )
            if row:
                hierarchy.append(row[0])
                if row[1] is not None:
                    parent_row = self.db.fetch_one(
                        'SELECT chat_id FROM posts WHERE id = :parent_msg_id',
                        {'parent_msg_id': row[1]}
                    )
                    chat_id = parent_row[0] if parent_row else None
                else:
                    chat_id = None
            else:
                break
        return hierarchy[::-1]
