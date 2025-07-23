# /app/agent/managers/posts.py, updated 2025-07-22 11:24 EEST
import time
import re
import asyncio
from managers.db import Database, DataTable
import globals
from lib.basic_logger import BasicLogger

log = globals.get_logger("postman")

class PostManager:
    def __init__(self, user_manager):
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

    def add_change(self, chat_id, post_id, action):
        """Добавляет post_id в changes_history. Для удалений использует -post_id."""
        if chat_id not in self.changes_history:
            self.changes_history[chat_id] = []
        effective_post_id = -post_id if action == "delete" else post_id
        self.changes_history[chat_id].append(effective_post_id)
        log.debug("Added change for chat_id=%d, post_id=%d, action=%s", chat_id, effective_post_id, action)

    def get_changes(self, chat_id):
        """Возвращает changes_history для указанного chat_id без очистки."""
        changes = self.changes_history.get(chat_id, [])
        if changes:
            log.debug("Retrieved changes for chat_id=%d: ~C95%s~C00", chat_id, str(changes))
        return changes

    def clear_changes(self, chat_id):
        """Очищает changes_history для указанного chat_id."""
        if chat_id in self.changes_history:
            log.debug("Cleared changes for chat_id=%d", chat_id)
            self.changes_history[chat_id] = []

    def get_quotes(self, history):
        """Извлекает все quote_id из history и возвращает словарь цитат из quotes_table."""
        try:
            quote_ids = set()
            for post in history:
                if post["message"]:
                    matches = re.findall(r'@quote#(\d+)', post["message"])
                    quote_ids.update(int(qid) for qid in matches)
            quotes = {}
            for quote_id in quote_ids:
                row = globals.post_processor.quotes_table.select_from(
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
                        "message": row[0][3],  # Используем content как message
                        "timestamp": row[0][4],
                        "user_name": user_name
                    }
            log.debug("Extracted quotes for chat_id=%d: %s", history[0]["chat_id"] if history else 0, str(quotes))
            return quotes
        except Exception as e:
            log.excpt("Ошибка извлечения цитат: %s", str(e), exc_info=(type(e), e, e.__traceback__))
            return {}

    def add_message(self, chat_id, user_id, message, rql=None):
        try:
            timestamp = int(time.time())
            user_row = self.users_table.select_row(
                conditions={'user_id': user_id},
                columns=['llm_class', 'user_name']
            )
            is_llm = user_row and user_row[0] is not None
            user_name = user_row[1] if user_row else 'unknown'
            result = None
            agent_message = None
            has_code_file = False
            message = message.strip()
            if not is_llm:
                try:
                    result = globals.post_processor.process_response(chat_id, user_id, message)
                    log.debug("Результат process_response: handled_cmds=%d, failed_cmds=%d, processed_msg=%s",
                              result["handled_cmds"], result["failed_cmds"], result["processed_msg"][:50])
                    if isinstance(result, dict):
                        processed_message = result.get("processed_msg", message)
                        agent_message = result.get("agent_reply")
                        has_code_file = result.get("has_code_file", False)
                        if result["handled_cmds"] == 0 and result["failed_cmds"] > 0:
                            log.warn("post_processor: no commands handled, %d failed", result["failed_cmds"])
                            agent_message = f"@{user_name} {agent_message or 'Unknown error'}"
                    else:
                        raise ValueError("Unexpected process_response result type: %s" % type(result))
                except Exception as e:
                    log.excpt("Ошибка обработки сообщения в post_processor: %s", str(e),
                              exc_info=(type(e), e, e.__traceback__))
                    processed_message = message
                    agent_message = f"@{user_name} Error processing message: {str(e)}"
            else:
                processed_message = message
            self.posts_table.insert_into({
                'chat_id': chat_id,
                'user_id': user_id,
                'message': processed_message,
                'timestamp': timestamp,
                'rql': rql
            })
            post_id_row = self.posts_table.select_row(
                columns=['last_insert_rowid()']
            )
            post_id = post_id_row[0] if post_id_row else None
            if post_id is None:
                raise ValueError("Failed to retrieve last_insert_rowid()")
            self.add_change(chat_id, post_id, "add")
            log.debug("Добавлено сообщение post_id=%d, chat_id=%d, user_id=%d, rql=%s, message=%s",
                      post_id, chat_id, user_id, str(rql), processed_message[:50])

            if not is_llm and agent_message:
                self.posts_table.insert_into({
                    'chat_id': chat_id,
                    'user_id': 2,
                    'message': agent_message,
                    'timestamp': timestamp + 1,
                    'rql': rql
                })
                agent_post_id_row = self.posts_table.select_row(
                    columns=['last_insert_rowid()']
                )
                agent_post_id = agent_post_id_row[0] if agent_post_id_row else None
                if agent_post_id is None:
                    raise ValueError("Failed to retrieve last_insert_rowid() for agent message")
                self.add_change(chat_id, agent_post_id, "add")
                log.debug("Добавлен ответ агента post_id=%d, chat_id=%d, rql=%s, message=%s",
                          agent_post_id, chat_id, str(rql), agent_message[:50])
                if has_code_file and result.get("handled_cmds", 0) > 0:
                    self.edit_post(post_id, processed_message, user_id)
                    log.debug("Обновлён пост post_id=%d с processed_message=%s", post_id, processed_message[:50])
            if not is_llm and globals.replication_manager and not message.startswith('@agent'):
                log.debug("Triggering replication for post_id=%d, chat_id=%d, user_id=%d", post_id, chat_id, user_id)
                try:
                    loop = asyncio.get_event_loop()
                    loop.create_task(self.trigger_replication(chat_id, post_id))
                except Exception as e:
                    log.excpt("Ошибка запуска репликации для post_id=%d, chat_id=%d: %s",
                              post_id, chat_id, str(e), exc_info=(type(e), e, e.__traceback__))
                    self.posts_table.insert_into({
                        'chat_id': chat_id,
                        'user_id': 2,
                        'message': f"@{user_name} Failed to trigger replication: {str(e)}",
                        'timestamp': int(time.time()),
                        'rql': rql
                    })
                    error_post_id_row = self.posts_table.select_row(
                        columns=['last_insert_rowid()']
                    )
                    error_post_id = error_post_id_row[0] if error_post_id_row else None
                    if error_post_id is None:
                        raise ValueError("Failed to retrieve last_insert_rowid() for error message")
                    self.add_change(chat_id, error_post_id, "add")
                    log.debug("Added error message to chat_id=%d for user_id=2", chat_id)
            else:
                log.debug("Skipping replication for post_id=%d, chat_id=%d, user_id=%d, is_llm=%s, starts_with_@agent=%s",
                          post_id, chat_id, user_id, str(is_llm), str(message.startswith('@agent')))
            return {"status": "ok", "post_id": post_id}
        except Exception as e:
            log.excpt("Ошибка добавления сообщения для chat_id=%d, user_id=%d: %s",
                      chat_id, user_id, str(e), exc_info=(type(e), e, e.__traceback__))
            return {"error": str(e)}

    async def trigger_replication(self, chat_id, post_id):
        try:
            log.debug("Running replication for chat_id=%d, post_id=%d", chat_id, post_id)
            await globals.replication_manager.replicate_to_llm(chat_id)
            log.debug("Replication completed for chat_id=%d, post_id=%d", chat_id, post_id)
        except Exception as e:
            log.excpt("Ошибка репликации для chat_id=%d, post_id=%d: %s",
                      chat_id, post_id, str(e), exc_info=(type(e), e, e.__traceback__))
            user_row = self.users_table.select_row(
                conditions={'user_id': 2},
                columns=['user_name']
            )
            user_name = user_row[0] if user_row else 'agent'
            self.posts_table.insert_into({
                'chat_id': chat_id,
                'user_id': 2,
                'message': f"@{user_name} Replication error: {str(e)}",
                'timestamp': int(time.time()),
                'rql': None
            })
            error_post_id_row = self.posts_table.select_row(
                columns=['last_insert_rowid()']
            )
            error_post_id = error_post_id_row[0] if error_post_id_row else None
            if error_post_id is None:
                raise ValueError("Failed to retrieve last_insert_rowid() for replication error")
            self.add_change(chat_id, error_post_id, "add")
            log.debug("Added replication error message to chat_id=%d for user_id=2", chat_id)

    def get_history(self, chat_id, only_changes=False):
        try:
            changes = self.get_changes(chat_id)
            if only_changes and not changes:
                # NO_LOG!
                return {"chat_history": "no changes"}

            history = []
            post_ids = [abs(pid) for pid in changes if pid > 0] if only_changes else None
            deleted_ids = [-pid for pid in changes if pid < 0] if only_changes else []
            hierarchy = globals.chat_manager.get_chat_hierarchy(chat_id)

            if only_changes:
                for deleted_id in deleted_ids:
                    history.append({
                        "id": deleted_id,
                        "chat_id": chat_id,
                        "user_id": None,
                        "message": None,
                        "timestamp": int(time.time()),
                        "rql": None,
                        "user_name": None,
                        "action": "delete"
                    })

            for c_id in hierarchy:
                query = f"SELECT p.id, p.chat_id, p.user_id, p.message, p.timestamp, u.user_name, p.rql FROM posts p " \
                        f"JOIN users u ON p.user_id = u.user_id WHERE p.chat_id = :chat_id"
                params = {'chat_id': c_id}
                if post_ids:
                    if len(post_ids) == 1:
                        query += " AND p.id = :post_id"
                        params['post_id'] = post_ids[0]
                    else:
                        query += f" AND p.id IN ({','.join([':pid' + str(i) for i in range(len(post_ids))])})"
                        for i, pid in enumerate(post_ids):
                            params[f'pid{i}'] = pid
                query += " ORDER BY p.id"
                posts = self.db.fetch_all(query, params)
                for post in posts:
                    action = "delete" if post[0] in deleted_ids else "add"
                    message = None if action == "delete" else post[3]
                    history.append({
                        "id": post[0],
                        "chat_id": post[1],
                        "user_id": post[2],
                        "message": message,
                        "timestamp": post[4],
                        "rql": post[6],
                        "user_name": post[5],
                        "action": action
                    })
            if history and only_changes:
                self.clear_changes(chat_id)
            log.debug("Получена история для chat_id=%d: %d сообщений, only_changes=%s",
                      chat_id, len(history), str(only_changes))
            return history
        except Exception as e:
            log.excpt("Ошибка получения истории для chat_id=%d: %s", chat_id, str(e),
                      exc_info=(type(e), e, e.__traceback__))
            return {"error": str(e)}

    def get_post(self, post_id):
        row = self.posts_table.select_from(
            conditions={'id': post_id},
            columns=['id', 'chat_id', 'user_id', 'message', 'timestamp', 'rql']
        )
        return {
            'id': row[0][0],
            'chat_id': row[0][1],
            'user_id': row[0][2],
            'message': row[0][3],
            'timestamp': row[0][4],
            'rql': row[0][5]
        } if row else None

    def edit_post(self, post_id, message, user_id):
        try:
            post = self.posts_table.select_from(
                columns=['user_id', 'chat_id', 'rql'],
                conditions={'id': post_id}
            )
            if not post:
                log.info("Сообщение post_id=%d не найдено", post_id)
                return {"error": "Post not found"}
            post_user_id, chat_id, rql = post[0]
            if post_user_id != user_id and self.user_manager.get_user_role(user_id) != 'admin':
                log.info("Пользователь user_id=%d не имеет прав для редактирования post_id=%d", user_id, post_id)
                self.posts_table.insert_into({
                    'chat_id': chat_id,
                    'user_id': 2,
                    'message': f"Permission denied: User {user_id} cannot edit post_id={post_id} owned by user {post_user_id}",
                    'timestamp': int(time.time()),
                    'rql': rql
                })
                error_post_id_row = self.posts_table.select_row(
                    columns=['last_insert_rowid()']
                )
                error_post_id = error_post_id_row[0] if error_post_id_row else None
                if error_post_id is None:
                    raise ValueError("Failed to retrieve last_insert_rowid() for permission error")
                self.add_change(chat_id, error_post_id, "add")
                log.debug("Added permission error message to chat_id=%d for user_id=2", chat_id)
                return {"error": "Permission denied"}
            self.posts_table.update(
                conditions={'id': post_id},
                values={'message': message, 'timestamp': int(time.time()), 'rql': rql}
            )
            self.add_change(chat_id, post_id, "edit")
            log.debug("Отредактировано сообщение post_id=%d для user_id=%d", post_id, user_id)
            return {"status": "ok"}
        except Exception as e:
            log.excpt("Ошибка редактирования сообщения post_id=%d: %s", post_id, str(e),
                      exc_info=(type(e), e, e.__traceback__))
            return {"error": str(e)}

    def delete_post(self, post_id, user_id):
        try:
            post = self.posts_table.select_from(
                columns=['user_id', 'chat_id', 'rql'],
                conditions={'id': post_id}
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
            log.excpt("Ошибка удаления сообщения post_id=%d: %s", post_id, str(e),
                      exc_info=(type(e), e, e.__traceback__))
            return {"error": str(e)}
