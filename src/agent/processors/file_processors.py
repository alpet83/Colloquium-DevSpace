# /app/agent/processors/file_processors.py, updated 2025-07-23 18:22 EEST
import re
import time
import globals
from datetime import datetime
from collections import Counter
from pathlib import Path
from processors.block_processor import BlockProcessor, res_error, res_success, ProcessorError

log = globals.get_logger("llm_proc")

class PatchMismatch:
    def __init__(self, line_num, patch_line, file_line, effect=0):
        self.line_num = line_num
        self.patch_line = patch_line
        self.file_line = file_line
        self.effect = effect

    def format_row(self):
        if self.line_num == 0:
            return f'<tr><td>-</td><td colspan="3">{self.patch_line}</td></tr>'
        effects = ['-', 'N', '+']
        eff = effects[self.effect + 1]
        return f'<tr><td>{self.line_num}</td><td>{eff}</td><td>{self.patch_line}</td><td>{self.file_line}</td></tr>'

class HunkBlock:
    def __init__(self, patch_line: str):
        self.start_old = 0
        self.start_new = 0
        self.count_old = 0
        self.count_new = 0
        self.mismatches = []

        match = re.match(r'@@ -(\d+),(\d+) \+(\d+),(\d+) @@', patch_line)
        if not match:
            return
        self.start_old = int(match.group(1))
        self.count_old = int(match.group(2))
        self.start_new = int(match.group(3))
        self.count_new = int(match.group(4))
        self.patch = []

    def parse(self, patch_lines, patch_idx):
        patch_line = patch_lines[patch_idx]

        log.debug("Parsing hunk with %d old lines, %d new lines", self.count_old, self.count_new)
        old_line_num = self.start_old
        new_line_num = self.start_new
        old_lines = 0
        new_lines = 0
        seen_plus_plus = False
        while patch_idx < len(patch_lines) and (old_lines < self.count_old or new_lines < self.count_new):
            line = patch_lines[patch_idx]
            log.debug("Processing line at index=%d: %s", patch_idx, line.rstrip())
            if line.startswith('@@'):
                break
            patch_idx += 1
            if line.startswith('---'):
                continue
            if line.startswith('+++'):
                log.debug("Detected +++ metadata")
                seen_plus_plus = True
                continue
            if line.startswith('-'):
                if seen_plus_plus:
                    self.patch.append((0, 0, f"Invalid hunk: removal line after +++"))
                    continue
                log.debug("Indexing removal line at %d", old_line_num)
                self.patch.append((old_line_num, -1, line[1:]))
                old_lines += 1
            elif line.startswith('+'):
                self.patch.append((new_line_num, 1, line[1:]))
                new_line_num += 1
                new_lines += 1
            else:
                self.patch.append((old_line_num, 0, line))
                old_line_num += 1
                new_line_num += 1
                old_lines += 1
                new_lines += 1

        if old_lines != self.count_old:
            log.error("Invalid hunk: expected %d old lines, got %d", self.count_old, old_lines)
            self.patch.append((0, 0, f"Invalid hunk: expected {self.count_old} old lines, got {old_lines}"))
        if new_lines != self.count_new:
            log.error("Invalid hunk: expected %d new lines, got %d", self.count_new, new_lines)
            self.patch.append((0, 0, f"Invalid hunk: expected {self.count_new} new lines, got {new_lines}"))
        log.debug("Parsed hunk contents:\n%s", self.dump())
        return patch_idx

    def dump(self):
        result = []
        for line_num, effect, line in self.patch:
            effects = ['-', 'N', '+']
            eff = effects[effect + 1]
            result.append(f"{line_num:5}: {eff} '{line.rstrip()}'")
        return "\n".join(result)

    def apply(self, file_lines, offset, line_ending):
        new_lines = file_lines.copy()
        self.mismatches = []
        removed = 0
        added = 0
        for line_num, effect, line in self.patch:
            l_num = line_num + offset
            if effect == 0:  # Neutral
                if l_num <= 0 or l_num > len(file_lines):
                    self._add_pm(l_num, line.rstrip(), '[EOF]', effect)
                else:
                    real_text = file_lines[l_num]
                    if real_text is None or line != real_text:
                        self._add_pm(l_num, line.rstrip(), real_text.rstrip() if real_text else '[None]', effect)
                    else:
                        log.debug("Validated neutral context line at line=%d: '%s' ", l_num, line.rstrip())
            elif effect == -1:  # Removal
                if l_num <= 0 or l_num > len(file_lines):
                    self._add_pm(l_num, line.rstrip(), '[EOF]', effect)
                else:
                    real_text = file_lines[l_num]
                    if real_text is None or line != real_text:
                        self._add_pm(l_num, line.rstrip(), real_text.rstrip() if real_text else '[None]', effect)
                    else:
                        new_lines[l_num] = None
                        log.debug("Removed line at line=%d: '%s' ", l_num, line.rstrip())
                        removed += 1
            elif effect == 1:  # Addition
                log.debug("Inserted line at line=%d with content '%s'", l_num, line.rstrip())
                new_lines.insert(l_num, line.rstrip() + line_ending)
                added += 1

        neutral_count = sum(1 for _, eff, _ in self.patch if eff == 0)
        if removed != self.count_old - neutral_count:
            self._add_pm(0, f"Expected {self.count_old - neutral_count} removals, got {removed}", '', 0)
        if added != self.count_new - neutral_count:
            self._add_pm(0, f"Expected {self.count_new - neutral_count} additions, got {added}", '', 0)
        return new_lines

    def _add_pm(self, l_num: int, patch_line: str, file_line: str, effect = 0):
        pm = PatchMismatch(l_num, patch_line, file_line, effect)
        self.mismatches.append(pm)


