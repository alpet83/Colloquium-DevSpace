# /app/agent/managers/context_assembler.py, updated 2025-07-26 16:30 EEST
import re
from datetime import datetime, timezone
from managers.db import Database, DataTable
from lib.content_block import ContentBlock
from lib.sandwich_pack import SandwichPack
import globals as g
import json, math, time
from pathlib import Path

log = g.get_logger("context_assembler")

class ContextAssembler:
    def __init__(self):
        self.db = Database.get_database()
        self.chat_manager = g.chat_manager
        self.file_manager = g.file_manager
        self._init_tables()
        self.fresh_files = set()
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

    def assemble_posts(self, chat_id: int, exclude_source_id: int, attached_files: set, file_map: dict) -> list:
        content_blocks = []
        log.debug("Сборка постов для chat_id=%d", chat_id)
        history = g.post_manager.scan_history(chat_id)
        log.debug("Получено %d постов для chat_id=%d", len(history), chat_id)
        count = 0
        self.fresh_files = set()
        fresh_window = time.time() - 600
        base_rel = 10
        for post in reversed(history.values()):  # история нужна реверсированной, для эффективной обработки LLM
            file_ids = set()
            post_t = post['timestamp']  # expected float/int from DB
            dt = datetime.fromtimestamp(post_t, timezone.utc)
            message = self.filter_input(post["message"]) if post["message"] else ""
            message = message.replace('@attach#', '@attached_file#')
            message = re.sub(g.ATTACHES_REGEX, lambda m: self._resolve_file_id(m, file_ids, file_map), message)
            relevance = post.get('relevance', 50) + base_rel
            relevance = min(100, relevance)
            relevance = max(0, relevance)
            count += 1

            if file_ids and (post_t >= fresh_window or count < 10 or relevance > 50):
                log.debug("[-%d] relevance = %d, файлы %s оценены как свежие для включения в контекст ", count, relevance, str(file_ids))
                self.fresh_files.update(file_ids)   # only this files content will be added to sandwich
            elif count < 20:
                log.debug("[-%d], file_set %s ", count, file_ids)

            attached_files.update(file_ids)
            base_rel -= 1

            content_blocks.append(ContentBlock(
                content_text=message,
                content_type=":post",
                file_name=None,
                timestamp=dt.strftime("%Y-%m-%d %H:%M:%SZ"),
                post_id=post["id"],
                user_id=post["user_id"],
                relevance=math.floor(relevance)
            ))
            if count <= 10:
                log.debug("Добавлен пост [-%d] post_id=%d для chat_id=%d", count, post["id"], post["chat_id"])
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
                    relevance = file_data.get('relevance', 50)
                    if '.rulz' == extension:
                        relevance = 100

                    content_block = SandwichPack.create_block(
                        content_text=content_text,
                        content_type=extension,
                        file_name=file_data['file_name'],
                        timestamp=datetime.fromtimestamp(file_data['ts'], timezone.utc).strftime(
                            "%Y-%m-%d %H:%M:%SZ"),
                        file_id=file_id,
                        relevance=relevance
                    )
                    content_blocks.append(content_block)
                    """
                    log.debug(
                        "Добавлен в сэндвич file_id=%d, file_name=%s, block_class=%s, size=%d chars",
                        file_id, file_data['file_name'], content_block.__class__.__name__, len(content_text)
                    )
                    """
                    file_map[file_id] = file_data['file_name']
                except Exception as e:
                    log.excpt("Ошибка обработки file_id=%d: ", file_id, e=e)
                    continue
            else:
                log.warn("Файл file_id=%d не найден в attached_files", file_id)
        return content_blocks

    def _resolve_file_id(self, match, file_ids: set, file_map: dict) -> str:
        file_id_list = []  # local id-list
        file_id = 0

        def _add(_name: str):
            file_ids.add(file_id)
            file_map[file_id] = _name
            file_id_list.append(str(file_id))

        if match.group(1):  # @attach_dir#dir_name
            g.project_manager.scan_project_files()
            dir_name = match.group(1)
            log.debug("Обработка @attach_dir#%s", dir_name)
            query = "SELECT id, file_name FROM attached_files WHERE file_name LIKE :dir_name OR file_name LIKE :ref_name"
            rows = self.db.fetch_all(query, {'dir_name': f"{dir_name}%", 'ref_name': f"@{dir_name}%"})
            for row in rows:
                file_id = row[0]
                _add(row[1])

            if not file_id_list:
                log.debug("Отсутствуют файлы для каталога в БД")
                return f"#no_files_at:{dir_name}"

            result = f"@attached_files#[{','.join(file_id_list)}]"
            log.debug("\tНайденные файлы: %s", result)
            return result
        elif match.group(2):  # @attached_file#file_id
            file_id = int(match.group(2))
            file_data = self.file_manager.get_file(file_id)
            if file_data:
                _add(file_data['file_name'])
                # log.debug("Разрешён file_id=%d, file_name=%s", file_id, file_data['file_name'])
            else:
                log.warn("Файл file_id=%d не найден в attached_files", file_id)
            return f"@attached_file#{file_id}"
        elif match.group(3):  # @attach_index#chat_id
            chat_id = int(match.group(3))
            index_file = Path(g.CHAT_META_DIR) / f"{chat_id}-index.json"
            # TODO: check string id is allowed
            if index_file.exists():
                file_id = f"index_{chat_id}"
                _add(str(index_file))
                log.debug("Разрешён индекс chat_id=%d, file=%s", chat_id, str(index_file))
            else:
                log.warn("Индекс для chat_id=%d не найден в %s", chat_id, str(index_file))
            return f"@attached_index#{chat_id}"
        elif match.group(4):  # @attached_files#[1,2,3] - включение при явном цитировании списка файлов от LLM
            ids = match.group(4).split(',')
            log.debug(" Обработка @attached_files#%s", str(ids))
            for file_id in ids:
                file_id = file_id.strip("\"' ")
                if 'index_' in file_id:
                    continue
                file_id = int(file_id)
                file_data = self.file_manager.get_file(file_id)
                if file_data:
                    _add(file_data["file_name"])
            return "@attached_files:" + str(ids)

        log.warn("Неверное совпадение в _resolve_file_id: %s", str(match.groups()))
        return match.group(0)
