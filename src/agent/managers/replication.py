# /agent/managers/replication.py, updated 2025-07-18 20:10 EEST
import asyncio
import re
import json
import datetime
from pathlib import Path
from lib.sandwich_pack import SandwichPack
from lib.content_block import ContentBlock
from lib.basic_logger import BasicLogger
from llm_api import LLMConnection, XAIConnection, OpenAIConnection
from managers.db import Database, DataTable
from chat_actor import ChatActor
import globals

PRE_PROMPT_PATH = "/app/docs/llm_pre_prompt.md"

log = globals.get_logger("replication")

class ReplicationManager:
    def __init__(self, user_manager, chat_manager, post_manager, file_manager, debug_mode: bool = False):
        self.user_manager = user_manager
        self.chat_manager = chat_manager
        self.post_manager = post_manager
        self.file_manager = file_manager
        self.db = Database.get_database()
        self.debug_mode = debug_mode
        self.last_sent_tokens = 0
        self.last_num_sources_used = 0
        self.post_processor = globals.post_processor
        SandwichPack.load_block_classes()
        self.actors = self._load_actors()
        self._init_tables()
        self.active_replications = set()
        if debug_mode:
            log.debug("Режим отладки репликации включён")
        else:
            log.debug("Репликация активирована")
        try:
            with open(PRE_PROMPT_PATH, 'r', encoding='utf-8-sig') as f:
                self.pre_prompt = f.read()
            log.debug("Загружен пре-промпт из %s", PRE_PROMPT_PATH)
        except FileNotFoundError as e:
            log.excpt("Файл пре-промпта %s не найден", PRE_PROMPT_PATH, exc_info=(type(e), e, e.__traceback__))
            raise

    def _init_tables(self):
        self.llm_context_table = DataTable(
            table_name="llm_context",
            template=[
                "actor_id INTEGER",
                "chat_id INTEGER",
                "last_post_id INTEGER",
                "last_timestamp INTEGER",
                "PRIMARY KEY (actor_id, chat_id)",
                "FOREIGN KEY (actor_id) REFERENCES users(user_id)",
                "FOREIGN KEY (chat_id) REFERENCES chats(chat_id)"
            ]
        )
        self.llm_responses_table = DataTable(
            table_name="llm_responses",
            template=[
                "response_id INTEGER PRIMARY KEY AUTOINCREMENT",
                "actor_id INTEGER",
                "chat_id INTEGER",
                "response_text TEXT",
                "timestamp INTEGER",
                "triggered_by INTEGER",
                "FOREIGN KEY (actor_id) REFERENCES users(user_id)",
                "FOREIGN KEY (chat_id) REFERENCES chats(chat_id)",
                "FOREIGN KEY (triggered_by) REFERENCES posts(id)"
            ]
        )

    def _load_actors(self):
        actors = []
        rows = self.db.fetch_all('SELECT user_id, user_name, llm_class, llm_token FROM users')
        log.debug("Загружено %d актёров из таблицы users: ~C95%s~C00", len(rows), str([(row[0], row[1]) for row in rows]))
        for row in rows:
            actor = ChatActor(row[0], row[1], row[2], row[3], self.post_manager)
            actors.append(actor)
        return actors

    def _resolve_file_id(self, match, file_ids: set, file_map: dict) -> str:
        if match.group(1):  # @attach_dir#dir_name
            dir_name = match.group(1)
            log.debug("Обработка @attach_dir#%s", dir_name)
            rows = self.db.fetch_all(
                'SELECT id, file_name FROM attached_files WHERE file_name LIKE :dir_name',
                {'dir_name': f"{dir_name}%"}
            )
            file_id_list = [str(row[0]) for row in rows]
            for row in rows:
                file_ids.add(row[0])
                file_map[row[0]] = row[1]
                log.debug("Добавлен файл директории file_id=%d, file_name=%s из @attach_dir#%s", row[0], row[1], dir_name)
            return f"@attached_files#[{','.join(file_id_list)}]"
        elif match.group(2):  # @attach#file_id
            file_id = int(match.group(2))
            file_data = self.file_manager.get_file(file_id)
            if file_data:
                file_ids.add(file_id)
                file_map[file_id] = file_data['file_name']
                log.debug("Разрешён file_id=%d, file_name=%s", file_id, file_data['file_name'])
            else:
                log.warn("Файл file_id=%d не найден в attached_files", file_id)
            return f"@attached_file#{file_id}"
        log.warn("Неверное совпадение в _resolve_file_id: %s", str(match.groups()))
        return match.group(0)

    def _assemble_posts(self, chat_id, exclude_source_id, file_ids: set, file_map: dict) -> list:
        content_blocks = []
        hierarchy = self.chat_manager.get_chat_hierarchy(chat_id)
        log.debug("Сборка постов для chat_id=%d, иерархия=~C95%s~C00", chat_id, str(hierarchy))
        if not hierarchy:
            log.warn("Чаты не найдены в иерархии для chat_id=%d", chat_id)
            return content_blocks

        for cid in hierarchy:
            last_post_row = self.llm_context_table.select_from(
                conditions={'actor_id': exclude_source_id or 0, 'chat_id': cid},
                limit=1
            )
            last_post_id = last_post_row[0][2] if last_post_row else 0
            log.debug("Обработка chat_id=%d, last_post_id=%d", cid, last_post_id)
            parent_msg_row = self.db.fetch_one(
                'SELECT parent_msg_id FROM chats WHERE chat_id = :chat_id',
                {'chat_id': cid}
            )
            parent_msg_id = parent_msg_row[0] if parent_msg_row else None
            parent_msg_timestamp = None
            if parent_msg_id and cid != chat_id:
                parent_msg = self.db.fetch_one(
                    'SELECT timestamp FROM posts WHERE id = :parent_msg_id',
                    {'parent_msg_id': parent_msg_id}
                )
                parent_msg_timestamp = parent_msg[0] if parent_msg else None
            if parent_msg_id and parent_msg_id > last_post_id:
                parent_msg = self.db.fetch_one(
                    'SELECT id, chat_id, timestamp, user_id, message FROM posts WHERE id = :parent_msg_id',
                    {'parent_msg_id': parent_msg_id}
                )
                if parent_msg:
                    message = re.sub(r'@attach_dir#([\w\d/]+)|@attach#(\d+)',
                                     lambda m: self._resolve_file_id(m, file_ids, file_map), parent_msg[4])
                    content_blocks.append(ContentBlock(
                        content_text=message,
                        content_type=":post",
                        file_name=None,
                        timestamp=datetime.datetime.fromtimestamp(parent_msg[2], datetime.UTC).strftime(
                            "%Y-%m-%d %H:%M:%SZ"),
                        post_id=parent_msg[0],
                        user_id=parent_msg[3],
                        relevance=50
                    ))
                    log.debug("Добавлен родительский пост post_id=%d для chat_id=%d", parent_msg[0], cid)
                else:
                    log.debug("Родительский пост не найден для parent_msg_id=%d, chat_id=%d", parent_msg_id, cid)
            query = 'SELECT id, chat_id, user_id, message, timestamp FROM posts WHERE chat_id = :chat_id AND id > :last_post_id'
            params = {'chat_id': cid, 'last_post_id': last_post_id}
            if parent_msg_timestamp and cid != chat_id:
                query += ' AND timestamp <= :parent_timestamp'
                params['parent_timestamp'] = parent_msg_timestamp
            query += ' ORDER BY id'
            history = self.db.fetch_all(query, params)
            for row in history:
                message = re.sub(r'@attach_dir#([\w\d/]+)|@attach#(\d+)',
                                 lambda m: self._resolve_file_id(m, file_ids, file_map), row[3])
                content_blocks.append(ContentBlock(
                    content_text=message,
                    content_type=":post",
                    file_name=None,
                    timestamp=datetime.datetime.fromtimestamp(row[4], datetime.UTC).strftime("%Y-%m-%d %H:%M:%SZ"),
                    post_id=row[0],
                    user_id=row[2],
                    relevance=50
                ))
                log.debug("Добавлен post_id=%d для chat_id=%d", row[0], cid)
        return content_blocks

    def _assemble_files(self, file_ids: set, file_map: dict) -> list:
        content_blocks = []
        log.debug("Сборка файлов для file_ids=~C95%s~C00", str(file_ids))
        unique_files = {}
        for file_id in sorted(file_ids):
            file_data = self.file_manager.get_file(file_id)
            if file_data:
                file_name = file_data['file_name']
                if file_name not in unique_files:
                    unique_files[file_name] = file_id
                    log.debug("Добавлен уникальный файл: id=%d, file_name=%s", file_id, file_name)
                else:
                    log.debug("Пропущен дубликат файла: id=%d, file_name=%s", file_id, file_name)
            else:
                log.warn("Файл file_id=%d не найден в attached_files", file_id)
        for file_id in unique_files.values():
            file_data = self.file_manager.get_file(file_id)
            if file_data:
                extension = '.' + file_data['file_name'].rsplit('.', 1)[-1].lower() if '.' in file_data['file_name'] else ''
                if not SandwichPack.supported_type(extension):
                    log.warn("Неподдерживаемое расширение файла '%s' для file_id=%d, пропуск", extension, file_id)
                    continue
                try:
                    content_text = file_data['content'].decode('utf-8', errors='replace')
                    content_block = SandwichPack.create_block(
                        content_text=content_text,
                        content_type=extension,
                        file_name=file_data['file_name'],
                        timestamp=datetime.datetime.fromtimestamp(file_data['ts'], datetime.UTC).strftime(
                            "%Y-%m-%d %H:%M:%SZ"),
                        file_id=file_id
                    )
                    content_blocks.append(content_block)
                    log.debug(
                        "Добавлен file_id=%d, file_name=%s, block_class=%s, size=%d chars",
                        file_id, file_data['file_name'], content_block.__class__.__name__, len(content_text)
                    )
                    file_map[file_id] = file_data['file_name']
                except Exception as e:
                    log.excpt("Ошибка обработки file_id=%d: %s", file_id, str(e), exc_info=(type(e), e, e.__traceback__))
                    continue
            else:
                log.warn("Файл file_id=%d не найден в attached_files", file_id)
        return content_blocks

    def _write_context_stats(self, content_blocks: list, llm_name: str, chat_id: int, index_json: str):
        """Записывает статистику по блокам сэндвича в файл /app/logs/{$llm_name}_context.stats."""
        stats_file = Path(f"/app/logs/{llm_name}_context.stats")
        stats = []

        pre_prompt_tokens = len(self.pre_prompt) // 4 if self.pre_prompt else 0
        stats.append({
            "block_type": ":pre_prompt",
            "block_id": "N/A",
            "file_name": PRE_PROMPT_PATH,
            "tokens": pre_prompt_tokens
        })

        index_tokens = len(index_json) // 4 if index_json else 0
        stats.append({
            "block_type": ":index",
            "block_id": "N/A",
            "file_name": "JSON index",
            "tokens": index_tokens
        })

        unique_file_names = set()
        for block in content_blocks:
            block_text = block.to_sandwich_block()
            token_count = len(block_text) // 4 if block_text else 0
            block_id = block.post_id or block.file_id or getattr(block, 'quote_id', None) or "N/A"
            file_name = block.file_name or "N/A"
            if file_name != "N/A" and file_name in unique_file_names:
                log.debug("Пропущен дубликат файла в статистике: file_name=%s, block_id=%s", file_name, block_id)
                continue
            unique_file_names.add(file_name)
            stats.append({
                "block_type": block.content_type,
                "block_id": block_id,
                "file_name": file_name,
                "tokens": token_count
            })

        stats.sort(key=lambda x: x["tokens"], reverse=True)

        accumulated_tokens = 0
        for stat in stats:
            accumulated_tokens += stat["tokens"]
            stat["accumulated"] = accumulated_tokens

        header = f"{'Block Type':<15} {'Block ID':<10} {'File Name':<50} {'Tokens':<10} {'Accumulated':<10}\n"
        separator = "-" * 95 + "\n"
        rows = [
            f"{s['block_type']:<15} {s['block_id']:<10} {s['file_name']:<50} {s['tokens']:<10} {s['accumulated']:<10}\n"
            for s in stats]
        try:
            with open(stats_file, "w", encoding="utf-8") as f:
                f.write(header)
                f.write(separator)
                f.writelines(rows)
            log.info("Статистика контекста записана в %s для chat_id=%d, блоков=%d", str(stats_file), chat_id, len(stats))
        except Exception as e:
            log.excpt(
                "Не удалось записать статистику контекста в %s: %s",
                str(stats_file), str(e), exc_info=(type(e), e, e.__traceback__)
            )
            self.post_manager.add_message(chat_id, 2, "Не удалось записать статистику контекста для %s: %s", llm_name, str(e))

    async def _pack_and_send(self, content_blocks: list, users: list, chat_id: int, exclude_source_id=None,
                             debug_mode: bool = False):
        log.debug("Запуск _pack_and_send для chat_id=%d, блоков=%d", chat_id, len(content_blocks))

        latest_post = self.db.fetch_one(
            'SELECT user_id, message FROM posts WHERE chat_id = :chat_id ORDER BY id DESC LIMIT 1',
            {'chat_id': chat_id}
        )
        if latest_post and latest_post[0] == 2 and "Permission denied" in latest_post[1]:
            if not (re.search(r'@grok|@all', latest_post[1], re.IGNORECASE)):
                log.debug(
                    "Пропуск репликации для chat_id=%d из-за ошибки агента без @grok или @all: %s",
                    chat_id, latest_post[1][:50]
                )
                return

        max_tokens = 131072
        content_blocks.sort(key=lambda x: x.relevance if x.relevance else 0, reverse=True)
        total_tokens = len(self.pre_prompt) // 4
        filtered_blocks = []
        for block in content_blocks:
            block_text = block.to_sandwich_block()
            block_tokens = len(block_text) // 4 if block_text else 0
            if total_tokens + block_tokens <= max_tokens:
                filtered_blocks.append(block)
                total_tokens += block_tokens
            else:
                log.debug(
                    "Пропуск блока post_id=%s, file_id=%s из-за лимита токенов",
                    str(block.post_id or 'N/A'), str(block.file_id or 'N/A')
                )
        content_blocks = filtered_blocks

        log.debug("Упаковка %d блоков контента", len(content_blocks))
        try:
            packer = SandwichPack(max_size=1_000_000, system_prompt=self.pre_prompt)
            result = packer.pack(content_blocks, users=users)
            context = f"{self.pre_prompt}\n{result['index']}\n{''.join(result['sandwiches'])}"
            self.last_sent_tokens = len(context) // 4
            log.debug("Контекст сгенерирован, длина=%d символов, оценено токенов=%d", len(context), self.last_sent_tokens)
            self._write_context_stats(content_blocks, "grok", chat_id, result['index'])
            if self.last_sent_tokens > max_tokens:
                raise ValueError("Контекст превышает лимит токенов: %d > %d", self.last_sent_tokens, max_tokens)
        except Exception as e:
            log.excpt("Не удалось упаковать блоки контента для chat_id=%d: %s", chat_id, str(e), exc_info=(type(e), e, e.__traceback__))
            raise

        user_id = self.db.fetch_one(
            'SELECT user_id FROM posts WHERE chat_id = :chat_id ORDER BY id DESC LIMIT 1',
            {'chat_id': chat_id}
        )
        search_parameters = {}
        if user_id:
            user_id = user_id[0]
            settings = self.db.fetch_one(
                'SELECT search_mode, search_sources, max_search_results, from_date, to_date FROM user_settings WHERE user_id = :user_id',
                {'user_id': user_id}
            )
            if settings:
                try:
                    sources = json.loads(settings[1]) if settings[1] else ['web', 'x', 'news']
                    sources = [{"type": src} for src in sources if src in ['web', 'x', 'news']]
                except json.JSONDecodeError:
                    sources = [{"type": "web"}, {"type": "x"}, {"type": "news"}]
                search_parameters = {
                    "mode": settings[0] or "off",
                    "sources": sources,
                    "max_search_results": settings[2] or 20,
                    "from_date": settings[3],
                    "to_date": settings[4]
                }
            else:
                search_parameters = {
                    "mode": "off",
                    "sources": [{"type": "web"}, {"type": "x"}, {"type": "news"}],
                    "max_search_results": 20,
                    "from_date": datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d"),
                    "to_date": datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d")
                }
            log.debug("Используются search_parameters для user_id=%d: ~C95%s~C00", user_id, str(search_parameters))
        else:
            user_id = 2

        llm_actors = [actor for actor in self.actors if actor.llm_connection]
        log.debug("Найдено %d LLM-актёров: ~C95%s~C00", len(llm_actors), str([actor.user_id for actor in llm_actors]))
        processed_actors = set()
        for actor in llm_actors:
            if actor.user_id in processed_actors:
                log.debug("Пропуск дубликата actor_id=%d", actor.user_id)
                continue
            processed_actors.add(actor.user_id)
            if exclude_source_id and actor.user_id == exclude_source_id:
                log.debug("Пропуск actor_id=%d из-за exclude_source_id", actor.user_id)
                continue
            latest_post = self.db.fetch_one(
                'SELECT user_id, message, id FROM posts WHERE chat_id = :chat_id ORDER BY id DESC LIMIT 1',
                {'chat_id': chat_id}
            )
            if latest_post and latest_post[0] == actor.user_id:
                log.debug("Пропуск репликации для actor_id=%d: последнее сообщение от этого актёра", actor.user_id)
                continue
            should_respond = False
            triggered_by = user_id
            if latest_post:
                message = latest_post[1]
                user_name = self.user_manager.get_user_name(actor.user_id)
                triggered_by = latest_post[2]
                if re.search(f'@{user_name}|@all', message, re.IGNORECASE) or '#critics_allowed' in message:
                    should_respond = True
            context_file = Path(f"/app/logs/context-{actor.user_name}.log")
            stats_file = Path(f"/app/logs/{actor.user_name}_context.stats")
            try:
                with open(context_file, "w", encoding="utf-8") as f:
                    f.write(context)
                log.info("Контекст сохранён в %s для user_id=%d, размер=%d символов", str(context_file), actor.user_id, len(context))
            except Exception as e:
                log.excpt("Не удалось сохранить контекст в %s: %s", str(context_file), str(e), exc_info=(type(e), e, e.__traceback__))
                self.post_manager.add_message(chat_id, 2, "Не удалось сохранить контекст для %s: %s", actor.user_name, str(e))
            if debug_mode:
                debug_file = Path(
                    f"/app/logs/debug_{actor.user_name}_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.txt")
                with open(debug_file, "w", encoding="utf-8") as f:
                    f.write(context)
                log.info("Сохранён отладочный контекст в %s для user_id=%d", str(debug_file), actor.user_id)
            else:
                log.debug("Отправка в LLM для user_id=%d: %d символов", actor.user_id, len(context))
                try:
                    if search_parameters.get("mode") == "off":
                        log.debug("Поиск отключён для user_id=%d", user_id)
                        response = await actor.llm_connection.call(context)
                    else:
                        response = await actor.llm_connection.call(context, search_parameters=search_parameters)
                    if response:
                        self.last_num_sources_used = response.get('usage', {}).get('num_sources_used', 0)
                        original_response = response.get('text', '')
                        processed_response = self.post_processor.process_response(chat_id, actor.user_id, original_response)
                        log.debug("Исходный ответ: %s, Обработанный ответ: %s",
                                 original_response[:50], str(processed_response)[:50])
                        if isinstance(processed_response, dict):
                            if processed_response["status"] == "success" and processed_response["processed_msg"]:
                                self._store_response(
                                    actor_id=actor.user_id,
                                    chat_id=chat_id,
                                    original_response=original_response,
                                    processed_response=processed_response["processed_msg"],
                                    triggered_by=triggered_by
                                )
                                if processed_response["agent_reply"]:
                                    self.post_manager.add_message(chat_id, actor.user_id, processed_response["agent_reply"])
                            else:
                                log.warn("Обработанный ответ не содержит processed_msg или status != success: %s", processed_response)
                        else:
                            log.warn("Обработанный ответ не является словарем: %s", type(processed_response))
                            self._store_response(
                                actor_id=actor.user_id,
                                chat_id=chat_id,
                                original_response=original_response,
                                processed_response=processed_response,
                                triggered_by=triggered_by
                            )
                        log.debug(
                            "Получен LLM-ответ для user_id=%d, num_sources_used=%d, токенов=%d",
                            actor.user_id, self.last_num_sources_used, self.last_sent_tokens
                        )
                    else:
                        log.warn("Ответ от LLM не получен для user_id=%d", actor.user_id)
                        self.post_manager.add_message(chat_id, 2, "Нет ответа от LLM для %s", actor.user_name)
                except Exception as e:
                    error_msg = "Ошибка LLM для %s: %s" % (actor.user_name, str(e))
                    log.excpt(error_msg, exc_info=(type(e), e, e.__traceback__))
                    self.post_manager.add_message(chat_id, 2, error_msg)
                    continue

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
                    log.debug(
                        "Обновлён llm_context для actor_id=%d, chat_id=%d, last_post_id=%d",
                        actor.user_id, chat_id, max_post_id
                    )
                except Exception as e:
                    log.excpt(
                        "Не удалось обновить llm_context для actor_id=%d, chat_id=%d: %s",
                        actor.user_id, chat_id, str(e), exc_info=(type(e), e, e.__traceback__)
                    )

    async def replicate_to_llm(self, chat_id, exclude_source_id=None, debug_mode: bool = None):
        debug_mode = self.debug_mode if debug_mode is None else debug_mode
        replication_key = (chat_id, exclude_source_id)
        if replication_key in self.active_replications:
            log.debug(
                "Пропуск репликации для chat_id=%d, exclude_source_id=%s: уже выполняется",
                chat_id, str(exclude_source_id)
            )
            return
        self.active_replications.add(replication_key)
        try:
            log.debug(
                "Запуск репликации для chat_id=%d, debug_mode=%s, exclude_source_id=%s",
                chat_id, str(debug_mode), str(exclude_source_id)
            )
            file_ids = set()
            file_map = {}
            users = []
            user_rows = self.db.fetch_all('SELECT user_id, user_name, llm_class FROM users')
            log.debug("Загружено %d пользователей для индекса: ~C95%s~C00", len(user_rows), str([(row[0], row[1]) for row in user_rows]))
            for row in user_rows:
                user_id, username, llm_class = row
                role = 'LLM' if llm_class else (
                    'admin' if username == 'admin' else 'mcp' if username == 'agent' else 'developer')
                users.append({"user_id": user_id, "username": username, "role": role})
            content_blocks = self._assemble_posts(chat_id, exclude_source_id, file_ids, file_map)
            content_blocks.extend(self._assemble_files(file_ids, file_map))
            await self._pack_and_send(content_blocks, users, chat_id, exclude_source_id, debug_mode)
            log.debug("Репликация завершена для chat_id=%d, exclude_source_id=%s", chat_id, str(exclude_source_id))
        finally:
            self.active_replications.remove(replication_key)

    def _store_response(self, actor_id, chat_id, original_response, processed_response, triggered_by):
        user_name = self.user_manager.get_user_name(actor_id)
        log.debug("Сохранение ответа: processed_response=%s", str(processed_response)[:50])
        messages = self.db.fetch_all(
            'SELECT message FROM posts WHERE chat_id = :chat_id AND user_id = :user_id ORDER BY id DESC LIMIT 1',
            {'chat_id': chat_id, 'user_id': actor_id}
        )
        processed_msg = processed_response if isinstance(processed_response, str) else processed_response.get("processed_msg", "")
        processed_msg = processed_msg.strip()
        for (message,) in messages:
            if processed_msg == message:
                log.debug(
                    "Пропуск дубликата LLM-ответа для chat_id=%d, actor_id=%d: %s",
                    chat_id, actor_id, processed_msg[:50]
                )
                return
        if len(processed_msg) > 2 and processed_msg != "✅":
            self.post_manager.add_message(chat_id, actor_id, processed_msg)
            log.debug("Добавлен обработанный ответ в posts для chat_id=%d, actor_id=%d: %s", chat_id, actor_id, processed_msg[:50])
        else:
            log.debug(
                "Игнорирование ответа для chat_id=%d, actor_id=%d, length=%d, triggered_by=%d",
                chat_id, actor_id, len(processed_msg), triggered_by
            )
        self.llm_responses_table.insert_into(
            {
                'actor_id': actor_id,
                'chat_id': chat_id,
                'response_text': original_response,
                'timestamp': int(datetime.datetime.now(datetime.UTC).timestamp()),
                'triggered_by': triggered_by
            }
        )
        log.debug("Сохранён исходный ответ для actor_id=%d, chat_id=%d", actor_id, chat_id)