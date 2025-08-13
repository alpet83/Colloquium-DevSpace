# /app/agent/managers/replication.py, updated 2025-07-27 14:00 EEST
import re
import asyncio
import datetime
import time
from pathlib import Path
from llm_interactor import LLMInteractor
from managers.db import Database, DataTable
from chat_actor import ChatActor
import globals

log = globals.get_logger("replication")

class ReplicationManager(LLMInteractor):
    """Управляет репликацией сообщений между LLM-актёрами."""
    def __init__(self, debug_mode: bool = False):
        """Инициализирует ReplicationManager с настройкой режима отладки.

        Args:
            debug_mode (bool, optional): Включает режим отладки. Defaults to False.
        """
        super().__init__()
        self.debug_mode = debug_mode
        self.actors = self._load_actors()
        self.active_replications = set()
        self.processing_state = "free"
        self.processing_actor = None
        self.processing_start = None
        if debug_mode:
            log.debug("Режим отладки репликации включён")
        else:
            log.debug("Репликация активирована")

    def _load_actors(self) -> list:
        """Загружает список актёров из таблицы users.

        Returns:
            list: Список объектов ChatActor.
        """
        actors = []
        rows = self.db.fetch_all('SELECT user_id, user_name, llm_class, llm_token FROM users')
        log.debug("Загружено %d актёров из таблицы users: ~C95%s~C00", len(rows),
                  str([(row[0], row[1]) for row in rows]))
        for row in rows:
            actor = ChatActor(row[0], row[1], row[2], row[3], globals.post_manager)
            actors.append(actor)
        return actors

    def check_start_replication(self, chat_id: int, post_id: int,
                                      user_id: int, message: str, rql: int = 0) -> bool:
        """Проверяет условия и запускает репликацию, если rql=0 и есть триггер (@username или @all).

        Args:
            chat_id (int): ID чата.
            post_id (int): ID поста.
            user_id (int): ID пользователя.
            message (str): Текст сообщения.
            rql (int, optional): Уровень рекурсии диалога. Defaults to 0.
        """
        log.debug("Проверка репликации: chat_id=%d, post_id=%d, user_id=%d, rql=%d, message=%s",
                  chat_id, post_id, user_id, rql, message[:50])
        if rql > 0:
            log.debug("Репликация не запущена для post_id=%d, chat_id=%d: rql=%d (требуется 0)",
                      post_id, chat_id, rql)
            return False

        # Проверяем наличие @username (LLM-актёры) или @all
        llm_usernames = [actor.user_name for actor in self.actors if actor.llm_connection and actor.user_id > 1]
        log.debug("LLM-актёры для проверки триггеров: %s", llm_usernames)
        pattern = '|'.join([f'@{re.escape(username)}' for username in llm_usernames] + ['@all'])
        if not re.search(pattern, message, re.IGNORECASE):
            log.debug("Репликация не запущена для post_id=%d, chat_id=%d: нет триггера (@username или @all)",
                      post_id, chat_id)
            return False

        try:
            log.debug("Запуск репликации для post_id=%d, chat_id=%d, user_id=%d", post_id, chat_id, user_id)
            loop = asyncio.get_event_loop()
            loop.create_task(self.replicate_to_llm(chat_id))
            log.debug("Репликация запланирована для post_id=%d, chat_id=%d", post_id, chat_id)
            return True
        except Exception as e:
            log.excpt("Ошибка запуска репликации для post_id=%d, chat_id=%d: %s", post_id, chat_id, e=e)
            user_name = globals.user_manager.get_user_name(user_id) or 'unknown'
            error_result = globals.post_manager.save_message(
                chat_id, 2, f"@{user_name} Replication check error: {str(e)}", 0, post_id
            )
            if error_result.get("error"):
                log.warn("Не удалось сохранить сообщение об ошибке проверки репликации: %s", error_result["error"])
            else:
                log.debug("Добавлено сообщение об ошибке проверки репликации в chat_id=%d для user_id=2", chat_id)
            return False

    async def _recursive_replicate(self, content_blocks: list, users: list, chat_id: int, actor: ChatActor,
                                   exclude_source_id: int = None, rql: int = 1, max_rql: int = 5):
        """Рекурсивно обрабатывает ответы LLM, добавляя посты и вызывая репликацию для других участников.

        Args:
            content_blocks (list): Список блоков контента.
            users (list): Список пользователей.
            chat_id (int): ID чата.
            actor (ChatActor): Актёр, выполняющий репликацию.
            exclude_source_id (int, optional): ID пользователя для исключения.
            rql (int, optional): Уровень рекурсии диалога. Defaults to 1.
            max_rql (int, optional): Максимальный уровень рекурсии. Defaults to 5.
        """
        if rql > max_rql:
            log.info("Достигнут предел рекурсивного диалога %d для actor_id=%d, chat_id=%d", max_rql, actor.user_id,
                     chat_id)
            return

        self.processing_state = "busy"
        self.processing_actor = actor.user_name
        self.processing_start = int(datetime.datetime.now(datetime.UTC).timestamp())
        log.debug("Начался рекурсивный диалог для actor_id=%d (%s), chat_id=%d, rql=%d",
                  actor.user_id, actor.user_name, chat_id, rql)
        t_start = time.time()
        try:
            original_response = await self.interact(content_blocks, users, chat_id, actor, self.debug_mode, rql)
            elapsed = time.time() - t_start
            if original_response:
                log.debug("Ответ получен после %.1f секунд, проверка возможности обработки агентом...", elapsed)
                # Вырезаем всё до @agent, если присутствует
                prefix = ''
                agent_command = original_response
                if '@agent' in original_response:
                    split_index = original_response.index('@agent')
                    prefix = original_response[:split_index]
                    agent_command = original_response[split_index:]
                    log.debug("Вырезан префикс до @agent: prefix=%s, agent_command=%s",
                              prefix[:50], agent_command[:50])

                # Определяем reply_to для ответа
                reply_to = None
                cite_match = re.search(r'@post#(\d+)', original_response)
                if cite_match:
                    cited_id_or_timestamp = int(cite_match.group(1))
                    if cited_id_or_timestamp < 1_000_000:  # post_id
                        cited_post = self.db.fetch_one(
                            'SELECT id FROM posts WHERE chat_id = :chat_id AND id = :post_id',
                            {'chat_id': chat_id, 'post_id': cited_id_or_timestamp}
                        )
                        if cited_post:
                            reply_to = cited_id_or_timestamp
                            log.debug("Найдено цитирование @post#%d (post_id), установлен reply_to=%d",
                                      cited_id_or_timestamp, reply_to)
                        else:
                            log.warn("Цитируемый пост @post#%d (post_id) не найден", cited_id_or_timestamp)
                    else:  # timestamp
                        log.debug("Цитирование @post#%d (timestamp) игнорируется для reply_to", cited_id_or_timestamp)
                if reply_to is None and actor.user_id > 1:
                    last_post = self.db.fetch_one(
                        'SELECT id FROM posts WHERE chat_id = :chat_id ORDER BY id DESC LIMIT 1',
                        {'chat_id': chat_id}
                    )
                    reply_to = last_post[0] if last_post else None
                    log.debug("Установлен reply_to=%s на основе последнего поста", str(reply_to))

                # Сохраняем ответ модели через add_message
                response_result = globals.post_manager.add_message(
                    chat_id, actor.user_id, prefix + agent_command, rql, reply_to
                )
                if response_result.get("error"):
                    log.warn("Не удалось сохранить ответ модели для actor_id=%d, chat_id=%d: %s",
                             actor.user_id, chat_id, response_result["error"])
                    return

                processed_msg = response_result.get("processed_msg", "")
                agent_reply = response_result.get("agent_reply")
                query_filter = ""
                if agent_reply:
                    query_filter = "AND user_id = 2"
                    log.debug("Добавлен ответ агента для chat_id=%d: %s", chat_id, agent_reply[:50])
                if processed_msg and rql < max_rql:
                    latest_post = self.db.fetch_one(
                        f"SELECT user_id, message FROM posts\n WHERE chat_id = :chat_id {query_filter}\n ORDER BY id DESC LIMIT 1",
                        {'chat_id': chat_id}
                    )
                    latest_post = latest_post[1].strip().lower() if latest_post else ""

                    for next_actor in self.actors:
                        ref = f"@{next_actor.user_name} "
                        if ('@all ' in latest_post) or (ref in latest_post) and (next_actor.user_id > 2) and next_actor.llm_connection:
                            # Выбрать всех доступных других LLM-актёров
                            file_ids = set()
                            file_map = {}
                            new_content_blocks = self.assemble_posts(chat_id, exclude_source_id, file_ids, file_map)
                            new_content_blocks.extend(self.assemble_files(file_ids, file_map))
                            log.debug("Продолжение рекурсивного диалога между %s и %s для rql=%d",
                                      actor.user_name, next_actor.user_name, rql + 1)
                            await self._recursive_replicate(
                                new_content_blocks, users, chat_id, next_actor, exclude_source_id,
                                rql + 1, max_rql
                            )
            else:
                log.warn("Ответ от LLM не получен для user_id=%d, rql=%d: %s", actor.user_id, rql, str(original_response))
                globals.post_manager.add_message(
                    chat_id, 2, f"Нет ответа от LLM для {actor.user_name}, время запроса {elapsed} сек.", rql, None
                )
        finally:
            self.processing_state = "free"
            self.processing_actor = None
            self.processing_start = None

    async def _pack_and_send(self, content_blocks: list, users: list, chat_id: int, exclude_source_id: int = None,
                             debug_mode: bool = False):
        """Отправляет контент всем LLM-актёрам при @all и RQL <= 1, иначе по триггерам.

        Args:
            content_blocks (list): Список блоков контента.
            users (list): Список пользователей.
            chat_id (int): ID чата.
            exclude_source_id (int, optional): ID пользователя для исключения.
            debug_mode (bool, optional): Режим отладки. Defaults to False.
        """
        log.debug("Запуск _pack_and_send для chat_id=%d, блоков=%d", chat_id, len(content_blocks))

        latest_post = self.db.fetch_one(
            'SELECT user_id, message, id, rql FROM posts WHERE chat_id = :chat_id ORDER BY id DESC LIMIT 1',
            {'chat_id': chat_id}
        )
        if latest_post and latest_post[0] == 2 and "Permission denied" in latest_post[1]:
            if not (re.search(r'@grok\S*|@all', latest_post[1], re.IGNORECASE)):
                log.debug("Пропуск диалога для chat_id=%d из-за ошибки агента без @grok или @all: %s",
                          chat_id, latest_post[1][:50])
                return

        llm_actors = [actor for actor in self.actors if actor.llm_connection and actor.user_id > 1]
        log.debug("Найдено %d LLM-актёров (user_id > 1): ~C95%s~C00", len(llm_actors),
                  str([actor.user_id for actor in llm_actors]))
        processed_actors = set()

        # Проверяем триггер @all перед циклом
        is_broadcast = False
        rql = 0
        triggered_by = None
        if latest_post:
            message = latest_post[1].lower()
            rql = latest_post[3] if latest_post[3] is not None else 0
            triggered_by = latest_post[2]
            if '@all' in message and rql <= 1:
                is_broadcast = True
                log.debug("Обнаружен триггер @all для широковещательной репликации: chat_id=%d, rql=%d", chat_id, rql)

        for actor in llm_actors:
            if actor.user_id in processed_actors:
                log.debug("Пропуск дубликата actor_id=%d", actor.user_id)
                continue
            if exclude_source_id and actor.user_id == exclude_source_id:
                log.debug("Пропуск actor_id=%d из-за exclude_source_id", actor.user_id)
                continue
            if latest_post and latest_post[0] == actor.user_id:
                log.debug("Пропуск диалога для actor_id=%d: последнее сообщение от этого актёра", actor.user_id)
                continue
            should_respond = False
            user_name = globals.user_manager.get_user_name(actor.user_id)
            if is_broadcast:
                should_respond = True
                log.debug("Широковещательная репликация для actor_id=%d, chat_id=%d, rql=%d", actor.user_id, chat_id,
                          rql)
            elif latest_post:
                message = latest_post[1]
                if re.search(rf"@{user_name}\s+", message):
                    should_respond = True
                    log.debug("Триггер ответа для actor_id=%d: message=%s", actor.user_id, message[:50])
            if should_respond:
                file_ids = set()
                file_map = {}
                new_content_blocks = self.assemble_posts(chat_id, exclude_source_id, file_ids, file_map)
                new_content_blocks.extend(self.assemble_files(file_ids, file_map))
                log.debug("Начался диалог между %s и %s для chat_id=%d",
                          globals.user_manager.get_user_name(latest_post[0]) if latest_post else "Unknown",
                          actor.user_name, chat_id)
                await self._recursive_replicate(new_content_blocks, users, chat_id, actor, exclude_source_id, rql + 1)
                processed_actors.add(actor.user_id)
            else:
                log.debug("Пропуск actor_id=%d: нет триггера для ответа", actor.user_id)

        max_post_id = max([block.post_id for block in content_blocks if block.post_id is not None] or [0])
        if max_post_id:
            for actor in llm_actors:
                if actor.user_id in processed_actors:
                    continue
                if exclude_source_id and actor.user_id == exclude_source_id:
                    continue
                if latest_post and latest_post[0] == actor.user_id:
                    continue
                params = {
                    'actor_id': actor.user_id,
                    'chat_id': chat_id,
                    'last_post_id': max_post_id,
                    'last_timestamp': int(datetime.datetime.now(datetime.UTC).timestamp())
                }
                log.debug("Выполняется обновление llm_context с параметрами: ~C95%s~C00", str(params))
                try:
                    self.llm_context_table.insert_or_replace(params)
                    log.debug("Обновлён llm_context для actor_id=%d, chat_id=%d, last_post_id=%d",
                              actor.user_id, chat_id, max_post_id)
                except Exception as e:
                    globals.handle_exception(
                        f"Не удалось обновить llm_context для actor_id={actor.user_id}, chat_id={chat_id}", e)

    async def replicate_to_llm(self, chat_id: int, exclude_source_id: int = None, debug_mode: bool = None):
        """Запускает репликацию контента для указанного chat_id.

        Args:
            chat_id (int): ID чата.
            exclude_source_id (int, optional): ID пользователя для исключения.
            debug_mode (bool, optional): Режим отладки. Defaults to None.
        """
        debug_mode = self.debug_mode if debug_mode is None else debug_mode
        replication_key = (chat_id, exclude_source_id)
        if replication_key in self.active_replications:
            log.debug("Пропуск диалога для chat_id=%d, exclude_source_id=%s: уже выполняется",
                      chat_id, str(exclude_source_id))
            return
        self.active_replications.add(replication_key)
        try:
            log.debug("Запуск диалога для chat_id=%d, debug_mode=%s, exclude_source_id=%s",
                      chat_id, str(debug_mode), str(exclude_source_id))
            file_ids = set()
            file_map = {}
            users = []
            user_rows = self.db.fetch_all('SELECT user_id, user_name, llm_class FROM users')
            log.debug("Загружено %d пользователей для индекса: ~C95%s~C00",
                      len(user_rows), str([(row[0], row[1]) for row in user_rows]))
            for row in user_rows:
                user_id, username, llm_class = row
                role = 'LLM' if llm_class else (
                    'admin' if username == 'admin' else 'mcp' if username == 'agent' else 'developer')
                users.append({"user_id": user_id, "username": username, "role": role})
            content_blocks = self.assemble_posts(chat_id, exclude_source_id, file_ids, file_map)
            content_blocks.extend(self.assemble_files(file_ids, file_map))
            await self._pack_and_send(content_blocks, users, chat_id, exclude_source_id, debug_mode)
            log.debug("Диалог завершён для chat_id=%d, exclude_source_id=%s", chat_id, str(exclude_source_id))
        finally:
            self.active_replications.remove(replication_key)

    def get_processing_status(self) -> dict:
        """Возвращает статус обработки и время выполнения.

        Returns:
            dict: {'status': str, 'actor': str | None, 'elapsed': int | None}
        """
        if self.processing_state == "busy" and self.processing_start:
            elapsed = int(datetime.datetime.now(datetime.UTC).timestamp()) - self.processing_start
            return {
                "status": "busy",
                "actor": self.processing_actor,
                "elapsed": elapsed
            }
        return {"status": "free"}