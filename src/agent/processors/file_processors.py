# /app/agent/processors/file_processors.py, updated 2025-07-27 15:00 EEST
import re
import time
import globals as g
from datetime import datetime
from pathlib import Path
from processors.block_processor import BlockProcessor, res_error, res_success, ProcessorError

log = g.get_logger("llm_proc")


class FileEditProcessor(BlockProcessor):
    """Обрабатывает тег <code_file> для создания или модификации файлов."""
    def __init__(self):
        super().__init__('code_file')

    async def handle_block(self, attrs: dict, block_code: str) -> dict:
        """Обрабатывает блок <code_file> для создания или обновления файла.

        Args:
            attrs (dict): Атрибуты тега (например, name).
            block_code (str): Содержимое файла.

        Returns:
            dict: Результат обработки (успех или ошибка).
        """
        file_name = str(attrs.get('name', 'dummy.txt'))
        user_name = attrs.get('user_name', '@self')
        if not file_name:
            log.error("Отсутствует атрибут name в code_file")
            return res_error(user_name, "Error: Missing file name")

        log.debug("Обработка code_file: file_name=%s, content_length=%d", file_name, len(block_code))
        proj_man = g.project_manager
        project_id = proj_man.project_id if proj_man and hasattr(proj_man, 'project_id') else None
        if project_id is None:
            log.error("Нет активного проекта для обработки code_file")
            return res_error(user_name, "Error: No active project selected")
        project_name = proj_man.project_name
        root = file_name.startswith('/')
        applied = file_name.startswith(project_name)

        if (not root) and (not applied):
            file_name = f"{project_name}/{file_name}"
            log.debug("Добавлен префикс project_name к file_name: %s", file_name)
        else:
            file_name = file_name.lstrip('/')

        try:
            safe_path = (proj_man.projects_dir / file_name).resolve()
            if not str(safe_path).startswith('/app/projects'):
                log.error("Недопустимый путь файла: %s", file_name)
                return res_error(user_name, "Error: File path outside /app/projects")
        except Exception as e:
            log.excpt("Ошибка проверки пути файла %s: ", file_name, e=e)
            return res_error(user_name, "Error: Invalid file path")

        file_man = g.file_manager
        file_id = file_man.find(file_name, project_id)
        action = "сохранён" if file_id else "создан"

        if file_id:
            res = self.save_file(file_id, file_name, block_code, project_id, user_name)
        else:
            file_id = file_man.add_file(
                file_name=file_name,
                content=block_code,
                timestamp=int(time.time()),
                project_id=project_id
            )
            if file_id is None or file_id < 0:
                return res_error(user_name, f"Ошибка создания файла {file_name}: {file_id}")
            res = res_success(user_name, f"Файл @attach#{file_id} успешно {action}", '')

        if res.is_ok():
            res.processed_message = f"@attach#{file_id}"
        return res