class FileEditProcessor(BlockProcessor):
    def __init__(self):
        super().__init__('code_file')

    def handle_block(self, attrs, block_code):
        file_name = attrs.get('name')
        user_name = attrs.get('user_name', '@self')
        if not file_name:
            log.error("Отсутствует атрибут name в code_file")
            return res_error(user_name, "Error: Missing file name")

        log.debug("Обработка code_file: file_name=%s, content_length=%d", file_name, len(block_code))
        proj_man = globals.project_manager
        project_id = proj_man.project_id if proj_man and hasattr(proj_man, 'project_id') else None
        if project_id is None:
            log.error("Нет активного проекта для обработки code_file")
            return res_error(user_name, "Error: No active project selected")

        project_name = proj_man.project_name
        if '/' not in file_name:
            file_name = f"{project_name}/{file_name}"
            log.debug("Добавлен префикс project_name к file_name: %s", file_name)

        file_manager = globals.file_manager
        file_id = file_manager.find(file_name, project_id)
        action = "сохранён" if file_id else "создан"
        if file_id:
            return self.save_file(file_id, file_name, block_code, project_id, user_name)
        else:
            file_id = file_manager.add_file(
                content=block_code,
                file_name=file_name,
                timestamp=int(time.time()),
                project_id=project_id
            )
            return res_success(user_name, f"Файл @attach#{file_id} успешно {action}", "@attach#%d" % file_id)


