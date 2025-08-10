# /app/agent/processors/entity_processor.py, updated 2025-07-29 14:20 EEST
import re
import json
import globals
from processors.block_processor import BlockProcessor, res_error, res_success, ProcessorError

log = globals.get_logger("llm_proc")

class EntityUpdateProcessor(BlockProcessor):
    """Обрабатывает тег <entity_update> для обновления кода сущности по file_id и имени."""
    def __init__(self):
        super().__init__('entity_update')
        self.replace = False
        self.file_lines = None
        self.block_lines = None
        self.old_lines_count = 0
        self.attrs = None

    def _check_lines(self, entity_name: str, file_id: int, entity_end_line: int):
        """Проверяет соответствие контекстных строк между файлом и блоком кода, загружает context_pairs.

        Args:
            entity_name (str): Имя сущности.
            file_id (int): ID файла.
            entity_end_line (int): Граница сущности.

        Raises:
            ProcessorError: Если контекстные строки не соответствуют или их недостаточно.
        """
        context_lines = self.attrs.get('context_lines', '')
        context_pairs = []
        if context_lines:
            try:
                for pair in context_lines.split(','):
                    block_line, offset = map(int, pair.split(':'))
                    file_line = self.attrs['file_line'] + offset
                    context_pairs.append((block_line, file_line))
                if 0 == len(context_pairs):
                    raise ValueError
            except ValueError:
                log.error("Неверный формат context_lines: %s", context_lines)
                raise ProcessorError(
                    "Error: Invalid context_lines format, expected pairs like 'block_line:offset'",
                    self.attrs.get('user_name', '@self')
                )
        else:
            log.warn("Правка сущности применяется без проверки имеющегося кода")
            return  # no checks

        for block_line, file_line_num in context_pairs:
            if file_line_num >= len(self.file_lines) or not self.file_lines[file_line_num]:
                log.error("Контекстная строка %d недоступна в файле file_id=%d", file_line_num, file_id)
                raise ProcessorError(f"Error: Context line {file_line_num} not available in file_id={file_id}")
            if block_line > len(self.block_lines) or not self.block_lines[block_line - 1]:
                log.error("Контекстная строка %d недоступна в block_code", block_line)
                raise ProcessorError(f"Error: Context line {block_line} not available in block_code")
            if self.file_lines[file_line_num].rstrip() != self.block_lines[block_line - 1].rstrip():
                log.error(
                    "Несоответствие контекстной строки %d: file='%s' vs block='%s'",
                    file_line_num, self.file_lines[file_line_num].rstrip(), self.block_lines[block_line - 1].rstrip()
                )
                raise ProcessorError(f"Error: Context line {file_line_num} mismatch")

    def _load_file(self, file_id: int, user_name: str) -> tuple:
        """Загружает данные файла и инициализирует self.file_lines и self.old_lines_count.

        Args:
            file_id (int): ID файла.
            user_name (str): Имя пользователя.

        Returns:
            tuple: (file_name, project_id)

        Raises:
            ProcessorError: Если файл не найден или не читается.
        """
        file_name, source, project_id = self.get_file_data(file_id, user_name)
        self.file_lines = [None] + source.splitlines(keepends=True)  # 1-based indexing
        self.old_lines_count = len(self.file_lines) - 1
        return file_name, project_id

    def _load(self, user_name: str) -> tuple:
        """Находит сущность по file_id и name в last_sandwich_idx и загружает файл.

        Args:
            user_name (str): Имя пользователя.

        Returns:
            tuple: (entity_type, file_line, end_line, file_name, project_id)

        Raises:
            ProcessorError: Если индекс, файл или сущность не найдены.
        """
        file_manager = globals.file_manager
        file_data = file_manager.get_file(self.attrs['file_id'])
        if not file_data:
            log.error("Неверный file_id=%d", self.attrs['file_id'])
            raise ProcessorError(f"Error: Invalid file_id {self.attrs['file_id']}", user_name)

        index_json = globals.replication_manager.last_sandwich_idx
        if not index_json:
            log.error("Индекс сущностей недоступен")
            raise ProcessorError("Error: Entity index not available", user_name)

        try:
            index = json.loads(index_json)
        except json.JSONDecodeError as e:
            log.error("Ошибка парсинга JSON индекса: %s", str(e))
            raise ProcessorError("Error: Failed to parse entity index", user_name)

        entities = index.get('entities', [])
        entity = None
        for e in entities:
            parts = e.split(',')
            if len(parts) == 6 and parts[3] == str(self.attrs['file_id']) and parts[2] == self.attrs['name']:
                entity = e
                break
        if not entity:
            log.error("В файле %s нет сущности с именем %s", file_data['file_name'], self.attrs['name'])
            raise ProcessorError(
                f"Error: In file {file_data['file_name']} no entity with name {self.attrs['name']} found", user_name
            )

        # Разбираем строку сущности: vis,тип,имя,file_id,start_line-end_line,tokens
        parts = entity.split(',')
        if len(parts) != 6:
            log.error("Неверный формат строки сущности: %s", entity)
            raise ProcessorError(f"Error: Invalid entity format: {entity}", user_name)
        entity_type = parts[1]
        line_range = parts[4]
        try:
            start_line, end_line = map(int, line_range.split('-'))
        except ValueError:
            log.error("Неверный формат диапазона строк: %s", line_range)
            raise ProcessorError(f"Error: Invalid line range format: {line_range}", user_name)

        file_name, project_id = self._load_file(self.attrs['file_id'], user_name)
        return entity_type, start_line, end_line, file_name, project_id

    def _apply_replace(self, file_line: int, replace_lines: int, block_code: str) -> list:
        """Применяет замену или удаление строк в self.file_lines.

        Args:
            file_line (int): Начальная строка для замены.
            replace_lines (int): Количество строк для замены (или удаления, если < 0).
            block_code (str): Новый код для вставки.

        Returns:
            list: Обновлённые строки файла.
        """
        new_lines = self.file_lines.copy()
        start_idx = file_line
        if replace_lines >= 0:
            end_idx = file_line + replace_lines if replace_lines > 0 else len(new_lines)
            new_content = block_code.rstrip()
            new_lines[start_idx:end_idx] = [new_content]
        else:
            end_idx = file_line + abs(replace_lines)
            del new_lines[start_idx:end_idx]
        if len(new_lines) > 0 and new_lines[0] is None:
            del new_lines[0]  # Удаляем смещение индекса
        return new_lines

    def handle_block(self, attrs: dict, block_code: str) -> dict:
        """Обрабатывает блок <entity_update> для замены кода сущности.

        Args:
            attrs (dict): Атрибуты тега (file_id, name, lines, context_lines).
            block_code (str): Новый код для сущности.

        Returns:
            dict: Результат обработки (успех или ошибка).
        """
        self.attrs = attrs
        user_name = self.attrs.get('user_name', '@self')
        try:
            file_id = self.attrs.get('file_id')
            if not file_id:
                log.error("Отсутствует атрибут file_id в entity_update")
                raise ProcessorError("Error: Missing file_id", user_name)
            try:
                self.attrs['file_id'] = int(file_id)
            except ValueError:
                log.error("Неверный формат file_id: %s", file_id)
                raise ProcessorError("Error: Invalid file_id format", user_name)

            if not self.attrs.get('name'):
                log.error("Отсутствует атрибут name в entity_update")
                raise ProcessorError("Error: Missing entity name", user_name)

            lines = self.attrs.get('lines')
            lines_count = None
            if lines:
                try:
                    lines_count = int(lines)
                except ValueError:
                    log.error("Неверный формат lines: %s", lines)
                    raise ProcessorError("Error: Invalid lines format", user_name)

            # Загружаем сущность и файл
            entity_type, file_line, end_line, file_name, project_id = self._load(user_name)
            self.attrs['file_line'] = file_line  # Сохраняем file_line для _check_lines
            self.block_lines = block_code.splitlines(keepends=True)
            replace_lines = len(self.block_lines) if lines_count is None else lines_count

            # Проверяем, что правка не превышает границы сущности
            if replace_lines > 0 and file_line + replace_lines - 1 > end_line:
                log.error(
                    "Правка для entity_name=%s превышает границу сущности на строке %d",
                    self.attrs['name'], end_line
                )
                raise ProcessorError(
                    f"Error: Update for entity `{self.attrs['name']}` exceeds entity boundary at line {end_line}",
                    user_name
                )

            # Проверяем контекстные строки
            self._check_lines(self.attrs['name'], self.attrs['file_id'], end_line)

            # Применяем правку
            new_lines = self._apply_replace(file_line, replace_lines, block_code)
            if new_lines == self.file_lines[1:]:
                log.debug("Правка для entity_name=%s не вносит изменений", self.attrs['name'])
                return res_success(
                    user_name,
                    f"Файл @attach#{self.attrs['file_id']} не изменён, было {self.old_lines_count} строк, "
                    f"осталось {self.old_lines_count} строк"
                )

            result = self.save_file(self.attrs['file_id'], file_name, ''.join(new_lines), project_id, user_name)
            return result
        except ProcessorError as e:
            return res_error(user_name, str(e))
        finally:
            self.attrs = None  # Очищаем self.attrs после обработки