class FileUndoProcessor(BlockProcessor):
    """Обрабатывает тег <undo> для восстановления предыдущей версии файла."""
    def __init__(self):
        super().__init__('undo')
        self.replace = False

    async def handle_block(self, attrs: dict, block_code: str) -> dict:
        """Обрабатывает блок <undo> для восстановления файла из бэкапа.

        Args:
            attrs (dict): Атрибуты тега (например, file_id, time_back).
            block_code (str): Содержимое тега (не используется).

        Returns:
            dict: Результат обработки (успех или ошибка).
        """
        user_name = attrs.get('user_name', '@self')
        try:
            file_id = self.validate_file_id(attrs.get('file_id'), user_name)
            time_back = attrs.get('time_back')
            if not time_back:
                log.error("Отсутствует атрибут time_back в undo")
                raise ProcessorError("Error: Missing time_back", user_name)

            try:
                time_back = int(time_back)
            except ValueError:
                log.error("Неверный формат time_back: %s", time_back)
                raise ProcessorError("Error: Invalid time_back format", user_name)

            log.debug("Processing undo for file_id=%d, time_back=%d", file_id, time_back)
            file_name, source, project_id = self.get_file_data(file_id, user_name)
            proj_man = g.project_manager
            if proj_man.projects_dir is None:
                raise ProcessorError("Error: No project selected for undo file", user_name)
            proj_dir = str(proj_man.projects_dir)
            file_path = proj_man.locate_file(file_name, project_id)
            backed_file = str(file_path).replace(proj_dir, proj_dir + '/backups')
            backed_file = Path(backed_file)
            backup_dir = backed_file.parent
            backup_pattern = str(backed_file.name) + '.*'

            current_time = int(time.time())
            latest_backup = None
            latest_timestamp = 0
            oldest = 0
            checked = 0
            log.debug("Сканирование последнего файла %s в %s", backup_pattern, str(backup_dir))
            for backup_path in backup_dir.glob(backup_pattern):
                try:
                    checked += 1
                    timestamp = int(backup_path.suffix[1:])
                    oldest = max(oldest, timestamp)
                    age = current_time - timestamp
                    sdt = datetime.utcfromtimestamp(timestamp).strftime('%Y-%m-%d %H:%M:%S')
                    log.debug(" Проверка timestamp [%5d] %s, возраст %.3f часов",
                              timestamp, sdt, age / 3600.0)
                    if age <= time_back and timestamp > latest_timestamp:
                        latest_backup = backup_path
                        latest_timestamp = timestamp
                except ValueError:
                    continue

            if not latest_backup:
                log.error("Бэкап для file_id=%d не найден в пределах %d секунд",
                          file_id, time_back)
                emsg = f"Error: No backup found for file_id={file_id} within {time_back} seconds " + \
                       f"from {checked} files."
                if oldest > 0:
                    emsg += f" Oldest file have age {current_time - oldest} seconds"
                raise ProcessorError(emsg, user_name)

            with latest_backup.open('r', encoding='utf-8') as f:
                backup_content = f.read()
            old_lines_count = len(source.splitlines())
            result = self.save_file(file_id, file_name, backup_content, project_id, user_name,
                                   timestamp=latest_timestamp)
            if result.is_ok():
                latest_backup.unlink()
                log.debug("Removed backup file: %s", str(latest_backup))
            return result
        except ProcessorError as e:
            return res_error(user_name, str(e))


class FileReplaceProcessor(BlockProcessor):
    """Обрабатывает тег <replace> для замены текста в файле по шаблону."""
    def __init__(self):
        super().__init__('replace')
        self.replace = False

    async def handle_block(self, attrs: dict, block_code: str) -> dict:
        """Обрабатывает блок <replace> для замены текста в файле.

        Args:
            attrs (dict): Атрибуты тега (например, file_id, find, to).
            block_code (str): Содержимое тега (не используется).

        Returns:
            dict: Результат обработки (успех или ошибка).
        """
        user_name = attrs.get('user_name', '@self')
        try:
            file_id = self.validate_file_id(attrs.get('file_id'), user_name)
            pattern = attrs.get('find')
            replacement = attrs.get('to', '')
            if not pattern:
                log.error("Отсутствует атрибут find в replace")
                raise ProcessorError("Error: Missing find pattern", user_name)

            log.debug("Processing replace for file_id=%d, pattern=%s, replacement=%s",
                      file_id, pattern, replacement)
            file_name, source, project_id = self.get_file_data(file_id, user_name)

            new_content = re.sub(pattern, replacement, source, flags=re.MULTILINE)
            if new_content == source:
                old_lines_count = len(source.splitlines())
                log.debug("Replace для file_id=%d не внёс изменений", file_id)
                return res_success(user_name,
                                   f"Файл @attach#{file_id} не изменён, было {old_lines_count} строк, " + \
                                   f"осталось {old_lines_count} строк")
            return self.save_file(file_id, file_name, new_content, project_id, user_name)
        except ProcessorError as e:
            return res_error(user_name, str(e))


