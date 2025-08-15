# /agent/managers/chats.py, updated 2025-07-20 15:45 EEST
from .db import Database, DataTable
from lib.basic_logger import BasicLogger
import globals as g
import asyncio

log = g.get_logger("chatman")


class ChatManager:
    def __init__(self):
        self.db = Database.get_database()
        self.chats_table = DataTable(
            table_name="chats",
            template=[
                "chat_id INTEGER PRIMARY KEY AUTOINCREMENT",
                "chat_description TEXT",
                "user_list TEXT DEFAULT 'all'",
                "parent_msg_id INTEGER",
                "FOREIGN KEY(parent_msg_id) REFERENCES posts(id)"
            ]
        )
        self.attached_files_table = DataTable(
            table_name="attached_files",
            template=[
                "id INTEGER PRIMARY KEY AUTOINCREMENT",
                "content BLOB",
                "ts INTEGER",
                "file_name TEXT",
                "project_id INTEGER",
                "FOREIGN KEY(project_id) REFERENCES projects(id)"
            ]
        )
        self.switch_events = {}  # Хранилище событий переключения чата: {f"{user_id}:{chat_id}": asyncio.Event}

    def active_chat(self, user) -> int:
        # Проверяем active_chat в sessions_table
        user_id = user
        if isinstance(user, str):
            user_id = g.user_manager.get_user_id_by_name(user)
        row = g.sessions_table.select_row(
            columns=['session_id', 'active_chat'],
            conditions={'user_id': int(user_id)} )
        if not row:
            log.error("No session record for user_id %d", user_id)
            return None
        return row[1] if row and row[1] is not None else None

    def sw_event(self, user_id: int, chat_id: int, action=None):
        """Управляет событием переключения чата и возвращает его состояние is_set."""
        switch_key = f"{user_id}:{chat_id}"
        if switch_key not in self.switch_events:
            self.switch_events[switch_key] = asyncio.Event()
            log.debug("Создано событие для switch_key=%s", switch_key)

        event = self.switch_events[switch_key]
        if action == 'set':
            event.set()
            log.debug("Установлено событие для switch_key=%s", switch_key)
        elif action == 'clear':
            if self.active_chat(user_id) == chat_id:
                event.clear()
                log.debug("Сброшено событие для switch_key=%s, active_chat=%d", switch_key, chat_id)

        return event.is_set()

    def list_chats(self, user_id: int):
        chats = self.chats_table.select_from(
            columns=['chat_id', 'chat_description', 'user_list', 'parent_msg_id']
        )
        result = []
        active = self.active_chat(user_id)
        for chat in chats:
            user_list = chat[2].split(',')
            if str(user_id) in user_list or 'all' in user_list:
                result.append({"chat_id": chat[0], "description": chat[1],
                               "user_list": user_list, "parent_msg_id": chat[3], "active": active == chat[0]})
            else:
                log.debug("Chat %d not allowed for user_id %d due list %s",  chat[0], user_id, str(user_list))
        log.debug("Возвращено %d чатов для user_id=%d", len(result), user_id)
        return result

    def create_chat(self, description, user_id, parent_msg_id=None):
        try:
            chat_id = self.chats_table.insert_into(
                values={
                    'chat_description': description,
                    'user_list': str(user_id),
                    'parent_msg_id': parent_msg_id
                }
            )
            log.debug("Создан чат chat_id=%d для user_id=%d", chat_id, user_id)
            return chat_id
        except Exception as e:
            log.excpt("Ошибка создания чата для user_id=%d: ", user_id, e=e)
            return {"error": str(e)}

    def delete_chat(self, chat_id, user_id: int):
        try:
            # Проверяем наличие подчатов
            sub_chats = self.chats_table.select_from(
                conditions={'parent_msg_id': f"(SELECT id FROM posts WHERE chat_id = {chat_id})"},
                columns=['chat_id']
            )
            if sub_chats:
                log.info("Невозможно удалить чат chat_id=%d, так как он имеет подчаты", chat_id)
                return {"error": "Cannot delete chat with sub-chats"}

            # Удаляем посты
            self.db.execute('DELETE FROM posts WHERE chat_id = :chat_id', {'chat_id': chat_id})

            # Удаляем чат
            result = self.chats_table.delete_from(
                conditions={'chat_id': chat_id, 'user_list': user_id}
            )
            if result.rowcount == 0:
                log.info("Чат chat_id=%d не найден или пользователь user_id=%d не авторизован", chat_id, user_id)
                return {"error": "Chat not found or unauthorized"}
            log.info("Удалён чат chat_id=%d пользователем user_id=%d", chat_id, user_id)
            return {"status": "ok"}
        except Exception as e:
            log.excpt("Ошибка удаления чата chat_id=%d: ", chat_id, e=e)
            return {"error": str(e)}

    def get_chat_hierarchy(self, chat_id):
        hierarchy = []
        while chat_id is not None:
            row = self.chats_table.select_row(
                columns=['chat_id', 'parent_msg_id'],
                conditions={'chat_id': chat_id}
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
        # log.debug("Иерархия чатов для chat_id=%d: ~C95%s~C00", chat_id, str(hierarchy))
        return hierarchy[::-1]

    def get_file_stats(self, chat_id):
        try:
            files = self.attached_files_table.select_from(
                conditions={'chat_id': chat_id},
                columns=['id', 'file_name', 'ts']
            )
            stats = {
                "chat_id": chat_id,
                "total_files": len(files),
                "files": [
                    {
                        "file_id": file[0],
                        "file_name": file[1].lstrip('@'),
                        "timestamp": file[2]
                    } for file in files
                ]
            }
            log.debug("Получена статистика файлов для chat_id=%d: %d файлов", chat_id, stats['total_files'])
            return stats
        except Exception as e:
            log.excpt("Ошибка получения статистики файлов для chat_id=%d: ", chat_id, e=e)
            return {"error": str(e)}