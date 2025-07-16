# /agent/managers/replication.py, updated 2025-07-16 15:20 EEST
import asyncio
import logging
import re
import sys
import json
import datetime
import traceback
from pathlib import Path
from lib.sandwich_pack import SandwichPack
from lib.content_block import ContentBlock
sys.path.append("/app/agent")
from llm_api import LLMConnection, XAIConnection, OpenAIConnection
from managers.db import Database

class ReplicationManager:
    def __init__(self, user_manager, chat_manager, post_manager, file_manager, debug_mode: bool = False):
        self.user_manager = user_manager
        self.chat_manager = chat_manager
        self.post_manager = post_manager
        self.file_manager = file_manager
        self.db = Database.get_database()
        self.debug_mode = debug_mode
        self.last_sent_tokens = 0  # Хранит количество токенов последнего контекста
        SandwichPack.load_block_classes()
        self.actors = self._load_actors()
        self._init_tables()
        if debug_mode:
            logging.debug("Replication debug mode is enabled")
        else:
            logging.debug("Replication activated")
        try:
            with open('/app/docs/llm_pre_prompt.md', 'r', encoding='utf-8-sig') as f:
                self.pre_prompt = f.read()
            logging.debug("Loaded pre-prompt from /app/docs/llm_pre_prompt.md")
        except FileNotFoundError:
            logging.error("Pre-prompt file /app/docs/llm_pre_prompt.md not found, using default")
            self.pre_prompt = (
                "Parse the following JSON index and sandwich content. The index contains metadata for posts, files, and users. "
                "Posts are tagged as <post> with attributes post_id, user_id, timestamp, and relevance. "
                "Files are tagged as <rustc>, <vue>, <jss>, <python>, or <document> with file_id and mod_time. "
                "Users are listed with user_id, username, and role (admin, developer, LLM, mcp). "
                "References in posts use @attached_file#id or @attached_files#[id1,id2,...] to link to file metadata in the index. "
                "Analyze the content, prioritizing supported file types (rs, vue, js, py) and posts with higher relevance, "
                "and generate a response based on the provided chat history, files, and user roles. "
                "If addressed directly (e.g., @grok), respond with relevant analysis or actions. "
                "If #critics_allowed is present, provide constructive critique for new issues."
            )

    def _init_tables(self):
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS llm_context (
                actor_id INTEGER,
                chat_id INTEGER,
                last_post_id INTEGER,
                last_timestamp INTEGER,
                PRIMARY KEY (actor_id, chat_id),
                FOREIGN KEY (actor_id) REFERENCES users(user_id),
                FOREIGN KEY (chat_id) REFERENCES chats(chat_id)
            )
        """)
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS llm_responses (
                response_id INTEGER PRIMARY KEY AUTOINCREMENT,
                actor_id INTEGER,
                chat_id INTEGER,
                response_text TEXT,
                timestamp INTEGER,
                triggered_by INTEGER,
                FOREIGN KEY (actor_id) REFERENCES users(user_id),
                FOREIGN KEY (chat_id) REFERENCES chats(chat_id),
                FOREIGN KEY (triggered_by) REFERENCES posts(id)
            )
        """)

    def _load_actors(self):
        actors = []
        rows = self.db.fetch_all('SELECT user_id, user_name, llm_class, llm_token FROM users')
        logging.debug(f"Loaded {len(rows)} actors from users table")
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
            last_post_row = self.db.fetch_one(
                'SELECT last_post_id FROM llm_context WHERE actor_id = :actor_id AND chat_id = :chat_id',
                {'actor_id': exclude_source_id or 0, 'chat_id': cid}
            )
            last_post_id = last_post_row[0] if last_post_row else 0
            logging.debug(f"Processing chat_id={cid}, last_post_id={last_post_id}")
            parent_msg_row = self.db.fetch_one(
                'SELECT parent_msg_id FROM chats WHERE chat_id = :chat_id',
                {'chat_id': cid}
            )
            parent_msg_id = parent_msg_row[0] if parent_msg_row else None
            if parent_msg_id and parent_msg_id > last_post_id:
                parent_msg = self.db.fetch_one(
                    'SELECT id, chat_id, timestamp, user_id, message FROM posts WHERE id = :parent_msg_id',
                    {'parent_msg_id': parent_msg_id}
                )
                if parent_msg:
                    message = re.sub(r'@attach_dir#([\w\d/]+)|@attach#(\d+)', lambda m: self._resolve_file_id(m, file_ids, file_map), parent_msg[4])
                    content_blocks.append(ContentBlock(
                        content_text=message,
                        content_type=":post",
                        file_name=None,
                        timestamp=datetime.datetime.fromtimestamp(parent_msg[2], datetime.UTC).strftime("%Y-%m-%d %H:%M:%SZ"),
                        post_id=parent_msg[0],
                        user_id=parent_msg[3],
                        relevance=50
                    ))
                    logging.debug(f"Added parent post_id={parent_msg[0]} for chat_id={cid}")
                else:
                    logging.debug(f"No parent post found for parent_msg_id={parent_msg_id}, chat_id={cid}")
            history = self.db.fetch_all(
                'SELECT id, chat_id, user_id, message, timestamp FROM posts WHERE chat_id = :chat_id AND id > :last_post_id ORDER BY timestamp DESC',
                {'chat_id': cid, 'last_post_id': last_post_id}
            )
            for row in history:
                message = re.sub(r'@attach_dir#([\w\d/]+)|@attach#(\d+)', lambda m: self._resolve_file_id(m, file_ids, file_map), row[3])
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
        for file_id in file_ids:
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
                        timestamp=datetime.datetime.fromtimestamp(file_data['ts'], datetime.UTC).strftime("%Y-%m-%d %H:%M:%SZ"),
                        file_id=file_id
                    )
                    content_blocks.append(content_block)
                    logging.debug(f"Added file_id={file_id}, file_name={file_data['file_name']}, block_class={content_block.__class__.__name__}, size={len(content_text)} chars")
                    file_map[file_id] = file_data['file_name']
                except Exception as e:
                    logging.error(f"Error processing file_id={file_id}: {str(e)}")
                    continue
            else:
                logging.warning(f"File id={file_id} not found in attached_files")
        return content_blocks

    async def _pack_and_send(self, content_blocks: list, users: list, chat_id: int, exclude_source_id=None, debug_mode: bool = False):
        max_tokens = 131072  # Лимит токенов для Grok-3 API
        # Ограничиваем контекст по токенам
        content_blocks.sort(key=lambda x: x.relevance if x.relevance else 0, reverse=True)
        total_tokens = len(self.pre_prompt) // 4
        filtered_blocks = []
        for block in content_blocks:
            block_tokens = len(block.content_text) // 4 if block.content_text else 0
            if total_tokens + block_tokens <= max_tokens:
                filtered_blocks.append(block)
                total_tokens += block_tokens
            else:
                logging.debug(f"Skipping block post_id={block.post_id or 'N/A'}, file_id={block.file_id or 'N/A'} due to token limit")
        content_blocks = filtered_blocks

        # Упаковываем контент
        logging.debug(f"Packing {len(content_blocks)} content blocks")
        try:
            packer = SandwichPack(max_size=80_000, system_prompt=self.pre_prompt)
            result = packer.pack(content_blocks, users=users)
            context = f"{self.pre_prompt}\n{result['index']}\n{''.join(result['sandwiches'])}"
            self.last_sent_tokens = len(context) // 4
            logging.debug(f"Context generated, length={len(context)} chars, estimated tokens={self.last_sent_tokens}")
        except Exception as e:
            logging.error(f"Failed to pack content blocks for chat_id={chat_id}: {str(e)}")
            traceback.print_exc()
            raise

        # Отправляем или сохраняем контекст
        llm_actors = [actor for actor in self.actors if actor.llm_connection]
        logging.debug(f"Found {len(llm_actors)} LLM actors")
        for actor in llm_actors:
            if exclude_source_id and actor.user_id == exclude_source_id:
                logging.debug(f"Skipping actor_id={actor.user_id} due to exclude_source_id")
                continue
            latest_post = self.db.fetch_one(
                'SELECT user_id FROM posts WHERE chat_id = :chat_id ORDER BY timestamp DESC LIMIT 1',
                {'chat_id': chat_id}
            )
            if latest_post and latest_post[0] == actor.user_id:
                logging.debug(f"Skipping replication for actor_id={actor.user_id}: last message is from this actor")
                continue
            # Сохраняем контекст в файл
            context_file = Path(f"/app/logs/context-{actor.user_name}.log")
            try:
                with open(context_file, "w", encoding="utf-8") as f:
                    f.write(context)
                logging.info(f"Saved context to {context_file} for user_id={actor.user_id}, size={len(context)} chars")
            except Exception as e:
                logging.error(f"Failed to save context to {context_file}: {str(e)}")
            if debug_mode:
                debug_file = Path(f"/app/logs/debug_{actor.user_name}_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.txt")
                with open(debug_file, "w", encoding="utf-8") as f:
                    f.write(context)
                logging.info(f"Saved debug context to {debug_file} for user_id={actor.user_id}")
            else:
                logging.debug(f"Sending to LLM for user_id={actor.user_id}: {len(context)} chars")
                try:
                    response = await actor.llm_connection.call(context)
                    if response:
                        self._store_response(actor_id=actor.user_id, chat_id=chat_id, response_text=response, triggered_by=latest_post[0] if latest_post else None)
                except Exception as e:
                    error_msg = f"LLM error for {actor.user_name}: {str(e)}"
                    logging.error(error_msg)
                    self.post_manager.add_message(chat_id, 2, error_msg)  # user_id=2 для mcp
                    continue

        # Обновляем llm_context
        max_post_id = max([block.post_id for block in content_blocks if block.post_id is not None] or [0])
        if max_post_id:
            for actor in llm_actors:
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
                    self.db.execute(
                        'INSERT OR REPLACE INTO llm_context (actor_id, chat_id, last_post_id, last_timestamp) VALUES (:actor_id, :chat_id, :last_post_id, :last_timestamp)',
                        params
                    )
                    logging.debug(f"Updated llm_context for actor_id={actor.user_id}, chat_id={chat_id}, last_post_id={max_post_id}")
                except Exception as e:
                    logging.error(f"Failed to update llm_context for actor_id={actor.user_id}, chat_id={chat_id}: {str(e)}")
                    traceback.print_exc()

    async def replicate_to_llm(self, chat_id, exclude_source_id=None, debug_mode: bool = None):
        debug_mode = self.debug_mode if debug_mode is None else debug_mode
        logging.debug(f"Starting replication for chat_id={chat_id}, debug_mode={debug_mode}, exclude_source_id={exclude_source_id}")
        file_ids = set()
        file_map = {}
        # Собираем пользователей
        users = []
        user_rows = self.db.fetch_all('SELECT user_id, user_name, llm_class FROM users')
        for row in user_rows:
            user_id, username, llm_class = row
            role = 'LLM' if llm_class else ('admin' if username == 'admin' else 'mcp' if username == 'agent' else 'developer')
            users.append({"user_id": user_id, "username": username, "role": role})
        logging.debug(f"Loaded {len(users)} users for index")
        # Собираем посты и файлы
        content_blocks = self._assemble_posts(chat_id, exclude_source_id, file_ids, file_map)
        content_blocks.extend(self._assemble_files(file_ids, file_map))
        # Упаковываем и отправляем
        await self._pack_and_send(content_blocks, users, chat_id, exclude_source_id, debug_mode)

    def _store_response(self, actor_id, chat_id, response_text, triggered_by):
        user_name = self.user_manager.get_user_name(actor_id)
        messages = self.db.fetch_all(
            'SELECT message FROM posts WHERE chat_id = :chat_id ORDER BY timestamp DESC',
            {'chat_id': chat_id}
        )
        should_add_to_posts = False
        for (message,) in messages:
            if re.search(f'@{user_name}|@all', message, re.IGNORECASE) or '#critics_allowed' in message:
                should_add_to_posts = True
                break
        logging.debug(f"Should add LLM response to posts for chat_id={chat_id}, actor_id={actor_id}: {should_add_to_posts}")
        if should_add_to_posts:
            self.post_manager.add_message(chat_id, actor_id, response_text)
            logging.debug(f"Added LLM response to chat_id={chat_id} from actor_id={actor_id}")
        self.db.execute(
            'INSERT INTO llm_responses (actor_id, chat_id, response_text, timestamp, triggered_by) VALUES (:actor_id, :chat_id, :response_text, :timestamp, :triggered_by)',
            {
                'actor_id': actor_id,
                'chat_id': chat_id,
                'response_text': response_text,
                'timestamp': int(datetime.datetime.now(datetime.UTC).timestamp()),
                'triggered_by': triggered_by
            }
        )
        logging.debug(f"Stored response for actor_id={actor_id}, chat_id={chat_id}")

class ChatActor:
    def __init__(self, user_id, user_name, llm_class=None, llm_token=None, post_manager=None):
        self.user_id = user_id
        self.user_name = user_name
        self.llm_connection = None
        if llm_class and llm_token:
            config = {"api_key": llm_token, "model": llm_class}
            model = llm_class.lower()
            if "grok" in model:
                self.llm_connection = XAIConnection(config)
            elif model == 'chatgpt':
                self.llm_connection = OpenAIConnection(config)
            else:
                self.llm_connection = LLMConnection(config)