class FileMoveProcessor(BlockProcessor):
    """Обрабатывает тег <move_file> для перемещения файла."""
    def __init__(self):
        super().__init__('move_file')
        self.replace = False

    async def handle_block(self, attrs: dict, block_code: str) -> dict:
        """Обрабатывает блок <move_file> для перемещения файла.

        Args:
            attrs (dict): Атрибуты тега (например, file_id, new_name).
            block_code (str): Содержимое тега (не используется).

        Returns:
            dict: Результат обработки (успех или ошибка).
        """
        user_name = attrs.get('user_name', '@self')
        try:
            file_id = self.validate_file_id(attrs.get('file_id'), user_name)
            new_name = attrs.get('new_name')
            if not new_name:
                log.error("Отсутствует атрибут new_name в move_file")
                raise ProcessorError("Error: Missing new_name", user_name)

            overwrite = attrs.get('overwrite', 'False').lower() == 'true'
            log.debug("Processing move_file: file_id=%d, new_name=%s, overwrite=%s",
                      file_id, new_name, overwrite)

            file_name, _, project_id = self.get_file_data(file_id, user_name)
            proj_man = g.project_manager
            if proj_man.projects_dir is None:
                raise ProcessorError("Error: No project selected for move file", user_name)
            project_name = proj_man.project_name

            if '/' not in new_name:
                new_name = f"{project_name}/{new_name}"
                log.debug("Добавлен префикс project_name к new_name: %s", new_name)

            try:
                safe_path = (proj_man.projects_dir / new_name).resolve()
                if not str(safe_path).startswith('/app/projects'):
                    log.error("Недопустимый путь файла: %s", new_name)
                    raise ProcessorError("Error: File path outside /app/projects", user_name)
            except Exception as e:
                log.excpt("Ошибка проверки пути файла %s: ", new_name, e=e)
                raise ProcessorError("Error: Invalid file path", user_name)

            file_manager = g.file_manager
            result = file_manager.move_file(file_id, new_name, project_id, overwrite)
            if result < 0:
                if result == -1:
                    raise ProcessorError(f"Error: File @attach#{file_id} not found", user_name)
                elif result == -2:
                    raise ProcessorError(f"Error: Target file {new_name} already exists", user_name)
                elif result == -3:
                    raise ProcessorError(f"Error: Failed to create backup for file @attach#{file_id}", user_name)
                else:
                    raise ProcessorError(f"Error: Failed to move file @attach#{file_id} to {new_name}", user_name)

            log.debug("File moved successfully: file_id=%d to new_name=%s", file_id, new_name)
            return res_success(user_name, f"Файл @attach#{file_id} успешно перемещён в {new_name}")
        except ProcessorError as e:
            return res_error(user_name, str(e))


class CodeInsertProcessor(BlockProcessor):
    """Обрабатывает тег <code_insert> для вставки кода в файл на указанную строку."""
    def __init__(self):
        super().__init__('code_insert')

    async def handle_block(self, attrs: dict, block_code: str) -> dict:
        """Обрабатывает блок <code_insert> для вставки кода на строку line_num.

        Args:
            attrs (dict): Атрибуты тега (file_id, line_num).
            block_code (str): Код для вставки.

        Returns:
            dict: Результат обработки (успех или ошибка).
        """
        user_name = attrs.get('user_name', '@self')
        try:
            file_id = self.validate_file_id(attrs.get('file_id'), user_name)
            line_num = int(attrs.get('line_num', 0))
            if line_num < 1:
                log.error("Недопустимый line_num: %d", line_num)
                raise ProcessorError("Error: Invalid line number", user_name)

            file_name, source, project_id = self.get_file_data(file_id, user_name)
            lines = source.splitlines(keepends=True)
            if line_num > len(lines):
                log.error("line_num=%d превышает количество строк в файле %s (%d)", line_num, file_name, len(lines))
                raise ProcessorError(f"Error: Line number {line_num} exceeds file length", user_name)

            # Проверяем, что строка line_num пустая
            target_line = lines[line_num - 1].strip()
            if target_line:
                log.error("Строка %d в файле %s не пуста: '%s'", line_num, file_name, target_line)
                dump = []
                for ln in range(line_num, line_num + 3):
                    i = ln - 1
                    if i >= len(lines):
                        break
                    dump.append(f"\t{ln}. {lines[i]}")
                raise ProcessorError(f"Error: Line {line_num} is not empty, check the code:\n" + ''.join(dump), user_name)

            # Вставляем block_code перед строкой line_num
            block_lines = block_code.splitlines(keepends=True)
            if not block_lines[-1].endswith('\n'):
                block_lines.append('\n')  # Добавляем перенос строки, если его нет
            lines = lines[:line_num - 1] + block_lines + lines[line_num - 1:]  # оригинальная пустая строка остается после вставки
            new_content = ''.join(lines)

            # Сохраняем обновлённый файл
            result = self.save_file(file_id, file_name, new_content, project_id, user_name)
            if result.is_ok():
                log.debug("Код успешно вставлен в файл %s на строку %d", file_name, line_num)
                return res_success(user_name, f"Код успешно вставлен в файл @attach#{file_id} на строку {line_num}")
            return result
        except ProcessorError as e:
            return res_error(user_name, str(e))


def get_exported() -> list:
    return [FileEditProcessor(), FileReplaceProcessor(), FileMoveProcessor(), FileUndoProcessor(), CodeInsertProcessor()]

#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
#
