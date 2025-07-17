# /agent/managers/replication.py, updated 2025-07-17 21:57 EEST
import asyncio
import logging
import re
import json
import datetime
import traceback
import globals
from pathlib import Path
from lib.sandwich_pack import SandwichPack
from lib.content_block import ContentBlock
from llm_api import LLMConnection, XAIConnection, OpenAIConnection
from managers.db import Database, DataTable
from chat_actor import ChatActor

PRE_PROMPT_PATH = "/app/docs/llm_pre_prompt.md"

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
            logging.debug("Replication debug mode is enabled")
        else:
            logging.debug("Replication activated")
        try:
            with open(PRE_PROMPT_PATH, 'r', encoding='utf-8-sig') as f:
                self.pre_prompt = f.read()
            logging.debug(f"Loaded pre-prompt from {PRE_PROMPT_PATH}")
        except FileNotFoundError:
            logging.error(f"Pre-prompt file {PRE_PROMPT_PATH} not found")
            raise FileNotFoundError(f"Pre-prompt file {PRE_PROMPT_PATH} is required for initialization")

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
        logging.debug(f"Loaded {len(rows)} actors from users table: {[(row[0], row[1]) for row in rows]}")
        for row in rows:
            actor = ChatActor(row[0], row[1], row[2], row[3], self.post_manager)
            actors.append(actor)
        return actors

    def _resolve_file_id(self, match, file_ids: set, file_map: dict) -> str:
        if match.group(1):  # @attach_dir#dir_name
            dir_name = match.group(1)
            logging.debug(f"Processing @attach_dir#{dir_name}")
            rows = self.db.fetch_all(
                'SELECT id, file_name FROM attached_files WHERE file_name LIKE :dir_name',
                {'dir_name': f"{dir_name}%"}
            )
            file_id_list = [str(row[0]) for row in rows]
            for row in rows:
                file_ids.add(row[0])
                file_map[row[0]] = row[1]
                logging.debug(f"Added dir file_id={row[0]}, file_name={row[1]} from @attach_dir#{dir_name}")
            return f"@attached_files#[{','.join(file_id_list)}]"
        elif match.group(2):  # @attach#file_id
            file_id = int(match.group(2))
            file_data = self.file_manager.get_file(file_id)
            if file_data:
                file_ids.add(file_id)
                file_map[file_id] = file_data['file_name']
                logging.debug(f"Resolved file_id={file_id}, file_name={file_data['file_name']}")
            else:
                logging.warning(f"File id={file_id} not found in attached_files")
            return f"@attached_file#{file_id}"
        logging.warning(f"Invalid match in _resolve_file_id: {match.groups()}")
        return match.group(0)

    def _assemble_posts(self, chat_id, exclude_source_id, file_ids: set, file_map: dict) -> list:
        content_blocks = []
        hierarchy = self.chat_manager.get_chat_hierarchy(chat_id)
        logging.debug(f"Assembling posts for chat_id={chat_id}, hierarchy={hierarchy}")
        if not hierarchy:
            logging.warning(f"No chats found in hierarchy for chat_id={chat_id}")
            return content_blocks

        for cid in hierarchy:
            last_post_row = self.llm_context_table.select_from(
                conditions={'actor_id': exclude_source_id or 0, 'chat_id': cid},
                limit=1
            )
            last_post_id = last_post_row[0][2] if last_post_row else 0
            logging.debug(f"Processing chat_id={cid}, last_post_id={last_post_id}")
            parent_msg_row = self.db.fetch_one(
                'SELECT parent_msg_id FROM chats WHERE chat_id = :chat_id',
                {'chat_id': cid}
            )
            parent_msg_id = parent_msg_row[0] if parent_msg_row else None
            parent_msg_timestamp = None
            if parent_msg_id and cid != chat_id:  # Фильтруем только для дочерних чатов
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
                    logging.debug(f"Added parent post_id={parent_msg[0]} for chat_id={cid}")
                else:
                    logging.debug(f"No parent post found for parent_msg_id={parent_msg_id}, chat_id={cid}")
            query = 'SELECT id, chat_id, user_id, message, timestamp FROM posts WHERE chat_id = :chat_id AND id > :last_post_id'
            params = {'chat_id': cid, 'last_post_id': last_post_id}
            if parent_msg_timestamp and cid != chat_id:  # Фильтруем сообщения до создания дочернего чата
                query += ' AND timestamp <= :parent_timestamp'
                params['parent_timestamp'] = parent_msg_timestamp
            query += ' ORDER BY id'  # Сортировка по id вместо timestamp
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
                logging.debug(f"Added post_id={row[0]} for chat_id={cid}")
        return content_blocks

    def _assemble_files(self, file_ids: set, file_map: dict) -> list:
        content_blocks = []
        logging.debug(f"Assembling files for file_ids={file_ids}")
        # Проверяем уникальность файлов по file_name и сортируем по file_id
        unique_files = {}
        for file_id in sorted(file_ids):  # Сортировка по file_id
            file_data = self.file_manager.get_file(file_id)
            if file_data:
                file_name = file_data['file_name']
                if file_name not in unique_files:
                    unique_files[file_name] = file_id
                    logging.debug(f"Added unique file: id={file_id}, file_name={file_name}")
                else:
                    logging.debug(f"Skipped duplicate file: id={file_id}, file_name={file_name}")
            else:
                logging.warning(f"File id={file_id} not found in attached_files")
        # Обрабатываем только уникальные файлы
        for file_id in unique_files.values():
            file_data = self.file_manager.get_file(file_id)
            if file_data:
                extension = '.' + file_data['file_name'].rsplit('.', 1)[-1].lower() if '.' in file_data['file_name'] else ''
                if not SandwichPack.supported_type(extension):
                    logging.warning(f"Unsupported file extension '{extension}' for file_id={file_id}, skipping")
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
                    logging.debug(
                        f"Added file_id={file_id}, file_name={file_data['file_name']}, block_class={content_block.__class__.__name__}, size={len(content_text)} chars")
                    file_map[file_id] = file_data['file_name']
                except Exception as e:
                    logging.error(f"Error processing file_id={file_id}: {str(e)}")
                    continue
            else:
                logging.warning(f"File id={file_id} not found in attached_files")
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

        unique_file_names = set()  # Проверяем уникальность file_name
        for block in content_blocks:
            block_text = block.to_sandwich_block()
            token_count = len(block_text) // 4 if block_text else 0
            block_id = block.post_id or block.file_id or getattr(block, 'quote_id', None) or "N/A"
            file_name = block.file_name or "N/A"
            if file_name != "N/A" and file_name in unique_file_names:
                logging.debug(f"Skipped duplicate file in stats: file_name={file_name}, block_id={block_id}")
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
            logging.info(f"Context stats written to {stats_file} for chat_id={chat_id}, blocks={len(stats)}")
        except Exception as e:
            logging.error(f"Failed to write context stats to {stats_file}: {str(e)}")
            self.post_manager.add_message(chat_id, 2, f"Failed to write context stats for {llm_name}: {str(e)}")

    async def _pack_and_send(self, content_blocks: list, users: list, chat_id: int, exclude_source_id=None,
                             debug_mode: bool = False):
        logging.debug(f"Starting _pack_and_send for chat_id={chat_id}, blocks={len(content_blocks)}")

        # Проверяем последнее сообщение от агента
        latest_post = self.db.fetch_one(
            'SELECT user_id, message FROM posts WHERE chat_id = :chat_id ORDER BY id DESC LIMIT 1',
            {'chat_id': chat_id}
        )
        if latest_post and latest_post[0] == 2 and "Permission denied" in latest_post[1]:
            if not (re.search(r'@grok|@all', latest_post[1], re.IGNORECASE)):
                logging.debug(
                    f"Skipping replication for chat_id={chat_id} due to agent error without @grok or @all: {latest_post[1][:50]}...")
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
                logging.debug(
                    f"Skipping block post_id={block.post_id or 'N/A'}, file_id={block.file_id or 'N/A'} due to token limit")
        content_blocks = filtered_blocks

        logging.debug(f"Packing {len(content_blocks)} content blocks")
        try:
            packer = SandwichPack(max_size=1_000_000, system_prompt=self.pre_prompt)
            result = packer.pack(content_blocks, users=users)
            context = f"{self.pre_prompt}\n{result['index']}\n{''.join(result['sandwiches'])}"
            self.last_sent_tokens = len(context) // 4
            logging.debug(f"Context generated, length={len(context)} chars, estimated tokens={self.last_sent_tokens}")
            self._write_context_stats(content_blocks, "grok", chat_id, result['index'])
            if self.last_sent_tokens > max_tokens:
                raise ValueError(f"Context exceeds token limit: {self.last_sent_tokens} > {max_tokens}")
        except Exception as e:
            logging.error(f"Failed to pack content blocks for chat_id={chat_id}: {str(e)}")
            traceback.print_exc()
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
            logging.debug(f"Using search_parameters for user_id={user_id}: {search_parameters}")
        else:
            user_id = 2  # Fallback to system user if no posts

        llm_actors = [actor for actor in self.actors if actor.llm_connection]
        logging.debug(f"Found {len(llm_actors)} LLM actors: {[actor.user_id for actor in llm_actors]}")
        processed_actors = set()
        for actor in llm_actors:
            if actor.user_id in processed_actors:
                logging.debug(f"Skipping duplicate actor_id={actor.user_id}")
                continue
            processed_actors.add(actor.user_id)
            if exclude_source_id and actor.user_id == exclude_source_id:
                logging.debug(f"Skipping actor_id={actor.user_id} due to exclude_source_id")
                continue
            latest_post = self.db.fetch_one(
                'SELECT user_id, message, id FROM posts WHERE chat_id = :chat_id ORDER BY id DESC LIMIT 1',
                {'chat_id': chat_id}
            )
            if latest_post and latest_post[0] == actor.user_id:
                logging.debug(f"Skipping replication for actor_id={actor.user_id}: last message is from this actor")
                continue
            should_respond = False
            triggered_by = user_id
            if latest_post:
                message = latest_post[1]
                user_name = self.user_manager.get_user_name(actor.user_id)
                triggered_by = latest_post[2]  # Используем id поста как triggered_by
                if re.search(f'@{user_name}|@all', message, re.IGNORECASE) or '#critics_allowed' in message:
                    should_respond = True
            context_file = Path(f"/app/logs/context-{actor.user_name}.log")
            stats_file = Path(f"/app/logs/{actor.user_name}_context.stats")
            try:
                with open(context_file, "w", encoding="utf-8") as f:
                    f.write(context)
                logging.info(f"Saved context to {context_file} for user_id={actor.user_id}, size={len(context)} chars")
            except Exception as e:
                logging.error(f"Failed to save context to {context_file}: {str(e)}")
                self.post_manager.add_message(chat_id, 2, f"Failed to save context for {actor.user_name}: {str(e)}")
            if debug_mode:
                debug_file = Path(
                    f"/app/logs/debug_{actor.user_name}_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.txt")
                with open(debug_file, "w", encoding="utf-8") as f:
                    f.write(context)
                logging.info(f"Saved debug context to {debug_file} for user_id={actor.user_id}")
            else:
                logging.debug(f"Sending to LLM for user_id={actor.user_id}: {len(context)} chars")
                try:
                    if search_parameters.get("mode") == "off":
                        logging.debug(f"Search disabled for user_id={user_id}")
                        response = await actor.llm_connection.call(context)
                    else:
                        response = await actor.llm_connection.call(context, search_parameters=search_parameters)
                    if response:
                        self.last_num_sources_used = response.get('usage', {}).get('num_sources_used', 0)
                        original_response = response.get('text', '')
                        processed_response = self.post_processor.process_response(chat_id, actor.user_id, original_response)
                        logging.debug(f"Original response: {original_response[:50]}..., Processed response: {processed_response[:50]}...")
                        if processed_response != original_response:  # Если llm_hands вернул обработанный ответ
                            should_respond = True  # Публикуем ответ от llm_hands
                        if not should_respond:
                            processed_response = "✅"
                        self._store_response(actor_id=actor.user_id, chat_id=chat_id, original_response=original_response,
                                             processed_response=processed_response, triggered_by=triggered_by)
                        logging.debug(
                            f"Received LLM response for user_id={actor.user_id}, num_sources_used={self.last_num_sources_used}, tokens={self.last_sent_tokens}")
                    else:
                        logging.warning(f"No response received from LLM for user_id={actor.user_id}")
                        self.post_manager.add_message(chat_id, 2, f"No response from LLM for {actor.user_name}")
                except Exception as e:
                    error_msg = f"LLM error for {actor.user_name}: {str(e)}"
                    logging.error(error_msg)
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
                logging.debug(f"Executing llm_context update with params: {params}")
                try:
                    self.llm_context_table.insert_or_replace(params)
                    logging.debug(
                        f"Updated llm_context for actor_id={actor.user_id}, chat_id={chat_id}, last_post_id={max_post_id}")
                except Exception as e:
                    logging.error(
                        f"Failed to update llm_context for actor_id={actor.user_id}, chat_id={chat_id}: {str(e)}")
                    traceback.print_exc()

    async def replicate_to_llm(self, chat_id, exclude_source_id=None, debug_mode: bool = None):
        debug_mode = self.debug_mode if debug_mode is None else debug_mode
        replication_key = (chat_id, exclude_source_id)
        if replication_key in self.active_replications:
            logging.debug(
                f"Skipping replication for chat_id={chat_id}, exclude_source_id={exclude_source_id}: already in progress")
            return
        self.active_replications.add(replication_key)
        try:
            logging.debug(
                f"Starting replication for chat_id={chat_id}, debug_mode={debug_mode}, exclude_source_id={exclude_source_id}")
            file_ids = set()
            file_map = {}
            users = []
            user_rows = self.db.fetch_all('SELECT user_id, user_name, llm_class FROM users')
            logging.debug(f"Loaded {len(user_rows)} users for index: {[(row[0], row[1]) for row in user_rows]}")
            for row in user_rows:
                user_id, username, llm_class = row
                role = 'LLM' if llm_class else (
                    'admin' if username == 'admin' else 'mcp' if username == 'agent' else 'developer')
                users.append({"user_id": user_id, "username": username, "role": role})
            content_blocks = self._assemble_posts(chat_id, exclude_source_id, file_ids, file_map)
            content_blocks.extend(self._assemble_files(file_ids, file_map))
            await self._pack_and_send(content_blocks, users, chat_id, exclude_source_id, debug_mode)
            logging.debug(f"Replication completed for chat_id={chat_id}, exclude_source_id={exclude_source_id}")
        finally:
            self.active_replications.remove(replication_key)

    def _store_response(self, actor_id, chat_id, original_response, processed_response, triggered_by):
        user_name = self.user_manager.get_user_name(actor_id)
        messages = self.db.fetch_all(
            'SELECT message FROM posts WHERE chat_id = :chat_id AND user_id = :user_id ORDER BY id DESC LIMIT 1',
            {'chat_id': chat_id, 'user_id': actor_id}
        )
        should_add_to_posts = processed_response != "✅"
        for (message,) in messages:
            if processed_response == message:
                logging.debug(
                    f"Skipping duplicate LLM response for chat_id={chat_id}, actor_id={actor_id}: {processed_response[:50]}...")
                return
            if re.search(f'@{user_name}|@all', message, re.IGNORECASE) or '#critics_allowed' in message:
                should_add_to_posts = True
                break
        logging.debug(
            f"Storing response for chat_id={chat_id}, actor_id={actor_id}, should_add_to_posts={should_add_to_posts}, triggered_by={triggered_by}")
        if should_add_to_posts and processed_response.strip():
            self.post_manager.add_message(chat_id, actor_id, processed_response)
            logging.debug(f"Added processed response to posts for chat_id={chat_id}, actor_id={actor_id}")
        self.llm_responses_table.insert_into(
            {
                'actor_id': actor_id,
                'chat_id': chat_id,
                'response_text': original_response,  # Сохраняем оригинальный ответ модели
                'timestamp': int(datetime.datetime.now(datetime.UTC).timestamp()),
                'triggered_by': triggered_by
            }
        )
        logging.debug(f"Stored original response for actor_id={actor_id}, chat_id={chat_id}")