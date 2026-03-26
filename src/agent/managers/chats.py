# /agent/managers/chats.py, updated 2025-07-20 15:45 EEST
from .db import Database, DataTable
from datetime import datetime
import globals as g
import asyncio
import time

log = g.get_logger("chatman")


class ChatLocker:    # можно использовать с оператором with
    def __init__(self, chat_id: int, user_name: str):
        self.chat_id = chat_id
        self.user_name = user_name
        self._acquired_at = None

    async def __aenter__(self):
        self._acquired_at = await g.chat_manager.acquire_chat_lock(self.chat_id, self.user_name)
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        g.chat_manager.release_chat_lock(self.chat_id, self.user_name, self._acquired_at)
        if exc_value:
            log.excpt("ChatLocker.__aexit__", exc_info=(exc_type, exc_value, traceback))

    def __enter__(self):
        g.chat_manager.set_chat_busy(self.chat_id, self.user_name)

    def __exit__(self, exc_type, exc_value, traceback):
        g.chat_manager.release_chat(self.chat_id, self.user_name)
        if exc_value:
            log.excpt("ChatLocker.__exit__", exc_info=(exc_type, exc_value, traceback))


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
        self.chats_busy = {}  # [id] = dict[user_name]:start_from, где статус free или thinking:user_name
        self.set_chat_busy(0, 'admin')
        log.debug("chats_info = %s", str(self.chats_busy))
        self.switch_events = {}  # Хранилище событий переключения чата: {f"{user_id}:{chat_id}": asyncio.Event}
        self.chat_locks = {}
        self.chat_lock_stats = {}

    @staticmethod
    def active_chat(user, sid=None) -> int:
        # Проверяем active_chat в sessions_table
        user_id = user
        if isinstance(user, str):
            user_id = g.user_manager.get_user_id_by_name(user)
        cond = {'user_id': int(user_id)}
        if sid is not None:
            cond['session_id'] = sid  # для будущей поддержки мульти-сессий

        row = g.sessions_table.select_row(
            columns=['session_id', 'active_chat'],
            conditions=cond)
        if not row:
            log.error("No session record for user_id %d", user_id)
            return 0
        return row[1] if row and row[1] is not None else None

    @staticmethod
    def select_chat(session_id: int, user_id: int, chat_id: int):
        g.sessions_table.insert_or_replace({
            'session_id': session_id,
            'user_id': user_id,
            'active_chat': chat_id
        })
        log.debug("Выбран активный чат id=%d для session_id=%s, user_id=%d", chat_id, session_id, user_id)

    @staticmethod
    def active_project(user_id, session_id=None) -> int | None:
        uid = user_id
        if isinstance(user_id, str):
            uid = g.user_manager.get_user_id_by_name(user_id)
        cond = {'user_id': int(uid)}
        if session_id is not None:
            cond['session_id'] = session_id
        row = g.sessions_table.select_row(
            columns=['active_project'],
            conditions=cond)
        return row[0] if row and row[0] is not None else None

    @staticmethod
    def select_project(session_id: str, user_id: int, project_id: int | None):
        g.sessions_table.insert_or_replace({
            'session_id': session_id,
            'user_id': user_id,
            'active_project': project_id
        })
        log.debug("Выбран активный проект id=%s для session_id=%s, user_id=%d",
                  str(project_id), session_id, user_id)

    def chat_status(self, chat_id: int) -> dict:
        busy = self.chats_busy.get(chat_id, {})
        now = datetime.utcnow().timestamp()
        stats = self.chat_lock_stats.get(chat_id, {})
        if not busy:
            return {
                'status': 'free',
                'actor': '',
                'elapsed': 0,
                'lock_wait_ms_avg': round(stats.get('wait_total', 0.0) / stats.get('acquires', 1), 1) if stats else 0.0,
                'lock_wait_ms_max': round(stats.get('wait_max', 0.0), 1) if stats else 0.0,
                'lock_hold_ms_avg': round(stats.get('hold_total', 0.0) / stats.get('releases', 1), 1) if stats else 0.0,
                'lock_hold_ms_max': round(stats.get('hold_max', 0.0), 1) if stats else 0.0,
                'lock_acquires': stats.get('acquires', 0),
            }

        oldest = now
        for from_ts in busy.values():
            oldest = min(oldest, from_ts)
        result = {'status': 'busy', 'from_ts': oldest}
        if result:
            result['elapsed'] = int(now - oldest)
            result['actor'] = ', '.join(busy.keys())  # TODO: better user actors
            result['lock_wait_ms_avg'] = round(stats.get('wait_total', 0.0) / stats.get('acquires', 1), 1) if stats else 0.0
            result['lock_wait_ms_max'] = round(stats.get('wait_max', 0.0), 1) if stats else 0.0
            result['lock_hold_ms_avg'] = round(stats.get('hold_total', 0.0) / stats.get('releases', 1), 1) if stats else 0.0
            result['lock_hold_ms_max'] = round(stats.get('hold_max', 0.0), 1) if stats else 0.0
            result['lock_acquires'] = stats.get('acquires', 0)
        return result

    def _chat_lock(self, chat_id: int) -> asyncio.Lock:
        lock = self.chat_locks.get(chat_id)
        if lock is None:
            lock = asyncio.Lock()
            self.chat_locks[chat_id] = lock
        return lock

    def _chat_lock_stats(self, chat_id: int) -> dict:
        stats = self.chat_lock_stats.get(chat_id)
        if stats is None:
            stats = {
                'acquires': 0,
                'releases': 0,
                'wait_total': 0.0,
                'wait_max': 0.0,
                'hold_total': 0.0,
                'hold_max': 0.0,
            }
            self.chat_lock_stats[chat_id] = stats
        return stats

    async def acquire_chat_lock(self, chat_id: int, user_name: str) -> float:
        lock = self._chat_lock(chat_id)
        stats = self._chat_lock_stats(chat_id)
        t0 = time.monotonic()
        await lock.acquire()
        wait_ms = (time.monotonic() - t0) * 1000.0
        stats['acquires'] += 1
        stats['wait_total'] += wait_ms
        stats['wait_max'] = max(stats['wait_max'], wait_ms)
        self.set_chat_busy(chat_id, user_name)
        log.debug("Чат %d lock acquired by %s wait=%.1fms", chat_id, user_name, wait_ms)
        return time.monotonic()

    def release_chat_lock(self, chat_id: int, user_name: str, acquired_at: float | None):
        lock = self._chat_lock(chat_id)
        hold_ms = 0.0
        if acquired_at:
            hold_ms = max(0.0, (time.monotonic() - acquired_at) * 1000.0)
        stats = self._chat_lock_stats(chat_id)
        stats['releases'] += 1
        stats['hold_total'] += hold_ms
        stats['hold_max'] = max(stats['hold_max'], hold_ms)
        self.release_chat(chat_id, user_name)
        if lock.locked():
            lock.release()
        log.debug("Чат %d lock released by %s hold=%.1fms", chat_id, user_name, hold_ms)

    def set_chat_busy(self, chat_id: int, user_name: str):
        now = datetime.utcnow().timestamp()
        busy = self.chats_busy.get(chat_id, {})
        if busy.get(user_name):
            log.debug("Повторный захват пользователем %s", user_name)
        else:
            busy[user_name] = now
        self.chats_busy[chat_id] = busy
        log.debug("Чат %d занят пользователями %s ", chat_id, str(busy.keys))

    def release_chat(self, chat_id: int, user_name: str):
        busy = self.chats_busy.get(chat_id, {})
        if user_name in busy:
            busy.pop(user_name)
            log.debug("Чат %d разблокирован пользователем %s", chat_id, user_name)

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
            log.debug("Создан чат chat_id=%s для user_id=%d", str(chat_id), user_id)
            return chat_id
        except Exception as e:
            log.excpt("Ошибка создания чата для user_id=%d: ", user_id, e=e)
            raise

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