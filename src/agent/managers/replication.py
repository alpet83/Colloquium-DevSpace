# /app/agent/managers/replication.py, updated 2025-07-27 14:00 EEST
import re
import asyncio
import datetime
import time
from pathlib import Path
from llm_interactor import LLMInteractor, ContextInput
from managers.chats import ChatLocker
from chat_actor import ChatActor
import globals as g

log = g.get_logger("replication")


class ReplicationManager(LLMInteractor):
    """Управляет репликацией сообщений между LLM-актёрами."""
    def __init__(self, debug_mode: bool = False):
        """Инициализирует ReplicationManager с настройкой режима отладки.

        Args:
            debug_mode (bool, optional): Включает режим отладки. Defaults to False.
        """
        super().__init__(debug_mode)
        self.active_chat_id = 0
        self.failed_tasks = 0
        self.actors = self._load_actors()
        self.active_replications = set()
        if debug_mode:
            log.debug("Режим отладки репликации включён")
        else:
            log.debug("Репликация активирована")

    def any_llm(self):
        for actor in self.actors:  # need find any LLM in actors
            if actor.llm_connection:
                return actor
        return None

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
            actor = ChatActor(row[0], row[1], row[2], row[3], g.post_manager)
            actors.append(actor)
        return actors

    def check_exceptions(self, task):
        try:
            result = task.result()
            log.debug("Задача %s завершена: %s", task.get_name(), str(result))
        except Exception as e:
            log.excpt("Task caused exception ", e=e)
            g.post_manager.agent_post(task.chat_id, f"Async task caused exception {e}", task.rql + 1, task.post_id)
            self.failed_tasks += 1

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
        self.active_chat_id = chat_id
        log.debug("Проверка репликации: chat_id=%d, post_id=%d, user_id=%d, rql=%d, message=%s",
                  chat_id, post_id, user_id, rql, message[:50])
        if rql > 0:
            log.debug("Репликация не запущена для post_id=%d, chat_id=%d: rql=%d (требуется 0)",
                      post_id, chat_id, rql)
            return False

        # Проверяем наличие @username (LLM-актёры) или @all
        llm_usernames = [actor.user_name for actor in self.actors if actor.llm_connection and actor.user_id > g.AGENT_UID]
        log.debug("LLM-актёры для проверки триггеров: %s", llm_usernames)
        pattern = '|'.join([f'@{re.escape(username)}' for username in llm_usernames] + ['@all'])
        if not re.search(pattern, message, re.IGNORECASE):
            log.debug("Репликация не запущена для post_id=%d, chat_id=%d: нет триггера (@username или @all)",
                      post_id, chat_id)
            return False

        try:
            log.debug("Запуск репликации для post_id=%d, chat_id=%d, user_id=%d", post_id, chat_id, user_id)
            loop = asyncio.get_event_loop()
            task = loop.create_task(self.replicate_to_llm(chat_id), name="Replicate to LLM")
            task.rql = rql
            task.chat_id = chat_id
            task.post_id = post_id
            task.add_done_callback(self.check_exceptions)
            log.debug("Репликация запланирована для post_id=%d, chat_id=%d", post_id, chat_id)
            return True
        except Exception as e:
            log.excpt("Ошибка запуска репликации для post_id=%d, chat_id=%d: %s", post_id, chat_id, e=e)
            user_name = g.user_manager.get_user_name(user_id) or 'unknown'
            error_result = g.post_manager.add_post(
                chat_id, 2, f"@{user_name} Replication check error: {str(e)}", 0, post_id
            )
            if error_result.get("error"):
                log.warn("Не удалось сохранить сообщение об ошибке проверки репликации: %s", error_result["error"])
            else:
                log.debug("Добавлено сообщение об ошибке проверки репликации в chat_id=%d для user_id=2", chat_id)
            return False

    async def _recursive_replicate(self, ci: ContextInput, rql: int = 1, max_rql: int = 5):
        """Рекурсивно обрабатывает ответы LLM, добавляя посты и вызывая репликацию для других участников.

        Args:
            ci (ContextInput): Набор входных данных в объекте
            rql (int, optional): Уровень рекурсии диалога. Defaults to 1.
            max_rql (int, optional): Максимальный уровень рекурсии. Defaults to 5.
        """
        actor = ci.actor
        post_man = g.post_manager

        if rql > max_rql:
            log.info("Достигнут предел рекурсивного диалога %d для actor_id=%d, chat_id=%d",
                     max_rql, actor.user_id, ci.chat_id)
            return

        msg = "Начался рекурсивный диалог для " if rql <= 1 else "Продолжение рекурсивного диалога для "
        log.debug(msg + "actor_id=%d (%s), chat_id=%d, rql=%d",
                  actor.user_id, actor.user_name, ci.chat_id, rql)

        t_start = time.time()

        try:
            ci.debug_mode = self.debug_mode
            with ChatLocker(ci.chat_id, actor.user_name):
                original_response = await self.interact(ci, rql=rql)
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
                            {'chat_id': ci.chat_id, 'post_id': cited_id_or_timestamp}
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
                        {'chat_id': ci.chat_id}
                    )
                    reply_to = last_post[0] if last_post else None
                    log.debug("Установлен reply_to=%s на основе последнего поста", str(reply_to))

                # Сохраняем ответ модели через add_message
                post = post_man.add_post(
                    ci.chat_id, actor.user_id, prefix + agent_command, rql, reply_to, elapsed
                )
                if not post.get("status"):
                    log.warn("Не удалось сохранить ответ модели для actor_id=%d, chat_id=%d: %s",
                             actor.user_id, ci.chat_id, str(post))
                    return
                response_result = await post_man.process_post(post, False)
                if response_result.get("error"):
                    log.warn("Не удалось обработать ответ модели для actor_id=%d, chat_id=%d: %s",
                             actor.user_id, ci.chat_id, response_result["error"])
                    return
                processed_msg = response_result.get("processed_msg", "")
                if processed_msg and rql < max_rql:
                    await self._broadcast(ci.users, ci.chat_id, ci.exclude_id, rql=rql + 1, post_id=post['post_id'])
                agent_reply = response_result.get("agent_reply")
                if agent_reply:
                    log.debug("Добавлен ответ агента для chat_id=%d: %s, проверка на репликацию", ci.chat_id, agent_reply[:50])
                    agent_post = g.post_manager.latest_post({'chat_id': ci.chat_id, 'user_id': g.AGENT_UID})
                    if isinstance(agent_post, dict) and agent_post.get('id'):
                        await self._broadcast(ci.users, ci.chat_id, ci.exclude_id, rql=rql + 1, post_id=agent_post.get('id'))
                    else:
                        log.error("latest_post returned %s, can't broadcast response to LLM,", str(agent_post))

            else:
                log.warn("Ответ от LLM не получен для user_id=%d, rql=%d: %s", actor.user_id, rql, str(original_response))
                g.post_manager.agent_post(
                    ci.chat_id, f"Нет ответа от LLM для {actor.user_name}, время запроса {elapsed} сек.", rql, None
                )
        except Exception as e:
            log.excpt("Error in _recursive_replicate", e=e)
            raise e

    def collect_blocks(self, chat_id: int, exclude_id: int = None):
        file_ids = set()
        file_map = {}
        content_blocks = self.assemble_posts(chat_id, exclude_id, file_ids, file_map)
        content_blocks.extend(self.assemble_files(file_ids, file_map))
        content_blocks.extend(self.assemble_spans())
        return content_blocks

    async def _broadcast(self, users: list, chat_id: int, exclude_id: int = None, rql: int = 0, post_id: int = -1):
        """Отправляет контент всем LLM-актёрам при @all и RQL <= 1, иначе по триггерам.

        Args:
            users (list): Список пользователей.
            chat_id (int): ID чата.
            exclude_id (int, optional): ID пользователя для исключения.
            rql (int, optional: Уровень рекурсии репликации (0 для начала, от инициатора)
            post_id (int, optional): ID поста, в котором смотреть получателей репликации
        """

        cond = {'chat_id': chat_id}
        if post_id >= 0:
            cond['id'] = post_id

        latest_post = g.post_manager.latest_post(cond)
        if not latest_post:
            log.warn("PostManager returned %s as latest_post", str(latest_post))
            return
        post_id = latest_post['id']
        user_id = latest_post['user_id']
        message = latest_post['message']
        if not message or len(message) < 5:
            log.warn("Длина сообщения поста слишком мала")
            return

        user_name = g.user_manager.get_user_name(user_id) if user_id else "Unknown"
        refs = re.findall(r"@(\w+)\s", message, re.M)
        if not refs:
            log.debug("В посте нет ссылок на пользователей - выход")
            return

        log.debug("Проверка _broadcast для поста chat_id=%d, post_id=%d by user=%d:%s, refs: %s",
                  chat_id, post_id, user_id, user_name, str(refs))

        llm_actors = [actor for actor in self.actors if actor.llm_connection and actor.user_id > 1]
        llm_ids = [actor.user_id for actor in llm_actors]

        log.debug("Найдено %d LLM-актёров (user_id > 1): %s", len(llm_actors),str(llm_ids))
        processed_actors = set()

        # Проверяем триггер @all перед циклом
        is_broadcast = False

        if ('all' in refs) and (user_id not in llm_ids):
            is_broadcast = True
            log.debug("BROADCAST!: Обнаружен триггер @all для широковещательной репликации: chat_id=%d, rql=%d от %s, refs: %s", chat_id, rql, user_name, str(refs))

        content_blocks = []
        # =================  Цикл по потенциальным получателям нового контекста ========================
        coro_list = []
        for actor in llm_actors:
            if actor.user_id in processed_actors:
                log.debug("Пропуск дубликата actor_id=%d", actor.user_id)
                continue
            if exclude_id and actor.user_id == exclude_id:
                log.debug("Пропуск actor_id=%d из-за exclude_id", actor.user_id)
                continue
            if user_id == actor.user_id:
                log.debug("Пропуск диалога для автора поста %d:%s", actor.user_id, actor.user_name)
                continue
            trigger = 0
            if is_broadcast:
                trigger = 1
            elif actor.user_name in refs:
                trigger = 2
                log.debug("Триггер ответа для LLM %d:%s message=%s ", actor.user_id, actor.user_name, message[:50])

            if trigger > 0:
                action = "Начат" if rql == 0 else "Продолжен"
                log.debug(f"{action} диалог между %s и %s для chat_id=%d, trigger = %d", user_name, actor.user_name, chat_id, trigger)
                content_blocks = self.collect_blocks(chat_id, exclude_id)
                ci = ContextInput(content_blocks, users, chat_id, actor, exclude_id)
                coro = self._recursive_replicate(ci, rql + 1)
                coro_list.append(coro)
                processed_actors.add(actor.user_id)
            else:
                log.debug("Пропуск actor_id=%d: нет триггера для репликации", actor.user_id)

        if coro_list:
            log.debug(" Параллельный запуск %d репликаций ", len(coro_list))
            await asyncio.gather(*coro_list)

        max_post_id = max([block.post_id for block in content_blocks if block.post_id is not None] or [0])
        if max_post_id:
            for actor in llm_actors:
                if actor.user_id in processed_actors:
                    continue
                if exclude_id and actor.user_id == exclude_id:
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
                    g.handle_exception(
                        f"Не удалось обновить llm_context для actor_id={actor.user_id}, chat_id={chat_id}", e)

    async def replicate_to_llm(self, chat_id: int, exclude_source_id: int = None):
        """Запускает репликацию контента для указанного chat_id.

        Args:
            chat_id (int): ID чата.
            exclude_source_id (int, optional): ID пользователя для исключения.
        """

        replication_key = (chat_id, exclude_source_id)
        if replication_key in self.active_replications:
            log.debug("Пропуск диалога для chat_id=%d, exclude_source_id=%s: уже выполняется",
                      chat_id, str(exclude_source_id))
            return
        self.active_replications.add(replication_key)
        try:
            log.debug("Запуск диалога для chat_id=%d, debug_mode=%s, exclude_source_id=%s",
                      chat_id, str(self.debug_mode), str(exclude_source_id))
            users = []
            user_rows = self.db.fetch_all('SELECT user_id, user_name, llm_class FROM users')
            log.debug("Загружено %d пользователей для индекса: ~C95%s~C00",
                      len(user_rows), str([(row[0], row[1]) for row in user_rows]))
            for actor in self.actors:
                username = actor.user_name
                role = 'LLM' if actor.llm_class else (
                        'admin' if username == 'admin' else 'mcp' if username == 'agent' else 'developer')
                users.append({"user_id": actor.user_id, "username": username, "role": role})

            await self._broadcast(users, chat_id, exclude_source_id)
            log.debug("Диалог завершён для chat_id=%d, exclude_source_id=%s", chat_id, str(exclude_source_id))
        finally:
            self.active_replications.remove(replication_key)

    def entity_index(self, chat_id: int):
        result = super().entity_index(chat_id)
        llm = self.any_llm()
        if not result and llm:
            admin = {"user_id": 1, "user_name": "admin"}
            blocks = self.collect_blocks(chat_id)
            self.build_context(blocks, [admin], chat_id, llm)
            return super().entity_index(chat_id)
        return result

    def fake_interact(self, chat_id: int):
        actor = self.any_llm()
        if actor:
            try:
                admin = {"user_id": 1, "user_name": "admin"}
                blocks = self.collect_blocks(chat_id)
                log.debug(f"Trying fake interact with LLM {actor} for chat {chat_id}")
                loop = asyncio.get_event_loop()
                ci = ContextInput(blocks, users=[admin], chat_id=chat_id, actor=actor)
                ci.debug_mode = True
                coro = self.interact(ci)
                task = loop.create_task(coro, name="Fake replicate")
                if not task.done():
                    log.debug("Task `%s` active", task.get_name())
                    task.rql = 1
                    task.chat_id = chat_id
                    task.post_id = 0
                    task.add_done_callback(self.check_exceptions)
                return True
            except Exception as e:
                log.excpt("Catched in fake_interact", e=e)
        log.error("Not avail LLM actors")
        return False
