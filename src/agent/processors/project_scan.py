# /app/agent/processors/project_scan.py, created 2025-08-13
# Formatted with proper line breaks and indentation for project compliance.

import re
import globals as g
from managers.project import ProjectManager
from processors.block_processor import BlockProcessor, res_error, res_success, ProcessorError

log = g.get_logger("project_scan")


class ProjectScanProcessor(BlockProcessor):
    """Процессор для поиска строк в файлах проекта."""
    def __init__(self):
        super().__init__('project_scan')

    async def handle_block(self, attrs: dict, block_code: str):
        """Обрабатывает блок <project_scan> для поиска строки в файлах attached_files.

        Args:
            attrs (dict): Атрибуты тега (не используются).
            block_code (str): Строка для поиска.

        Returns:
            dict: Результат обработки (успех с результатами поиска или ошибка).
        """
        user_name = attrs.get('user_name', '@self')
        try:
            query = block_code.strip()
            if not query:
                log.error("Нет строки для поиска")
                raise ProcessorError("Error: No search query provided", user_name)

            proj_id = attrs.get('project_id', None)
            proj_man = ProjectManager.get(proj_id) if proj_id else g.project_manager
            if proj_man is None or g.file_manager is None:
                raise ProcessorError("Error: No active project selected", user_name)
            # Получаем все файлы из attached_files
            fm = g.file_manager
            files = getattr(fm, 'list_files', lambda **_: [])(project_id=proj_man.project_id) or []  # поиск в активном проекте
            results = []
            for file in files:
                file_id = file['id']
                file_name = file['file_name']
                try:
                    file_data = g.file_manager.get_file(file_id)
                except (UnicodeDecodeError, Exception) as e:
                    log.debug("Skipping file %s (file_id=%d): %s", file_name, file_id, e)
                    continue
                if not file_data:
                    continue
                raw = file_data['content']
                if raw is None:
                    continue
                if isinstance(raw, bytes):
                    try:
                        content = raw.decode('utf-8')
                    except UnicodeDecodeError:
                        log.debug("Skipping binary file %s (file_id=%d)", file_name, file_id)
                        continue
                else:
                    content = raw
                lines = content.splitlines()
                matched_lines = []
                for i, line in enumerate(lines, 1):
                    if re.search(re.escape(query), line, re.IGNORECASE):
                        matched_lines.append(i)
                if matched_lines:
                    log.debug("Вхождение найдено в %35s на строках %s", file_name, str(matched_lines))
                    results.append(f"@attach#{file_id}: matched lines {matched_lines}")

            if results:
                result_text = '\n'.join(results)
                return res_success(user_name, f"@{user_name} have matches:\n {result_text}")
            else:
                return res_success(user_name, f"@{user_name} No matches found for query: " + query)
        except ProcessorError as e:
            return res_error(user_name, str(e))