class FilePatchProcessor(BlockProcessor):
    def __init__(self):
        super().__init__('code_patch')
        self.replace = False
        self.current_lines = None
        self.patch_lines = None
        self.line_ending = None

    def detect_offset(self, hunk: HunkBlock, file_id: int) -> dict:
        results = {}
        for offset in range(-4, 4):
            log.debug("Trying hunk with offset=%d at start_old=%d", offset, hunk.start_old)
            block_lines = hunk.apply(self.current_lines, offset, self.line_ending)
            agent_message = None
            if offset != 0:
                agent_message = f"Внимание: Ханк предполагал изменения с {hunk.start_old} строки, " + \
                                f"фактический код обнаружен на строке {hunk.start_old + offset}\n"
            results[offset] = {"new_lines": block_lines,
                               "mismatches": hunk.mismatches.copy(),
                               "agent_message": agent_message}
            if not hunk.mismatches:
                log.debug("Hunk successful with offset=%d for file_id=%d", offset, file_id)
                return results[offset]

        log.warn("No suitable offset detected")
        return results[0]

    def handle_block(self, attrs, block_code):
        user_name = attrs.get('user_name', 'Unknown')
        try:
            file_id = self.validate_file_id(attrs.get('file_id'), user_name)
            block_code = globals.unitext(block_code)
            if not isinstance(block_code, str):
                log.error("Неверный тип patch_content для file_id=%d: %s", file_id, type(block_code))
                raise ProcessorError("Error: Invalid patch content type", user_name)

            log.debug("Обработка code_patch: file_id=%d, patch_content=~C95%s~C00, type=%s",
                      file_id, block_code[:50], type(block_code))

            file_name, source, project_id = self.get_file_data(file_id, user_name)
            self.current_lines = [None] + source.splitlines(keepends=True)
            log.debug("Added None to file_lines for 1-based indexing")
            self.patch_lines = block_code.splitlines(keepends=True)
            old_lines_count = len(self.current_lines) - 1
            # Определяем наиболее частое окончание строки
            line_endings = [line[-2:] if line and line.endswith('\r\n') else line[-1:] if line and line.endswith('\n') else '' for line in self.current_lines[1:]]
            self.line_ending = '\n'
            if line_endings:
                most_common = Counter(line_endings).most_common(1)
                self.line_ending = most_common[0][0] if most_common[0][0] in ['\n', '\r\n'] else '\n'
            log.debug("Initialized patch context: lines=%d, line_ending=%s", old_lines_count, repr(self.line_ending))

            if not any(line.startswith('@@') for line in self.patch_lines):
                log.error("Невалидный формат патча для file_id=%d", file_id)
                raise ProcessorError("PatchError: Invalid patch format, no single @@ was found", user_name)

            # Обработка патча
            new_lines = self.current_lines.copy()
            mismatches = []
            agent_messages = []
            patch_idx = 0
            while patch_idx < len(self.patch_lines):
                patch_line = self.patch_lines[patch_idx]
                patch_idx += 1  # prevent dead-loop

                if patch_line.startswith('@@'):
                    hunk = HunkBlock(patch_line)
                    if hunk.start_old <= 0:
                        _pm = PatchMismatch(patch_idx, f"Invalid hunk header: {patch_line.rstrip()}", '', 0)
                        mismatches.append(_pm)
                        continue
                    patch_idx = hunk.parse(self.patch_lines, patch_idx)
                    # Пробуем применить патч с автодетектом смещения
                    result = self.detect_offset(hunk, file_id)
                    block_lines = result['new_lines']
                    block_mismatches = result['mismatches']
                    agent_message = result['agent_message']
                    if block_mismatches:
                        mismatches.extend(block_mismatches)
                    else:
                        new_lines = block_lines
                        if agent_message:
                            agent_messages.append(f"@{user_name} {agent_message}")
                    patch_idx += len(hunk.patch)

            if mismatches:
                table_rows = [mismatch.format_row() for mismatch in mismatches]
                table = '<table border=1 style="border-collapse: collapse; border-color: red;">'
                table += '<tr><th>Line</th><th>Effect</th><th>Patch</th><th>File</th></tr>' + \
                         '\n'.join(table_rows) + '</table>'
                log.debug("Formatted mismatch error as HTML table")
                log.error("Патч не соответствует содержимому файла file_id=%d", file_id)
                raise ProcessorError(
                    # <mismatch> блокирует вывод таблицы в чат, здесь его не нужно!
                    f"PatchError: Removed or neutral lines do not match @attach#{file_id} {file_name} content.\n{table}",
                    user_name
                )

            new_lines = [line for line in new_lines if line is not None]
            log.debug("Removed None entries from new_lines")
            if new_lines == source.splitlines(keepends=True):
                log.debug("Патч для file_id=%d не вносит изменений", file_id)
                return res_success(user_name, f"Файл @attach#{file_id} не изменён, было {old_lines_count} строк, осталось {old_lines_count} строк", agent_messages=agent_messages)

            result = self.save_file(file_id, file_name, ''.join(new_lines),  project_id, user_name)
            result.agent_messages = agent_messages
            return result
        except ProcessorError as e:
            return res_error(user_name, str(e))

class FileUndoProcessor(BlockProcessor):
    def __init__(self):
        super().__init__('undo')
        self.replace = False

    def handle_block(self, attrs, block_code):
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
            proj_man = globals.project_manager
            if proj_man.projects_dir is None:
                raise ProcessorError("Error: No project selected for undo file", user_name)
            proj_dir = str(proj_man.projects_dir)
            file_path = proj_man.locate_file(file_name, project_id)
            # поиск файла по маске, чтобы выбрать из файлов с одним именем, но разной датой
            backed_file = str(file_path).replace(proj_dir, proj_dir + '/backups')   # prefix
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
                    log.debug(" Проверка timestamp [%5d] %s, возраст %.3f часов", timestamp, sdt, age / 3600.0)
                    if age <= time_back and timestamp > latest_timestamp:
                        latest_backup = backup_path
                        latest_timestamp = timestamp
                except ValueError:
                    continue

            if not latest_backup:
                log.error("Бэкап для file_id=%d не найден в пределах %d секунд", file_id, time_back)
                emsg = f"Error: No backup found for file_id={file_id} within {time_back} seconds from {checked} files."
                if oldest > 0:
                    emsg += f"Oldest file have age {current_time - oldest} seconds"
                raise ProcessorError(emsg, user_name)

            with latest_backup.open('r', encoding='utf-8') as f:
                backup_content = f.read()
            old_lines_count = len(source.splitlines())
            result = self.save_file(file_id, file_name, backup_content, project_id, user_name, timestamp=latest_timestamp)
            if result.is_ok():
                latest_backup.unlink()
                log.debug("Removed backup file: %s", str(latest_backup))
            return result
        except ProcessorError as e:
            return res_error(user_name, str(e))
class FileReplaceProcessor(BlockProcessor):
    def __init__(self):
        super().__init__('replace')
        self.replace = False

    def handle_block(self, attrs, block_code):
        user_name = attrs.get('user_name', 'Unknown')
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
                return res_success(user_name, f"Файл @attach#{file_id} не изменён, было {old_lines_count} строк, осталось {old_lines_count} строк")
            return self.save_file(file_id, new_content, file_name, project_id, user_name)
        except ProcessorError as e:
            return res_error(user_name, str(e))