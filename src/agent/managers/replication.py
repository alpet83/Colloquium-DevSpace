# /app/agent/managers/replication.py, updated 2025-07-20 23:59 EEST
import re
import datetime
from pathlib import Path
from llm_interactor import LLMInteractor
from managers.db import Database, DataTable
from chat_actor import ChatActor
import globals

log = globals.get_logger("replication")

class ReplicationManager(LLMInteractor):
    def __init__(self, debug_mode: bool = False):
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

    def _load_actors(self):
        actors = []
        rows = self.db.fetch_all('SELECT user_id, user_name, llm_class, llm_token FROM users')
        log.debug("Загружено %d актёров из таблицы users: ~C95%s~C00", len(rows),
                  str([(row[0], row[1]) for row in rows]))
        for row in rows:
            actor = ChatActor(row[0], row[1], row[2], row[3], globals.post_manager)
            actors.append(actor)
        return actors


    async def _recursive_replicate(self, content_blocks: list, users: list, chat_id: int, actor: ChatActor,
                                  exclude_source_id=None, rql: int = 1, max_rql: int = 5):
        """Рекурсивно обрабатывает ответы LLM, добавляя посты и вызывая репликацию для других участников."""
        if rql > max_rql:
            log.info("Достигнут предел рекурсивного диалога %d для actor_id=%d, chat_id=%d", max_rql, actor.user_id, chat_id)
            return

        self.processing_state = "busy"
        self.processing_actor = actor.user_name
        self.processing_start = int(datetime.datetime.now(datetime.UTC).timestamp())
        log.debug("Начался рекурсивный диалог для actor_id=%d (%s), chat_id=%d, rql=%d",
                  actor.user_id, actor.user_name, chat_id, rql)
        try:
            original_response = await self.interact(content_blocks, users, chat_id, actor, self.debug_mode, rql)
            if original_response:
                # Вырезаем всё до @agent, если присутствует
                prefix = ''
                agent_command = original_response
                if '@agent' in original_response:
                    split_index = original_response.index('@agent')
                    prefix = original_response[:split_index]
                    agent_command = original_response[split_index:]
                    log.debug("Вырезан префикс до @agent: prefix=%s, agent_command=%s",
                              prefix[:50], agent_command[:50])

                # Обрабатываем команду агента
                processed_response = globals.post_processor.process_response(chat_id, actor.user_id, agent_command)
                log.debug("Исходный ответ: %s, Обработанный ответ: %s",
                          original_response[:50], str(processed_response)[:50])

                # Определяем rql для ответа
                new_rql = None
                if actor.user_id == 1:  # @admin
                    new_rql = 0
                    log.debug("Установлен rql=0 для ответа от admin, actor_id=%d, chat_id=%d", actor.user_id, chat_id)
                else:
                    # Проверяем цитирование @post#{post_id} или @post#{timestamp}
                    cite_match = re.search(r'@post#(\d+)', original_response)
                    if cite_match:
                        cited_id_or_timestamp = int(cite_match.group(1))
                        # Проверяем, является ли это post_id (небольшое значение, например, < 1_000_000)
                        if cited_id_or_timestamp < 1_000_000:
                            cited_post = self.db.fetch_one(
                                'SELECT rql FROM posts WHERE chat_id = :chat_id AND id = :post_id',
                                {'chat_id': chat_id, 'post_id': cited_id_or_timestamp}
                            )
                            if cited_post and cited_post[0] is not None:
                                new_rql = cited_post[0] + 1
                                log.debug("Найдено цитирование @post#%d (post_id), установлен rql=%d", cited_id_or_timestamp, new_rql)
                            else:
                                log.warn("Цитируемый пост @post#%d (post_id) не найден, fallback к последнему rql", cited_id_or_timestamp)
                        else:
                            # Предполагаем, что это timestamp
                            cited_post = self.db.fetch_one(
                                'SELECT rql FROM posts WHERE chat_id = :chat_id AND timestamp = :timestamp',
                                {'chat_id': chat_id, 'timestamp': cited_id_or_timestamp}
                            )
                            if cited_post and cited_post[0] is not None:
                                new_rql = cited_post[0] + 1
                                log.debug("Найдено цитирование @post#%d (timestamp), установлен rql=%d", cited_id_or_timestamp, new_rql)
                            else:
                                log.warn("Цитируемый пост @post#%d (timestamp) не найден, fallback к последнему rql", cited_id_or_timestamp)
                    if new_rql is None:
                        last_post = self.db.fetch_one(
                            'SELECT rql FROM posts WHERE chat_id = :chat_id ORDER BY id DESC LIMIT 1',
                            {'chat_id': chat_id}
                        )
                        last_rql = last_post[0] if last_post and last_post[0] is not None else 0
                        new_rql = last_rql + 1
                        log.debug("Установлен rql=%d на основе последнего поста (last_rql=%d)", new_rql, last_rql)

                if new_rql > max_rql:
                    log.info("Превышен предел rql=%d для actor_id=%d, chat_id=%d, ответ пропущен", new_rql, actor.user_id, chat_id)
                    return

                if isinstance(processed_response, dict):
                    # Пристыковываем префикс к processed_msg
                    processed_msg = processed_response.get("processed_msg", "")
                    if prefix and processed_msg:
                        processed_response["processed_msg"] = prefix + processed_msg
                        log.debug("Пристыкован префикс к processed_msg: %s", processed_response["processed_msg"][:50])

                    if (processed_response["handled_cmds"] > 0 or processed_response["failed_cmds"] > 0 or
                            processed_response["processed_msg"]):
                        self._store_response(
                            actor_id=actor.user_id,
                            chat_id=chat_id,
                            original_response=original_response,
                            processed_response=processed_response["processed_msg"],
                            triggered_by=actor.user_id,
                            rql=new_rql
                        )
                        if processed_response["agent_reply"]:
                            globals.post_manager.add_message(
                                chat_id, 2, f"@{actor.user_name} " + processed_response["agent_reply"],
                                rql=new_rql
                            )
                        if new_rql < max_rql:
                            latest_post = self.db.fetch_one(
                                'SELECT user_id, message FROM posts WHERE chat_id = :chat_id ORDER BY id DESC LIMIT 1',
                                {'chat_id': chat_id}
                            )
                            if latest_post and '@all' in latest_post[1]:
                                # Выбрать всех доступных других LLM-актёров
                                for next_actor in self.actors:
                                    if (next_actor.user_id > 1 and next_actor.user_id != actor.user_id and
                                        next_actor.llm_connection):
                                        file_ids = set()
                                        file_map = {}
                                        new_content_blocks = self.assemble_posts(chat_id, exclude_source_id, file_ids, file_map)
                                        new_content_blocks.extend(self.assemble_files(file_ids, file_map))
                                        log.debug("Начался рекурсивный диалог между %s и %s для rql=%d",
                                                  actor.user_name, next_actor.user_name, new_rql + 1)
                                        await self._recursive_replicate(
                                            new_content_blocks, users, chat_id, next_actor, exclude_source_id, new_rql + 1, max_rql
                                        )
                    else:
                        log.warn("Обработанный ответ не содержит processed_msg или команд: %s", processed_response)
                else:
                    log.warn("Обработанный ответ не является словарем: %s", type(processed_response))
                    processed_response = prefix + str(processed_response) if prefix else processed_response
                    self._store_response(
                        actor_id=actor.user_id,
                        chat_id=chat_id,
                        original_response=original_response,
                        processed_response=processed_response,
                        triggered_by=actor.user_id,
                        rql=new_rql
                    )
            else:
                log.warn("Ответ от LLM не получен для user_id=%d, rql=%d", actor.user_id, rql)
                globals.post_manager.add_message(
                    chat_id, 2, f"Нет ответа от LLM для {actor.user_name}", rql=rql
                )
        finally:
            self.processing_state = "free"
            self.processing_actor = None
            self.processing_start = None

    async def _pack_and_send(self, content_blocks: list, users: list, chat_id: int, exclude_source_id=None,
                             debug_mode: bool = False):
        log.debug("Запуск _pack_and_send для chat_id=%d, блоков=%d", chat_id, len(content_blocks))

        latest_post = self.db.fetch_one(
            'SELECT user_id, message FROM posts WHERE chat_id = :chat_id ORDER BY id DESC LIMIT 1',
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
        for actor in llm_actors:
            if actor.user_id in processed_actors:
                log.debug("Пропуск дубликата actor_id=%d", actor.user_id)
                continue
            if exclude_source_id and actor.user_id == exclude_source_id:
                log.debug("Пропуск actor_id=%d из-за exclude_source_id", actor.user_id)
                continue
            latest_post = self.db.fetch_one(
                'SELECT user_id, message, id FROM posts WHERE chat_id = :chat_id ORDER BY id DESC LIMIT 1',
                {'chat_id': chat_id}
            )
            if latest_post and latest_post[0] == actor.user_id:
                log.debug("Пропуск диалога для actor_id=%d: последнее сообщение от этого актёра", actor.user_id)
                continue
            should_respond = False
            triggered_by = latest_post[2] if latest_post else None
            if latest_post:
                message = latest_post[1]
                user_name = globals.user_manager.get_user_name(actor.user_id)
                if re.search(f'@{user_name}|@all', message, re.IGNORECASE) or '#critics_allowed' in message:
                    should_respond = True
            if should_respond:
                file_ids = set()
                file_map = {}
                new_content_blocks = self.assemble_posts(chat_id, exclude_source_id, file_ids, file_map)
                new_content_blocks.extend(self.assemble_files(file_ids, file_map))
                log.debug("Начался диалог между %s и %s для chat_id=%d",
                          globals.user_manager.get_user_name(latest_post[0]) if latest_post else "Unknown",
                          actor.user_name, chat_id)
                await self._recursive_replicate(new_content_blocks, users, chat_id, actor, exclude_source_id, 1)
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
                    globals.handle_exception(f"Не удалось обновить llm_context для actor_id={actor.user_id}, chat_id={chat_id}", e)

    async def replicate_to_llm(self, chat_id, exclude_source_id=None, debug_mode: bool = None):
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

    def get_processing_status(self):
        """Возвращает статус обработки и время выполнения."""
        if self.processing_state == "busy" and self.processing_start:
            elapsed = int(datetime.datetime.now(datetime.UTC).timestamp()) - self.processing_start
            return {
                "status": "busy",
                "actor": self.processing_actor,
                "elapsed": elapsed
            }
        return {"status": "free"}

    def _store_response(self, actor_id, chat_id, original_response, processed_response, triggered_by, rql=None):
        user_name = globals.user_manager.get_user_name(actor_id)
        log.debug("Сохранение ответа: processed_response=%s, rql=%s", str(processed_response)[:50], str(rql))
        messages = self.db.fetch_all(
            'SELECT message FROM posts WHERE chat_id = :chat_id AND user_id = :user_id ORDER BY id DESC LIMIT 1',
            {'chat_id': chat_id, 'user_id': actor_id}
        )
        processed_msg = processed_response if isinstance(processed_response, str) else processed_response.get(
            "processed_msg", "")
        processed_msg = processed_msg.strip()
        for (message,) in messages:
            if processed_msg == message:
                log.debug("Пропуск дубликата LLM-ответа для chat_id=%d, actor_id=%d: %s",
                          chat_id, actor_id, processed_msg[:50])
                return
        if len(processed_msg) > 2 and processed_msg != "✅":
            globals.post_manager.add_message(
                chat_id, actor_id, processed_msg, rql=rql
            )
            log.debug("Добавлен обработанный ответ в posts для chat_id=%d, actor_id=%d: %s",
                      chat_id, actor_id, processed_msg[:50])
        else:
            log.debug("Игнорирование ответа для chat_id=%d, actor_id=%d, length=%d, triggered_by=%d",
                      chat_id, actor_id, len(processed_msg), triggered_by)
        self.llm_responses_table.insert_into(
            {
                'actor_id': actor_id,
                'chat_id': chat_id,
                'response_text': original_response,
                'timestamp': int(datetime.datetime.now(datetime.UTC).timestamp()),
                'triggered_by': triggered_by,
                'rql': rql
            }
        )
        log.debug("Сохранён исходный ответ для actor_id=%d, chat_id=%d, rql=%s", actor_id, chat_id, str(rql))