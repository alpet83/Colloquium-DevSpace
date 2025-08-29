# /app/agent/managers/posts.py, updated 2025-07-27 14:00 EEST
import json
import time
import re
import asyncio
from managers.db import Database, DataTable
import globals as g
from lib.basic_logger import BasicLogger

log = g.get_logger("postman")


class PostManager:
    """Управляет сообщениями и их историей в чат-приложении."""
    def __init__(self, user_manager):
        """Инициализирует PostManager с user_manager и настройкой таблиц базы данных.

        Args:
            user_manager: Экземпляр UserManager для работы с пользователями.
        """
        self.user_manager = user_manager
        self.db = Database.get_database()
        self.changes_history = {}
        self.posts_table = DataTable(
            table_name="posts",
            template=[
                "id INTEGER PRIMARY KEY AUTOINCREMENT",
                "chat_id INTEGER",
                "user_id INTEGER",
                "message TEXT",
                "timestamp INTEGER",
                "rql INTEGER",
                "reply_to INTEGER",
                "elapsed FLOAT",   # сколько секунд ответ "обдумывался" или обрабатывался
                "FOREIGN KEY (chat_id) REFERENCES chats(id)",
                "FOREIGN KEY (user_id) REFERENCES users(id)"
            ]
        )
        self.users_table = DataTable(
            table_name="users",
            template=[
                "user_id INTEGER PRIMARY KEY",
                "user_name TEXT NOT NULL UNIQUE",
                "llm_class TEXT",
                "llm_token TEXT"
            ]
        )

    def add_change(self, chat_id: int, post_id: int, action: str):
        """Добавляет изменение (add/edit/delete) в историю изменений чата.

        Args:
            chat_id (int): ID чата.
            post_id (int): ID поста.
            action (str): Тип действия (add/edit/delete).
        """
        if chat_id not in self.changes_history:
            self.changes_history[chat_id] = []
        effective_post_id = -post_id if action == "delete" else post_id
        self.changes_history[chat_id].append(effective_post_id)
        log.debug("Added change for chat_id=%d, post_id=%d, action=%s", chat_id, effective_post_id, action)

    def get_changes(self, chat_id: int) -> list:
        """Возвращает список изменений для указанного chat_id.

        Args:
            chat_id (int): ID чата.

        Returns:
            list: Список изменений (post_id с учётом действия).
        """
        changes = self.changes_history.get(chat_id, [])
        if changes:
            log.debug("Retrieved changes for chat_id=%d: ~%s", chat_id, str(changes))
        return changes

    def clear_changes(self, chat_id: int):
        """Очищает историю изменений для указанного chat_id.

        Args:
            chat_id (int): ID чата.
        """
        if chat_id in self.changes_history:
            log.debug("Cleared changes for chat_id=%d", chat_id)
            self.changes_history[chat_id] = []

    def get_quotes(self, history: dict) -> dict:
        """Извлекает цитаты из истории постов для указанного chat_id.

        Args:
            history (dict): История постов.

        Returns:
            dict: Словарь цитат с их метаданными.
        """
        try:
            if "chat_history" in history:
                log.debug("No quotes extracted for chat_id=%d: history contains chat_history=%s",
                          history.get("chat_id", 0), history["chat_history"])
                return {}
            quote_ids = set()
            for post in history.values():
                if post.get("message"):
                    matches = re.findall(r'@quote#(\d+)', post["message"])
                    quote_ids.update(int(qid) for qid in matches)
            quotes = {}
            for quote_id in quote_ids:
                row = g.post_processor.quotes_table.select_from(
                    columns=['quote_id', 'chat_id', 'user_id', 'content', 'timestamp'],
                    conditions={'quote_id': quote_id}
                )
                if row:
                    user_row = self.users_table.select_row(
                        conditions={'user_id': row[0][2]},
                        columns=['user_name']
                    )
                    user_name = user_row[0] if user_row else 'unknown'
                    quotes[quote_id] = {
                        "id": row[0][0],
                        "chat_id": row[0][1],
                        "user_id": row[0][2],
                        "message": row[0][3],
                        "timestamp": row[0][4],
                        "user_name": user_name
                    }
            log.debug("Extracted quotes for chat_id=%d: %s", next(iter(history.values())).get("chat_id", 0) if history else 0, str(quotes))
            return quotes
        except Exception as e:
            log.excpt("Ошибка извлечения цитат ", e=e)
            return {}

    def agent_post(self, chat_id: int, message: str, rql: int = 1, reply_to: int = None, elapsed: float = 0):
        self.add_post(chat_id, g.AGENT_UID, message, rql, reply_to, elapsed)

    async def process_post(self, post: dict, allow_rep: bool):
        message = post['message']
        agent_message = None
        chat_id = post['chat_id']
        user_id = post['user_id']
        post_id = post['post_id']
        rql = post['rql']

        # Проверяем, является ли пользователь LLM
        user_row = self.users_table.select_row(
            conditions={'user_id': user_id},
            columns=['llm_class', 'user_name']
        )
        user_name = user_row[1] if user_row else 'unknown'
        t_start = time.time()

        # через пост-процессор не требуется пропускать лишь ответы агента
        if chat_id != 2:
            try:
                pp = g.post_processor
                result = await pp.process_response(chat_id, user_id, message, post_id)  # может занять много времени, если выполнять команды MCP
                log.debug("Результат process_response: handled_cmds=%d, failed_cmds=%d, processed_msg=%s",
                          result["handled_cmds"], result["failed_cmds"], result["processed_msg"][:50])
                if isinstance(result, dict):
                    message = result.get("processed_msg", message)
                    agent_message = result.get("agent_reply")
                    if result["handled_cmds"] == 0 and result["failed_cmds"] > 0:
                        log.warn("post_processor: no commands handled, %d failed", result["failed_cmds"])
                        agent_message = f"@{user_name} {agent_message or 'Unknown error'}"
                else:
                    raise ValueError("Unexpected process_response result type: %s" % type(result))
            except Exception as e:
                log.excpt("Ошибка обработки сообщения в post_processor", e=e)
                agent_message = f"@{user_name} Error processing message: {str(e)}"

        # Обновляем сообщение на processed_message
        if message != post['message']:
            self.edit_post(post_id, message, user_id)
            log.debug("Обновлено сообщение post_id=%d с processed_message=%s", post_id, message[:50])

        # Добавляем ответ агента, если есть
        if agent_message:
            elapsed = time.time() - t_start
            agent_result = self.add_post(chat_id, 2, agent_message, rql, post_id, elapsed)
            if agent_result.get("error"):
                log.warn("Не удалось сохранить ответ агента: %s", agent_result["error"])
            else:
                agent_post_id = agent_result["post_id"]
                log.debug("Добавлен ответ агента post_id=%d, chat_id=%d, rql=%d, reply_to=%s, message=%s",
                          agent_post_id, chat_id, rql, str(post_id), agent_message[:50])

        # Проверяем необходимость репликации
        sr = False
        if allow_rep:
            sr = g.replication_manager.check_start_replication(chat_id, post_id, user_id, message, rql)

        log.debug("Обработка сообщения %d в чате %d завершена %s",
                  post_id, chat_id, 'с репликацией' if sr else 'без репликации')
        return {"chat_id": chat_id, "user_id": user_id, "post_id": post_id,
                "processed_msg": message, "agent_reply": agent_message,
                "status": "ok"}

    def add_post(self, chat_id: int, user_id: int, message: str, rql: int = 0, reply_to: int = None, elapsed: float = 0) -> dict:
        """Добавляет сообщение, обрабатывает его через post_processor и сохраняет ответ агента, если есть.

        Args:
            chat_id (int): ID чата.
            user_id (int): ID пользователя.
            message (str): Текст сообщения.
            rql (int, optional): Уровень рекурсии диалога. Defaults to 0.
            reply_to (int, optional): ID поста, на который отвечает сообщение.
            elapsed (float, optional): Время на синтез и обработку ответа

        Returns:
            dict: {'status': 'ok', 'post_id': int, 'processed_msg': str, 'agent_reply': str | None}
                  или {'error': str}
        """
        try:
            timestamp = int(time.time())
            message = message.strip()
            self.posts_table.insert_into({
                'chat_id': chat_id,
                'user_id': user_id,
                'message': message,
                'timestamp': timestamp,
                'rql': rql,
                'reply_to': reply_to,
                'elapsed': elapsed
            })
            post_id_row = self.posts_table.select_row(
                columns=['last_insert_rowid()']
            )
            post_id = post_id_row[0] if post_id_row else None
            if post_id is None:
                raise ValueError("Failed to retrieve last_insert_rowid()")
            self.add_change(chat_id, post_id, "add")
            log.debug("Сохранено сообщение post_id=%d, chat_id=%d, user_id=%d, rql=%d, reply_to=%s, message=%s",
                      post_id, chat_id, user_id, rql, str(reply_to), message[:50])
            post = {
                "chat_id": chat_id,
                "user_id": user_id,
                "post_id": post_id,
                "status": "ok",
                "message": message,
                "reply_to": reply_to,
                "elapsed": elapsed,
                "rql": rql
            }
            return post
        except Exception as e:
            log.excpt("Ошибка добавления сообщения для chat_id=%d, user_id=%d: ", chat_id, user_id, e=e)
            return {"error": str(e)}

    def latest_post(self, filters=None):
        keys = ['id', 'chat_id', 'user_id', 'message', 'rql', 'reply_to']
        row = self.posts_table.select_row(
            columns=keys,
            conditions=filters,
            order_by="id DESC"
        )
        result = {}
        if not row:
            log.error("latest_posts: Нет сообщений для ограничения %s ", json.dumps(filters))
            return result

        if len(row) == len(keys):
            for i, k in enumerate(keys):
                result[k] = row[i]
                result[i] = row[i]
        return result

    def scan_history(self, chat_id: int, visited: set = None, before_id: int = None) -> dict:
        """Рекурсивно собирает историю постов для указанного chat_id, включая родительские чаты.

        Args:
            chat_id (int): ID чата.
            visited (set, optional): Множество посещённых chat_id для предотвращения циклов.
            before_id (int, optional): ID поста, до которого собирается история.

        Returns:
            dict: История постов с метаданными.
        """
        if visited is None:
            visited = set()
        if chat_id in visited:
            log.debug("Цикл обнаружен для chat_id=%d, пропуск", chat_id)
            return {}
        visited.add(chat_id)
        history = {}
        post_ids = []
        path = visited.copy()
        # Проверка наличия родительского чата
        parent_msg_row = self.db.fetch_one(
            'SELECT parent_msg_id FROM chats WHERE chat_id = :chat_id',
            {'chat_id': chat_id}
        )
        parent_msg_id = parent_msg_row[0] if parent_msg_row else None
        if parent_msg_id:
            # Поиск родительского чата
            parent_chat_row = self.db.fetch_one(
                'SELECT chat_id FROM posts WHERE id = :parent_msg_id',
                {'parent_msg_id': parent_msg_id}
            )
            parent_chat_id = parent_chat_row[0] if parent_chat_row else None
            if parent_chat_id:
                # Рекурсивный сбор истории родительского чата до parent_msg_id
                parent_history = self.scan_history(parent_chat_id, visited, before_id=parent_msg_id)
                history.update(parent_history)
            else:
                log.warn("Родительский чат не найден для parent_msg_id=%d, chat_id=%d", parent_msg_id, chat_id)
        # Сбор постов текущего чата
        conditions = [('chat_id', '=', chat_id)]
        if before_id is not None:
            conditions.append(('id', '<=', before_id))
        rows = self.posts_table.select_from(
            columns=['p.id', 'p.chat_id', 'p.user_id', 'p.message', 'p.timestamp', 'u.user_name', 'p.rql', 'p.reply_to', 'p.elapsed'],
            conditions=conditions,
            joins=[('users', 'u', 'p.user_id = u.user_id')],
            order_by='p.id'
        )
        for row in rows:
            pid = row[0]
            elps = round(row[8], 1) if row[8] else 0
            history[pid] = {
                "id": pid,
                "chat_id": row[1],
                "user_id": row[2],
                "message": row[3],
                "timestamp": row[4],
                "user_name": row[5],
                "rql": row[6],
                "reply_to": row[7],
                "elapsed": elps,
                "action": "add"
            }
            post_ids.append(row[0])
        # Логирование собранных post_id и reply_to
        path_str = " -> ".join(str(cid) for cid in sorted(path, reverse=True))
        log.debug("Собрана история для chat_id=%d (path=%s): post_ids=%s, reply_to=%s",
                  chat_id, path_str, str(post_ids), str([history[pid]["reply_to"] for pid in post_ids]))
        return history

    def get_history(self, chat_id: int, only_changes: bool = False) -> dict:
        """Возвращает историю постов для указанного chat_id, включая reply_to.

        Args:
            chat_id (int): ID чата.
            only_changes (bool, optional): Если True, возвращает только изменения. Defaults to False.

        Returns:
            dict: История постов или {'chat_history': 'no changes'} или {'error': str}.
        """
        try:
            changes = self.get_changes(chat_id)
            if only_changes and not changes:
                return {"chat_history": "no changes"}
            history = {}
            post_ids = [abs(pid) for pid in changes if pid > 0] if only_changes else None
            deleted_ids = [-pid for pid in changes if pid < 0] if only_changes else []
            if only_changes:
                for deleted_id in deleted_ids:
                    history[deleted_id] = {
                        "id": deleted_id,
                        "chat_id": chat_id,
                        "user_id": None,
                        "message": None,
                        "timestamp": int(time.time()),
                        "rql": None,
                        "reply_to": None,
                        "user_name": None,
                        "action": "delete"
                    }
                if post_ids:
                    conditions = [('chat_id', '=', chat_id)]
                    if len(post_ids) == 1:
                        conditions.append(('id', '=', post_ids[0]))
                    else:
                        conditions.append(('id', 'IN', post_ids))
                    rows = self.posts_table.select_from(
                        columns=['p.id', 'p.chat_id', 'p.user_id', 'p.message', 'p.timestamp', 'u.user_name', 'p.rql', 'p.reply_to'],
                        conditions=conditions,
                        joins=[('users', 'u', 'p.user_id = u.user_id')],
                        order_by='p.id'
                    )
                    for row in rows:
                        action = "delete" if row[0] in deleted_ids else "add"
                        message = None if action == "delete" else row[3]
                        history[row[0]] = {
                            "id": row[0],
                            "chat_id": row[1],
                            "user_id": row[2],
                            "message": message,
                            "timestamp": row[4],
                            "user_name": row[5],
                            "rql": row[6],
                            "reply_to": row[7],
                            "action": action
                        }
            else:
                history = self.scan_history(chat_id)
            if history and only_changes:
                self.clear_changes(chat_id)
            log.debug("Получена история для chat_id=%d: %d сообщений, only_changes=%s, reply_to=%s",
                      chat_id, len(history), str(only_changes),
                      str([history[pid]["reply_to"] for pid in history]))
            return history
        except Exception as e:
            log.excpt("Ошибка получения истории для chat_id=%d: ", chat_id, e=e)
            return {"error": str(e)}

    def get_post(self, post_id: int) -> dict:
        """Возвращает пост по его ID.

        Args:
            post_id (int): ID поста.

        Returns:
            dict: Метаданные поста или None, если пост не найден.
        """
        row = self.posts_table.select_from(
            columns=['id', 'chat_id', 'user_id', 'message', 'timestamp', 'rql', 'reply_to'],
            conditions={'id': post_id}
        )
        return {
            'id': row[0][0],
            'chat_id': row[0][1],
            'user_id': row[0][2],
            'message': row[0][3],
            'timestamp': row[0][4],
            'rql': row[0][5],
            'reply_to': row[0][6]
        } if row else None

    def edit_post(self, post_id: int, message: str, user_id: int) -> dict:
        """Редактирует пост, если пользователь имеет права.

        Args:
            post_id (int): ID поста.
            message (str): Новое содержимое поста.
            user_id (int): ID пользователя.

        Returns:
            dict: {'status': 'ok'} или {'error': str}
        """
        try:
            post = self.posts_table.select_from(
                columns=['user_id', 'chat_id', 'rql'],
                conditions=[('id', '=', post_id)]
            )
            if not post:
                log.info("Сообщение post_id=%d не найдено", post_id)
                return {"error": "Post not found"}
            post_user_id, chat_id, rql = post[0]
            if post_user_id != user_id and self.user_manager.get_user_role(user_id) != 'admin':
                log.info("Пользователь user_id=%d не имеет прав для редактирования post_id=%d", user_id, post_id)
                error_result = self.add_post(
                    chat_id, 2, f"Permission denied: User {user_id} cannot edit post_id={post_id} owned by user {post_user_id}",
                    rql, post_id
                )
                if error_result.get("error"):
                    log.warn("Не удалось сохранить сообщение об ошибке доступа: %s", error_result["error"])
                return {"error": "Permission denied"}
            self.posts_table.update(
                conditions={'id': post_id},
                values={'message': message, 'timestamp': int(time.time()), 'rql': rql}
            )
            self.add_change(chat_id, post_id, "edit")
            log.debug("Отредактировано сообщение post_id=%d для user_id=%d", post_id, user_id)
            return {"status": "ok"}
        except Exception as e:
            log.excpt("Ошибка редактирования сообщения post_id=%d: ", post_id, e=e)
            return {"error": str(e)}

    def delete_post(self, post_id: int, user_id: int) -> dict:
        """Удаляет пост, если пользователь имеет права.

        Args:
            post_id (int): ID поста.
            user_id (int): ID пользователя.

        Returns:
            dict: {'status': 'ok'} или {'error': str}
        """
        try:
            post = self.posts_table.select_from(
                columns=['user_id', 'chat_id', 'rql'],
                conditions=[('id', '=', post_id)]
            )
            if not post:
                log.info("Сообщение post_id=%d не найдено", post_id)
                return {"error": "Post not found"}
            post_user_id, chat_id, rql = post[0]
            if post_user_id != user_id and self.user_manager.get_user_role(user_id) != 'admin':
                log.info("Пользователь user_id=%d не имеет прав для удаления post_id=%d", user_id, post_id)
                return {"error": "Permission denied"}
            self.posts_table.delete_from(conditions={'id': post_id})
            self.add_change(chat_id, post_id, "delete")
            log.debug("Удалено сообщение post_id=%d для user_id=%d", post_id, user_id)
            return {"status": "ok"}
        except Exception as e:
            log.excpt("Ошибка удаления сообщения post_id=%d: ", post_id, e=e)
            return {"error": str(e)}