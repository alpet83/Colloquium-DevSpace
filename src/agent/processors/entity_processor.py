# /app/agent/processors/entity_processor.py, updated 2025-08-13
import re, os
import json
import globals as g
import hashlib
from datetime import datetime
from processors.block_processor import BlockProcessor, res_error, res_success, ProcessorError

log = g.get_logger("llm_proc")


def _span_hash(code: str) -> str:
    return hashlib.md5(code.encode('utf-8')).hexdigest()

def _save_span(hash_code: str, file_id: int, start: int, end: int, timestamp: str, block_code: str):
    """Сохраняет фрагмент в таблицу file_spans."""
    meta_data = json.dumps({"start": start, "end": end, "timestamp": timestamp})
    g.file_manager.spans_table.insert_into({
        "hash": hash_code,
        "file_id": file_id,
        "meta_data": meta_data,
        "block_code": block_code
    })
    log.debug("Сохранён span hash=%s для file_id=%d", hash_code, file_id)


class SpanProcessor(BlockProcessor):
    """Обрабатывает теги для итеративного редактирования кода."""
    def __init__(self, tag):
        super().__init__(tag)
        self.file_lines = []
        self.file_mod_ts = ''
        self.block_lines = []
        self.old_lines_count = 0
        self.user_name = '@self'
        self.attrs = None

    def _load_file(self, file_id: int):
        file_name, source, project_id = self.get_file_data(file_id, self.user_name)
        self.file_lines = [''] + source.splitlines(keepends=True)
        qfn = g.project_manager.locate_file(file_name, project_id)
        mt = os.path.getmtime(qfn)
        self.file_mod_ts = datetime.utcfromtimestamp(mt).strftime(g.SQL_TIMESTAMP)
        return self.file_lines

    def _lookup_span(self, file_id: int, start: int, end: int) -> str:
        """Извлекает фрагмент кода из файла по строкам start-end."""
        lines = self._load_file(file_id)[start:end + 1]
        code = ''.join(lines)
        hash_code = _span_hash(code)
        _save_span(hash_code, file_id, start, end, self.file_mod_ts, code)
        return hash_code

    def _lookup_entity(self, file_id: int) -> str:
        """Извлекает фрагмент кода сущности по file_id, name, defined."""
        name = self.attrs.get('name')
        defined = int(self.attrs.get('defined'))
        rep_man = g.replication_manager
        chat_id = g.chat_manager.active_chat(self.user_name)
        entities = rep_man.entity_index(chat_id)  # unpacked record list (full)
        if not entities:
            log.error("Отсутствует entities_idx, вероятно не было сборки контекста")
            raise ProcessorError("Error: No entity index available, need talk with any LLM before", self.user_name)

        file_ents = []
        for entity in entities:
            if file_id == entity['file_id']:
                file_ents.append(json.dumps(entity))
            if entity['name'] == name and entity['file_id'] == file_id:
                start_line = entity['first_line']
                end_line = entity['last_line']

                dist = abs(defined - start_line)
                if dist > 3:
                    log.debug(f" Entity {entity} skipped by line distance {dist}")
                    continue

                return self._lookup_span(file_id, start_line, end_line)
        log.error("Сущность `%s` не найдена в индексе для file_id=%d, в наличии только:\n\t%s", name, file_id, "\n\t".join(file_ents))
        raise ProcessorError(f"Error: Entity `{name}` not found in index for file_id={file_id}, with start_line near {defined}", self.user_name)

    def _check_hash(self, hash_code: str, start, end):
        """Проверяет hash фрагмента кода."""
        code = ''.join(self.file_lines[start:end + 1])
        current_hash = _span_hash(code)
        if current_hash != hash_code:
            log.error("Hash mismatch for span: current=%s, expected=%s, code: %s", current_hash, hash_code, code)
            raise ProcessorError("Error: Hash mismatch for span", self.user_name)

    def _replace_span(self, block_code: str) -> str:
        """Заменяет фрагмент кода по hash и cut_lines."""
        hash_code = self.attrs.get('hash')
        cut_lines = int(self.attrs.get('cut_lines', 0))
        spt = g.file_manager.spans_table
        cond = {'hash': hash_code}
        span_data = spt.select_from(
            columns=['file_id', 'meta_data', 'block_code'],
            conditions=cond
        )
        if not span_data:
            raise ProcessorError(f"Failed load span data for {hash_code}", self.user_name)

        file_id, meta_data, old_code = span_data[0]
        meta = json.loads(meta_data)
        file_name, source, project_id = self.get_file_data(file_id, self.user_name)
        self._load_file(file_id)
        start_line = meta['start']
        end_line = meta['end']
        src_count = end_line - start_line + 1
        self._check_hash(hash_code, start_line, end_line)  # фрагмент мог устареть из-за других правок, нужен контроль
        cut_lines = min(src_count, cut_lines)              # ограничить воздействие пределами фрагмента
        new_lines = self._apply_replace(start_line, cut_lines, block_code)
        new_content = ''.join(new_lines[1:])               # с пропуском индексного смещения
        result = self.save_file(file_id, file_name, new_content, project_id, self.user_name)   # сохранение подразумевает автобэкап
        if result.is_error():
            raise ProcessorError(f"Failed save span {hash_code} into file {file_id} {file_name}")
        spt.delete_from(cond)  # Фрагмент устарел после редактирования и вычищается из БД. Заменяется фрагментом правки, для контроля
        new_start = start_line
        new_end = start_line + len(block_code.splitlines())
        new_hash = _span_hash(block_code)
        timestamp = datetime.now().strftime(g.SQL_TIMESTAMP)
        _save_span(new_hash, file_id, new_start, new_end, timestamp, block_code)
        return new_hash

    def _apply_replace(self, file_line: int, replace_lines: int, block_code: str) -> list:
        """Применяет правку, заменяя или удаляя строки в файле.

        Args:
            file_line (int): Начальная строка.
            replace_lines (int): Количество строк для замены (положительное) или удаления (отрицательное).
            block_code (str): Новый код для вставки.

        Returns:
            list: Обновлённые строки файла.
        """
        new_lines = self.file_lines.copy()
        start_idx = file_line
        if replace_lines >= 0:
            end_idx = file_line + replace_lines if replace_lines > 0 else len(new_lines)
            if not (block_code.endswith("\n") or block_code.endswith("\r")):
                block_code += "\n"  # обрезка при извлечении из тегов, нужна компенсация
            new_lines[start_idx:end_idx] = [block_code]
        else:
            end_idx = file_line + abs(replace_lines)
            del new_lines[start_idx:end_idx]
        if len(new_lines) > 0 and new_lines[0] is None:
            del new_lines[0]  # Удаляем смещение индекса
        return new_lines

    def handle_block(self, attrs: dict, block_code: str) -> dict:
        """Обрабатывает блок для итеративного редактирования.

        Args:
            attrs (dict): Атрибуты тега.
            block_code (str): Содержимое блока.

        Returns:
            dict: Результат обработки.
        """
        self.attrs = attrs
        self.user_name = user_name = self.attrs.get('user_name', '@self')

        try:
            file_id = self.attrs.get('file_id')
            if not file_id:
                log.error("Отсутствует атрибут file_id")
                raise ProcessorError("Error: Missing file_id", user_name)
            file_id = int(file_id)

            if self.tag == 'lookup_span':
                start = int(self.attrs.get('start', 0))
                end = int(self.attrs.get('end', -1))
                hash_code = self._lookup_span(file_id, start, end)
                if hash_code:
                    return res_success(user_name, f"@{user_name} code located, @span#{hash_code}")
            elif self.tag == 'lookup_entity':
                hash_code = self._lookup_entity(file_id)
                if hash_code:
                    return res_success(user_name, f"@{user_name} entity located, @span#{hash_code}")
            elif self.tag == 'replace_span':
                hash_code = self._replace_span(block_code)
                if hash_code:
                    return res_success(user_name, f"@{user_name} new code @span#{hash_code}")
            else:
                log.error("Неподдерживаемый тег %s", self.tag)
                raise ProcessorError(f"Error: Unsupported tag {self.tag}", user_name)

        except ProcessorError as e:
            return res_error(user_name, str(e))
        finally:
            self.attrs = None


class LookupSpanProcessor(SpanProcessor):
    def __init__(self):
        super().__init__('lookup_span')


class LookupEntityProcessor(SpanProcessor):
    def __init__(self):
        super().__init__('lookup_entity')


class ReplaceSpanProcessor(SpanProcessor):
    def __init__(self):
        super().__init__('replace_span')
