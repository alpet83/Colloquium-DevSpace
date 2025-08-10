# /app/agent/managers/context_assembler.py, updated 2025-07-26 16:30 EEST
import re
from datetime import datetime, timezone
from managers.db import Database, DataTable
from lib.content_block import ContentBlock
from lib.sandwich_pack import SandwichPack
import globals
import json
from pathlib import Path

log = globals.get_logger("context_assembler")

class ContextAssembler:
    def __init__(self):
        self.db = Database.get_database()
        self.chat_manager = globals.chat_manager
        self.file_manager = globals.file_manager
        self._init_tables()
        res = SandwichPack.load_block_classes()
        log.debug("Загружены парсеры сэндвич-блоков: %s", str(res))

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

    def filter_input(self, message: str) -> str:
        filtered = re.sub(r'<traceback>[\s\S]*?</traceback>', '', message)
        if filtered != message:
            log.debug("Удалены теги <traceback> из сообщения: %s", message[:50])
        return filtered

    def assemble_posts(self, chat_id, exclude_source_id, file_ids: set, file_map: dict) -> list:
        content_blocks = []
        log.debug("Сборка постов для chat_id=%d", chat_id)
        history = globals.post_manager.scan_history(chat_id)
        log.debug("Получено %d постов для chat_id=%d", len(history), chat_id)
        for post in reversed(history.values()):  # история обязательно нужна реверсированной, для эффективной обработки LLM
            message = self.filter_input(post["message"]) if post["message"] else ""
            message = message.replace('@attach#', '@attached_file#')
            message = re.sub(r'@attach_dir#([\w\d/]+)|@attached_file#(\d+)|@attach_index#(\d+)',
                             lambda m: self._resolve_file_id(m, file_ids, file_map), message)
            content_blocks.append(ContentBlock(
                content_text=message,
                content_type=":post",
                file_name=None,
                timestamp=datetime.fromtimestamp(post["timestamp"], timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ"),
                post_id=post["id"],
                user_id=post["user_id"],
                relevance=50
            ))
            log.debug("Добавлен пост post_id=%d для chat_id=%d", post["id"], post["chat_id"])
        return content_blocks

    def assemble_files(self, file_ids: set, file_map: dict) -> list:
        content_blocks = []
        log.debug("Сборка файлов для file_ids=~%s", str(file_ids))
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
                        timestamp=datetime.fromtimestamp(file_data['ts'], timezone.utc).strftime(
                            "%Y-%m-%d %H:%M:%SZ"),
                        file_id=file_id
                    )
                    content_blocks.append(content_block)
                    log.debug(
                        "Добавлен в сэндвич file_id=%d, file_name=%s, block_class=%s, size=%d chars",
                        file_id, file_data['file_name'], content_block.__class__.__name__, len(content_text)
                    )
                    file_map[file_id] = file_data['file_name']
                except Exception as e:
                    log.excpt("Ошибка обработки file_id=%d: ", file_id, e=e)
                    continue
            else:
                log.warn("Файл file_id=%d не найден в attached_files", file_id)
        return content_blocks

    def _resolve_file_id(self, match, file_ids: set, file_map: dict) -> str:
        if match.group(1):  # @attach_dir#dir_name
            globals.project_manager.scan_project_files()
            dir_name = match.group(1)
            log.debug("Обработка @attach_dir#%s", dir_name)
            query = "SELECT id, file_name FROM attached_files WHERE file_name LIKE :dir_name OR file_name LIKE :ref_name"
            rows = self.db.fetch_all(query, {'dir_name': f"{dir_name}%", 'ref_name': f"@{dir_name}%"})
            file_id_list = [str(row[0]) for row in rows]
            if not file_id_list:
                log.debug("Отсутствуют файлы для каталога в БД")
                return f"#no_files_at:{dir_name}"
            for row in rows:
                file_ids.add(row[0])
                file_map[row[0]] = row[1]
            result = f"@attached_files#[{','.join(file_id_list)}]"
            log.debug("\tНайденные файлы: %s", result)
            return result
        elif match.group(2):  # @attached_file#file_id
            file_id = int(match.group(2))
            file_data = self.file_manager.get_file(file_id)
            if file_data:
                file_ids.add(file_id)
                file_map[file_id] = file_data['file_name']
                log.debug("Разрешён file_id=%d, file_name=%s", file_id, file_data['file_name'])
            else:
                log.warn("Файл file_id=%d не найден в attached_files", file_id)
            return f"@attached_file#{file_id}"
        elif match.group(3):  # @attach_index#chat_id
            chat_id = int(match.group(3))
            index_file = Path(globals.CHAT_META_DIR) / f"{chat_id}-index.json"
            if index_file.exists():
                file_ids.add(f"index_{chat_id}")
                file_map[f"index_{chat_id}"] = str(index_file)
                log.debug("Разрешён индекс chat_id=%d, file=%s", chat_id, str(index_file))
            else:
                log.warn("Индекс для chat_id=%d не найден в %s", chat_id, str(index_file))
            return f"@attached_index#{chat_id}"
        log.warn("Неверное совпадение в _resolve_file_id: %s", str(match.groups()))
        return match.group(0)