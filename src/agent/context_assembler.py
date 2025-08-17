# /app/agent/managers/context_assembler.py, updated 2025-08-13 18:00 EEST
# Formatted with proper line breaks and indentation for project compliance.

import re
from datetime import datetime, timezone
from managers.db import Database, DataTable
from lib.content_block import ContentBlock, SpanBlock
from lib.sandwich_pack import SandwichPack
import globals as g
import json, math, time
from pathlib import Path

log = g.get_logger("context_assembler")




def filter_input(message: str) -> str:
    filtered = re.sub(r'<traceback>[\s\S]*?</traceback>', '', message)
    if filtered != message:
        log.debug("Удалены теги <traceback> из сообщения: %s", message[:50])
    return filtered


class ContextAssembler:
    def __init__(self):
        self.db = Database.get_database()
        self._init_tables()
        self.fresh_files = set()
        self.fresh_spans = set()
        self.t = 0
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

    def assemble_files(self, attached_files: set, file_map: dict) -> list:
        content_blocks = []
        self.t += 1  # чтобы не доставал редактор, типа нужно статик
        log.debug("Сборка файлов для attached_files=%s", str(attached_files))
        unique_files = {}
        for file_id in sorted(attached_files):
            file_data = g.file_manager.get_file(file_id)
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
            file_data = g.file_manager.get_file(file_id)
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
                        timestamp=datetime.utcfromtimestamp(file_data['ts']).strftime(g.SQL_TIMESTAMP + "Z"),
                        file_id=file_id,
                        relevance=relevance
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

    def assemble_posts(self, chat_id: int, exclude_source_id: int, attached_files: set, file_map: dict) -> list:
        content_blocks = []
        log.debug("Сборка постов для chat_id=%d", chat_id)
        history = g.post_manager.scan_history(chat_id)
        log.debug("Получено %d постов для chat_id=%d", len(history), chat_id)
        count = 0
        self.fresh_files = set()
        self.fresh_spans = set()
        fresh_window = time.time() - 600
        base_rel = 10
        ref_relevance = {}

        for post in reversed(history.values()):  # история нужна реверсированной, для эффективной обработки LLM
            file_ids = set()
            post_id = post["id"]
            post_t = post['timestamp']  # expected float/int from DB
            dt = datetime.fromtimestamp(post_t, timezone.utc)
            message = filter_input(post["message"]) if post["message"] else ""
            message = message.replace('@attach#', '@attached_file#')
            message = re.sub(g.ATTACHES_REGEX, lambda m: self._resolve_file_id(m, file_ids, file_map), message, re.M)
            post_refs = re.findall(r'@post#(\d+)', message)   # если данный пост ссылается на ранние посты, будет список id
            pinned = ("#post_pinned" in message) or ("#pinned_post" in message)
            rel_offset = 20 if pinned else (base_rel - count)
            relevance = post.get('relevance', 50) + rel_offset
            relevance += ref_relevance.get(post_id, 0)

            relevance = min(100, relevance)
            relevance = max(0, relevance)
            attached_files.update(file_ids)
            if post_t >= fresh_window or count < 20:
                self.fresh_files.update(file_ids)
                # Extract @span#hash from message
                spans = re.findall(r'@span#(\w+)', message)
                self.fresh_spans.update(spans)
            if 0 == relevance:  # пост достаточно устарел, чтобы не показывать его в контексте LLM
                continue
            for ref_id in post_refs:
                ref_relevance[ref_id] = ref_relevance.get(ref_id, 0) + relevance * 0.1  # добавление релевантности более старым постам

            count += 1

            content_blocks.append(ContentBlock(
                content_text=message,
                content_type=":post",
                file_name=None,
                timestamp=dt.strftime(g.SQL_TIMESTAMP + "Z"),
                post_id=post_id,
                user_id=post["user_id"],
                relevance=math.floor(relevance)
            ))
        log.debug(" Относительная релевантность постов %s после сборки ", str(ref_relevance))
        return content_blocks

    def assemble_spans(self) -> list:
        span_blocks = []
        log.debug("Сборка spans для fresh_spans=~%s", str(self.fresh_spans))
        for hash_code in self.fresh_spans:
            span_data = g.file_manager.spans_table.select_from(
                columns=['file_id', 'meta_data', 'block_code'],
                conditions={'hash': hash_code}
            )
            if span_data:
                file_id, meta_data, block_code = span_data[0]
                meta = json.loads(meta_data)
                if not isinstance(meta, dict):
                    log.error("Failed parse metadata %s: %s", meta_data, type(meta))
                    continue
                block = SpanBlock(
                    content_text=block_code,
                    block_hash=hash_code,
                    file_id=file_id,
                    meta=meta
                )
                span_blocks.append(block)
                log.debug(
                    "Добавлен в сэндвич span hash=%s, file_id=%d, size=%d chars, metadata: %s",
                    hash_code, file_id, len(block_code), str(meta)
                )
            else:
                log.warn("Span hash=%s не найден в file_spans", hash_code)
        return span_blocks

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
            file_data = g.file_manager.get_file(file_id)
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
                file_data = g.file_manager.get_file(file_id)
                if file_data:
                    _add(file_data["file_name"])
            return "@attached_files:" + str(ids)

        log.warn("Неверное совпадение в _resolve_file_id: %s", str(match.groups()))
        return match.group(0)