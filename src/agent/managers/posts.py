# /agent/managers/posts.py, updated 2025-07-17 20:58 EEST
import time
import logging
import re
import asyncio
import traceback
from managers.db import Database, DataTable
import globals

logging.basicConfig(level=logging.DEBUG, format='%(asctime)s #%(levelname)s: %(message)s')

class PostManager:
    def __init__(self, user_manager):
        self.user_manager = user_manager
        self.db = Database.get_database()
        self.changes_history = {}  # Словарь для хранения изменённых post_id по chat_id
        self.posts_table = DataTable(
            table_name="posts",
            template=[
                "id INTEGER PRIMARY KEY AUTOINCREMENT",
                "chat_id INTEGER",
                "user_id INTEGER",
                "message TEXT",
                "timestamp INTEGER",
                "FOREIGN KEY (chat_id) REFERENCES chats(id)",
                "FOREIGN KEY (user_id) REFERENCES users(id)"
            ]
        )

    def add_change(self, chat_id, post_id, action):
        """Добавляет post_id в changes_history. Для удалений использует -post_id."""
        if chat_id not in self.changes_history:
            self.changes_history[chat_id] = []
        effective_post_id = -post_id if action == "delete" else post_id
        self.changes_history[chat_id].append(effective_post_id)
        logging.debug(f"Added change for chat_id={chat_id}, post_id={effective_post_id}")

    def get_changes(self, chat_id):
        """Возвращает changes_history для указанного chat_id и очищает его."""
        changes = self.changes_history.get(chat_id, [])
        self.changes_history[chat_id] = []
        if changes:  # Логируем только непустые изменения
            logging.debug(f"Retrieved and cleared changes for chat_id={chat_id}: {changes}")
        return changes

    def add_message(self, chat_id, user_id, message):
        try:
            timestamp = int(time.time())
            # Проверяем, является ли сообщение командой @agent
            user_row = self.db.fetch_one('SELECT llm_class, user_name FROM users WHERE user_id = :user_id', {'user_id': user_id})
            is_llm = user_row and user_row[0] is not None
            user_name = user_row[1] if user_row else 'unknown'
            processed_message = message
            agent_message = None
            if not is_llm and message.strip().startswith('@agent') and message.strip() != '@agent':
                try:
                    processed_message = globals.post_processor.process_response(chat_id, user_id, message)
                    if not processed_message:
                        processed_message = message  # Если процессор не вернул текст, сохраняем исходное сообщение
                except Exception as e:
                    logging.error(f"Ошибка обработки сообщения в post_processor: {str(e)}")
                    processed_message = message  # Сохраняем исходное сообщение
                    agent_message = f"@{user_name} Error processing message: {str(e)}"
            # Сохраняем обработанное сообщение пользователя
            self.posts_table.insert_into({
                'chat_id': chat_id,
                'user_id': user_id,
                'message': processed_message,
                'timestamp': timestamp
            })
            post_id = self.db.fetch_one('SELECT last_insert_rowid()')[0]
            self.add_change(chat_id, post_id, "add")
            logging.debug(f"Добавлено сообщение post_id={post_id}, chat_id={chat_id}, user_id={user_id}, message={processed_message[:50]}...")

            # Публикуем ответ от агента, если он есть
            if not is_llm and message.strip().startswith('@agent') and message.strip() != '@agent':
                final_agent_message = agent_message or f"@{user_name} {processed_message}"
                if processed_message != message or agent_message:  # Публикуем только при успешной обработке или ошибке
                    self.posts_table.insert_into({
                        'chat_id': chat_id,
                        'user_id': 2,  # Агент
                        'message': final_agent_message,
                        'timestamp': timestamp + 1
                    })
                    agent_post_id = self.db.fetch_one('SELECT last_insert_rowid()')[0]
                    self.add_change(chat_id, agent_post_id, "add")
                    logging.debug(f"Добавлен ответ агента post_id={agent_post_id}, chat_id={chat_id}, message={final_agent_message[:50]}...")
            # Запускаем репликацию, если сообщение не начинается с @agent
            if not is_llm and globals.replication_manager and not message.strip().startswith('@agent'):
                logging.debug(f"Triggering replication for post_id={post_id}, chat_id={chat_id}, user_id={user_id}")
                try:
                    loop = asyncio.get_event_loop()
                    loop.create_task(self.trigger_replication(chat_id, post_id))
                except Exception as e:
                    logging.error(f"Ошибка запуска репликации для post_id={post_id}, chat_id={chat_id}: {str(e)}")
                    self.posts_table.insert_into({
                        'chat_id': chat_id,
                        'user_id': 2,  # agent
                        'message': f"@{user_name} Failed to trigger replication: {str(e)}",
                        'timestamp': int(time.time())
                    })
                    self.add_change(chat_id, self.db.fetch_one('SELECT last_insert_rowid()')[0], "add")
                    logging.debug(f"Added error message to chat_id={chat_id} for user_id=2")
            else:
                logging.debug(f"Skipping replication for post_id={post_id}, chat_id={chat_id}, user_id={user_id}, is_llm={is_llm}, starts_with_@agent={message.strip().startswith('@agent')}")
            return {"status": "ok", "post_id": post_id}
        except Exception as e:
            logging.error(f"Ошибка добавления сообщения для chat_id={chat_id}, user_id={user_id}: {str(e)}")
            return {"error": str(e)}

    async def trigger_replication(self, chat_id, post_id):
        try:
            logging.debug(f"Running replication for chat_id={chat_id}, post_id={post_id}")
            await globals.replication_manager.replicate_to_llm(chat_id)
            logging.debug(f"Replication completed for chat_id={chat_id}, post_id={post_id}")
        except Exception as e:
            logging.error(f"Ошибка репликации для chat_id={chat_id}, post_id={post_id}: {str(e)}")
            user_row = self.db.fetch_one('SELECT user_name FROM users WHERE user_id = :user_id', {'user_id': 2})
            user_name = user_row[0] if user_row else 'agent'
            self.posts_table.insert_into({
                'chat_id': chat_id,
                'user_id': 2,  # agent
                'message': f"@{user_name} Replication error: {str(e)}",
                'timestamp': int(time.time())
            })
            self.add_change(chat_id, self.db.fetch_one('SELECT last_insert_rowid()')[0], "add")
            logging.debug(f"Added replication error message to chat_id={chat_id} for user_id=2")
            traceback.print_exc()

    def get_history(self, chat_id, only_changes=False):
        try:
            changes = self.get_changes(chat_id)
            if only_changes and not changes:
                return {"chat_history": "no changes"}

            history = []
            post_ids = [abs(pid) for pid in changes if pid > 0] if only_changes else None
            deleted_ids = [-pid for pid in changes if pid < 0] if only_changes else []
            hierarchy = globals.chat_manager.get_chat_hierarchy(chat_id)

            # Обрабатываем удалённые посты из changes_history
            if only_changes:
                for deleted_id in deleted_ids:
                    history.append({
                        "id": deleted_id,
                        "chat_id": chat_id,
                        "user_id": None,
                        "message": None,
                        "timestamp": int(time.time()),
                        "file_names": [],
                        "user_name": None,
                        "action": "delete"
                    })

            # Обрабатываем существующие посты
            for c_id in hierarchy:
                query = f"SELECT p.id, p.chat_id, p.user_id, p.message, p.timestamp, u.user_name FROM posts p JOIN users u ON p.user_id = u.user_id WHERE p.chat_id = :chat_id"
                params = {'chat_id': c_id}
                if post_ids:
                    if len(post_ids) == 1:
                        query += " AND p.id = :post_id"
                        params['post_id'] = post_ids[0]
                    else:
                        query += f" AND p.id IN ({','.join([':pid' + str(i) for i in range(len(post_ids))])})"
                        for i, pid in enumerate(post_ids):
                            params[f'pid{i}'] = pid
                query += " ORDER BY p.id"  # Сортировка по id вместо timestamp
                posts = self.db.fetch_all(query, params)
                for post in posts:
                    action = "delete" if post[0] in deleted_ids else "add"
                    message = None if action == "delete" else post[3]
                    file_ids = re.findall(r'@attach#(\d+)', message or "")
                    file_names = []
                    for file_id in file_ids:
                        file_data = self.db.fetch_one(
                            'SELECT file_name, ts FROM attached_files WHERE id = :file_id',
                            {'file_id': file_id}
                        )
                        if file_data:
                            file_names.append({"file_id": int(file_id), "file_name": file_data[0].lstrip('@'), "ts": file_data[1]})
                    history.append({
                        "id": post[0],
                        "chat_id": post[1],
                        "user_id": post[2],
                        "message": message,
                        "timestamp": post[4],
                        "file_names": file_names,
                        "user_name": post[5],
                        "action": action
                    })
            logging.debug(f"Returning history for chat_id={chat_id}: {len(history)} posts, only_changes={only_changes}")
            return history
        except Exception as e:
            logging.error(f"Ошибка получения истории для chat_id={chat_id}: {str(e)}")
            traceback.print_exc()
            return {"error": str(e)}

    def get_post(self, post_id):
        row = self.posts_table.select_from(
            conditions={'id': post_id},
            columns=['id', 'chat_id', 'user_id', 'message', 'timestamp'],
            limit=1
        )
        return {
            'id': row[0][0],
            'chat_id': row[0][1],
            'user_id': row[0][2],
            'message': row[0][3],
            'timestamp': row[0][4]
        } if row else None

    def edit_post(self, post_id, message, user_id):
        try:
            post = self.posts_table.select_from(
                conditions={'id': post_id},
                columns=['user_id', 'chat_id'],
                limit=1
            )
            if not post:
                logging.info(f"Сообщение post_id={post_id} не найдено")
                return {"error": "Post not found"}
            post_user_id, chat_id = post[0]
            if post_user_id != user_id and self.user_manager.get_user_role(user_id) != 'admin':
                logging.info(f"Пользователь user_id={user_id} не имеет прав для редактирования post_id={post_id}")
                self.posts_table.insert_into({
                    'chat_id': chat_id,
                    'user_id': 2,  # agent
                    'message': f"Permission denied: User {user_id} cannot edit post_id={post_id} owned by user {post_user_id}",
                    'timestamp': int(time.time())
                })
                self.add_change(chat_id, self.db.fetch_one('SELECT last_insert_rowid()')[0], "add")
                logging.debug(f"Added permission error message to chat_id={chat_id} for user_id=2")
                return {"error": "Permission denied"}
            self.posts_table.update(
                conditions={'id': post_id},
                values={'message': message, 'timestamp': int(time.time())}
            )
            self.add_change(chat_id, post_id, "edit")
            logging.debug(f"Отредактировано сообщение post_id={post_id} от user_id={user_id}")
            return {"status": "ok"}
        except Exception as e:
            logging.error(f"Ошибка редактирования сообщения post_id={post_id}: {str(e)}")
            return {"error": str(e)}

    def delete_post(self, post_id, user_id):
        try:
            post = self.posts_table.select_from(
                conditions={'id': post_id},
                columns=['user_id', 'chat_id'],
                limit=1
            )
            if not post:
                logging.info(f"Сообщение post_id={post_id} не найдено")
                return {"error": "Post not found"}
            post_user_id, chat_id = post[0]
            if post_user_id != user_id and self.user_manager.get_user_role(user_id) != 'admin':
                logging.info(f"Пользователь user_id={user_id} не имеет прав для удаления post_id={post_id}")
                return {"error": "Permission denied"}
            self.posts_table.delete_from(conditions={'id': post_id})
            self.add_change(chat_id, post_id, "delete")
            logging.debug(f"Удалено сообщение post_id={post_id} для user_id={user_id}")
            return {"status": "ok"}
        except Exception as e:
            logging.error(f"Ошибка удаления сообщения post_id={post_id}: {str(e)}")
            return {"error": str(e)}