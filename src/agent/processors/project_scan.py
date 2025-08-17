# /app/agent/processors/project_scan.py, created 2025-08-13
# Formatted with proper line breaks and indentation for project compliance.

import re
import globals as g
from processors.block_processor import BlockProcessor, res_error, res_success, ProcessorError

log = g.get_logger("project_scan")


class ProjectScanProcessor(BlockProcessor):
    """Процессор для поиска строк в файлах проекта."""
    def __init__(self):
        super().__init__('project_scan')

    async def handle_block(self, attrs: dict, block_code: str) -> dict:
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

            proj_man = g.project_manager
            # Получаем все файлы из attached_files
            files = g.file_manager.list_files(user_name, project_id=proj_man.project_id)  # поиск в активном проекте
            results = []
            for file in files:
                file_id = file['id']
                file_name = file['file_name']
                content = g.file_manager.get_file(file_id)['content']
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