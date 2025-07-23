# /app/agent/managers/context_assembler.py, updated 2025-07-19 17:49 EEST
import re
import datetime
from managers.db import Database, DataTable
from lib.content_block import ContentBlock
from lib.sandwich_pack import SandwichPack
import globals

log = globals.get_logger("context_assembler")

class ContextAssembler:
    def __init__(self):
        self.db = Database.get_database()
        self.chat_manager = globals.chat_manager
        self.file_manager = globals.file_manager
        self._init_tables()
        SandwichPack.load_block_classes()

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
                "rql INTEGER",
                "FOREIGN KEY (actor_id) REFERENCES users(user_id)",
                "FOREIGN KEY (chat_id) REFERENCES chats(chat_id)",
                "FOREIGN KEY (triggered_by) REFERENCES posts(id)"
            ]
        )


    def assemble_posts(self, chat_id, exclude_source_id, file_ids: set, file_map: dict) -> list:
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
            for row in reversed(history):
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

    def assemble_files(self, file_ids: set, file_map: dict) -> list:
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
                    content_text = file_data['content']
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

    def _resolve_file_id(self, match, file_ids: set, file_map: dict) -> str:
        if match.group(1):  # @attach_dir#dir_name
            globals.project_manager.scan_project_files()
            dir_name = match.group(1)
            log.debug("Обработка @attach_dir#%s", dir_name)
            query = "SELECT id, file_name FROM attached_files\n" + \
                " WHERE (file_name LIKE :dir_name) OR (file_name LIKE :ref_name)"
            rows = self.db.fetch_all(query, {'dir_name': f"{dir_name}%", 'ref_name': f"@{dir_name}%"})
            file_id_list = [str(row[0]) for row in rows]
            if 0 == len(file_id_list):
                log.debug("Отсутствуют файлы для каталога в БД")
                return f"#no_files_at:{dir_name}"

            for row in rows:
                file_ids.add(row[0])
                file_map[row[0]] = row[1]

            result = f"@attached_files#[{','.join(file_id_list)}]"
            log.debug("\tНайденные файлы: %s", result)
            return result
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