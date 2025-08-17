# /app/agent/processors/patch_processor.py, created 2025-07-27 15:00 EEST
import re
from collections import Counter
from processors.block_processor import BlockProcessor, res_error, res_success, ProcessorError
import globals

log = globals.get_logger("llm_proc")

class PatchMismatch:
    """Представляет несоответствие между строкой патча и файлом."""
    def __init__(self, line_num: int, patch_line: str, file_line: str, effect: int = 0):
        """Инициализирует объект несоответствия.

        Args:
            line_num (int): Номер строки.
            patch_line (str): Строка из патча.
            file_line (str): Строка из файла.
            effect (int, optional): Эффект изменения (-1 удаление, 0 нейтрально, 1 добавление). Defaults to 0.
        """
        self.line_num = line_num
        self.patch_line = patch_line
        self.file_line = file_line
        self.effect = effect

    def __str__(self) -> str:
        """Возвращает строковое представление несоответствия."""
        return f" {self.line_num:3}: patch '{self.patch_line}' file '{self.file_line}'"

    def format_row(self) -> str:
        """Форматирует несоответствие в HTML-строку для таблицы."""
        if self.line_num == 0:
            return f'<tr><td>-</td><td colspan="3">{self.patch_line}</td></tr>'
        effects = ['-', 'N', '+']
        eff = effects[self.effect + 1]
        return f'<tr><td>{self.line_num}</td><td>{eff}</td><td>{self.patch_line}</td><td>{self.file_line}</td></tr>'

class HunkBlock:
    """Представляет блок ханка в патче для обработки изменений файла."""
    def __init__(self, patch_line: str):
        """Инициализирует объект ханка.

        Args:
            patch_line (str): Строка заголовка ханка (например, '@@ -1,3 +1,4 @@').
        """
        self.start_old = 0
        self.count_old = 0
        self.start_new = 0
        self.count_new = 0
        self.offset = 0
        self.sp_warns = {}
        self.mismatches = []
        self.patch = []

        match = re.match(r'@@ -(\d+),(\d+) \+(\d+),(\d+) @@', patch_line)
        if match:
            self.start_old = int(match.group(1))
            self.count_old = int(match.group(2))
            self.start_new = int(match.group(3))
            self.count_new = int(match.group(4))

    def parse(self, patch_lines: list, patch_idx: int) -> int:
        """Парсит строки ханка из патча.

        Args:
            patch_lines (list): Список строк патча.
            patch_idx (int): Текущий индекс в списке строк.

        Returns:
            int: Новый индекс после обработки ханка.
        """
        log.debug("Parsing hunk with %d old lines, %d new lines", self.count_old, self.count_new)
        old_line_num = self.start_old
        new_line_num = self.start_new
        old_lines = 0
        new_lines = 0
        seen_plus_plus = False
        line = ''

        while patch_idx < len(patch_lines):
            line = patch_lines[patch_idx]
            patch_idx += 1

            log.debug("Processing line at index=%d: %s", patch_idx - 1, line.rstrip())
            if line.startswith('@@'):
                break
            if line.startswith('---'):
                continue
            if line.startswith('+++'):
                log.debug("Detected +++ metadata")
                seen_plus_plus = True
                continue

            ins_line_num = self.start_new + new_lines
            diff = new_lines - old_lines

            if line.startswith('-'):
                if seen_plus_plus:
                    log.error(f"Invalid hunk: removal line %d after +++", old_line_num)
                    continue
                log.debug("Indexing removal line at %d", old_line_num)
                self.patch.append((old_line_num, -1, line[1:]))
                old_lines += 1
            elif line.startswith('+'):
                log.debug("Indexing addition line at %d, diff = %d", ins_line_num, diff)
                self.patch.append((ins_line_num, 1, line[1:]))
                old_line_num += 1
                new_lines += 1
            else:
                log.debug("Indexing bypass line %d => %d, diff = %d", old_line_num, ins_line_num, diff)
                self.patch.append((ins_line_num, 0, line))
                old_line_num += 1
                old_lines += 1
                new_lines += 1

        if old_lines != self.count_old:
            log.error("Invalid hunk: expected %d old lines, got %d", self.count_old, old_lines)
            self.patch.append((0, 0, f"Invalid hunk: expected {self.count_old} old lines, got {old_lines}"))
        if new_lines != self.count_new:
            log.error("Invalid hunk: expected %d new lines, got %d", self.count_new, new_lines)
            self.patch.append((0, 0, f"Invalid hunk: expected {self.count_new} new lines, got {new_lines}"))
        log.debug("Parsed hunk contents:\n%s\n last checked line: '%s'", self.dump(), line)
        return patch_idx

    def dump(self) -> str:
        """Возвращает строковое представление содержимого ханка."""
        result = []
        for line_num, effect, line in self.patch:
            effects = ['-', 'N', '+']
            eff = effects[effect + 1]
            result.append(f"{line_num:5}: {eff} '{line.rstrip()}'")
        return "\n".join(result)

    def check(self, new_lines: list, l_num: int, line: str, effect: int) -> bool:
        """Проверяет соответствие строки патча содержимому файла.

        Args:
            new_lines (list): Список строк файла.
            l_num (int): Номер строки.
            line (str): Строка патча.
            effect (int): Эффект изменения (-1 удаление, 0 нейтрально, 1 добавление).

        Returns:
            bool: True, если строка соответствует, False иначе.
        """
        unspaced = line[1:] if line.startswith(' ') else line
        if l_num <= 0 or l_num >= len(new_lines):
            self._add_pm(l_num, line, '[EOF]', effect)
        else:
            real_text = new_lines[l_num].rstrip() if new_lines[l_num] else '[None]'
            if real_text != line:
                if unspaced == real_text:
                    self.sp_warns[l_num] = 1
                    line = unspaced
                else:
                    if 0 == self.offset:
                        log.warn("\tВарианты '%s' и '%s' не соответствуют реальному тексту '%s'",
                                 line, unspaced, real_text)
                    self._add_pm(l_num, line, real_text, effect)
                    return False
        return True

    def apply(self, file_lines: list, offset: int, line_ending: str) -> tuple:
        """Применяет ханк к строкам файла с учётом смещения.

        Args:
            file_lines (list): Список строк файла.
            offset (int): Смещение строк.
            line_ending (str): Окончание строки.

        Returns:
            tuple: Новые строки файла и сообщение агента (если есть).
        """
        new_lines = file_lines.copy()
        self.offset = offset
        self.mismatches = []
        self.sp_warns = {}
        removed = 0
        added = 0
        agent_message = None
        if offset != 0:
            agent_message = f"Внимание: Ханк предполагал изменения с {self.start_old} строки, " + \
                            f"фактический код обнаружен на строке {self.start_old + offset}\n"
        for line_num, effect, line in self.patch:
            l_num = line_num + offset
            line = line.rstrip()
            if effect == 0:  # Neutral
                if self.check(new_lines, l_num, line, effect):
                    log.debug("Validated neutral context line at line=%d: '%s' ", l_num, line)
            elif effect == -1:  # Removal
                if self.check(new_lines, l_num, line, effect):
                    text = new_lines.pop(l_num)
                    if text is None:
                        text = '[None]'
                    log.debug("Removed line at line=%d: '%s' ? '%s' ", l_num, line, text.rstrip())
                    removed += 1
            elif effect == 1:  # Addition
                if self.sp_warns.get(l_num, None):  # обработка замены линии с лишним пробелом
                    line = line[1:]
                log.debug("Inserted line at line=%d with content '%s'", l_num, line)
                new_lines.insert(l_num, line + line_ending)
                added += 1

        if self.mismatches and offset == 0:
            dump = "\n".join(map(str, self.mismatches))
            log.warn("For offset 0 have mismatches:\n%s", dump)
        neutral_count = sum(1 for _, eff, _ in self.patch if eff == 0)
        if removed != self.count_old - neutral_count:
            self._add_pm(0, f"Expected {self.count_old - neutral_count} removals, got {removed}", '', 0)
        if added != self.count_new - neutral_count:
            self._add_pm(0, f"Expected {self.count_new - neutral_count} additions, got {added}", '', 0)
        return new_lines, agent_message

    def _add_pm(self, l_num: int, patch_line: str, file_line: str, effect: int = 0):
        """Добавляет несоответствие в список.

        Args:
            l_num (int): Номер строки.
            patch_line (str): Строка патча.
            file_line (str): Строка файла.
            effect (int, optional): Эффект изменения. Defaults to 0.
        """
        pm = PatchMismatch(l_num, patch_line, file_line, effect)
        self.mismatches.append(pm)


class CodePatchProcessor(BlockProcessor):
    """Обрабатывает тег <code_patch> для применения патчей к файлам."""
    def __init__(self):
        super().__init__('code_patch')
        self.replace = False
        self.current_lines = None
        self.patch_lines = None
        self.line_ending = None

    def detect_offset(self, hunk: HunkBlock, file_id: int) -> dict:
        """Определяет подходящее смещение для ханка.

        Args:
            hunk (HunkBlock): Объект ханка.
            file_id (int): ID файла.

        Returns:
            dict: Результат применения ханка (новые строки, несоответствия, сообщение агента).
        """
        results = {}
        for offset in range(-4, 4):
            log.debug("Trying hunk with offset=%d at start_old=%d", offset, hunk.start_old)
            block_lines, agent_message = hunk.apply(self.current_lines, offset, self.line_ending)
            results[offset] = {"new_lines": block_lines, "mismatches": hunk.mismatches.copy(),
                               "agent_message": agent_message}
            if not hunk.mismatches:
                log.debug("Hunk successful with offset=%d for file_id=%d", offset, file_id)
                return results[offset]
        log.warn("No suitable offset detected")
        return results[0]

    async def handle_block(self, attrs: dict, block_code: str) -> dict:
        """Обрабатывает блок <code_patch> для применения патча к файлу.

        Args:
            attrs (dict): Атрибуты тега (например, file_id).
            block_code (str): Содержимое патча.

        Returns:
            dict: Результат обработки (успех или ошибка).
        """
        user_name = attrs.get('user_name', '@self')
        agent_messages = []
        try:
            file_id = self.validate_file_id(attrs.get('file_id'), user_name)
            block_code = globals.unitext(block_code)
            if not isinstance(block_code, str):
                log.error("Неверный тип patch_content для file_id=%d: %s",
                          file_id, type(block_code))
                raise ProcessorError("Error: Invalid patch content type", user_name)

            log.debug("Обработка code_patch: file_id=%d, patch_content=~C95%s~C00, type=%s",
                      file_id, block_code[:50], type(block_code))
            file_name, source, project_id = self.get_file_data(file_id, user_name)
            self.current_lines = [None] + source.splitlines(keepends=True)  # 1-based indexing
            self.patch_lines = block_code.splitlines(keepends=True)
            old_lines_count = len(self.current_lines) - 1
            line_endings = [line[-2:] if line and line.endswith('\r\n') else
                            line[-1:] if line and line.endswith('\n') else
                            '' for line in self.current_lines[1:]]
            self.line_ending = '\n'
            if line_endings:
                most_common = Counter(line_endings).most_common(1)
                self.line_ending = most_common[0][0] if most_common[0][0] in ['\n', '\r\n'] else '\n'
            log.debug("Initialized patch context: lines=%d, line_ending=%s",
                      old_lines_count, repr(self.line_ending))

            if not any(line.startswith('@@') for line in self.patch_lines):
                log.error("Невалидный формат патча для file_id=%d", file_id)
                raise ProcessorError("PatchError: Invalid patch format, no single @@ was found",
                                     user_name)

            new_lines = self.current_lines.copy()
            mismatches = []
            agent_messages = []
            patch_idx = 0
            while patch_idx < len(self.patch_lines):
                patch_line = self.patch_lines[patch_idx]
                if patch_line.startswith('@@'):
                    hunk = HunkBlock(patch_line)
                    if hunk.start_old <= 0:
                        mismatches.append(PatchMismatch(patch_idx + 1, f"Invalid hunk header: {patch_line.rstrip()}", '', 0))
                        patch_idx += 1
                        continue
                    patch_at = patch_idx
                    patch_idx = hunk.parse(self.patch_lines, patch_idx + 1)
                    result = self.detect_offset(hunk, file_id)
                    block_lines = result['new_lines']
                    block_mismatches = result['mismatches']
                    agent_message = result['agent_message']
                    if block_mismatches:
                        log.warn("Пропущен патч для строк с %d ", patch_at)
                        mismatches.extend(block_mismatches)
                    else:
                        new_lines = block_lines
                        reply = ""
                        if agent_message:
                            reply = f"@{user_name} {agent_message}"
                        if hunk.sp_warns:
                            reply += f"\nПатч успешно применен, повторять не требуется. Обнаружены избыточные пробелы, при форматировании ханка, будьте внимательней в следующий раз ;)"
                        if reply:
                            agent_messages.append(reply)

                    patch_idx += len(hunk.patch)
                else:
                    patch_idx += 1

            if mismatches:
                table_rows = [mismatch.format_row() for mismatch in mismatches]
                table = '<table class=code-lines border=1 style="border-collapse: collapse; border-color: red">' + \
                        '<tr><th>Line</th><th>Effect</th><th>Patch</th><th>File</th></tr>' + \
                        '\n'.join(table_rows) + '</table>'
                log.debug("Formatted mismatch error as HTML table")
                log.error("Патч не соответствует содержимому файла file_id=%d", file_id)
                raise ProcessorError(
                    f"PatchError: <mismatch>Удаленные или пропускаемые линии патча не совпадают " +
                    f"в файле {file_id} - {file_name}</mismatch>\n{table}",
                    user_name
                )
            if len(new_lines) > 0 and new_lines[0] is None:
                del new_lines[0]  # удалить смещение индекса

            if new_lines == source.splitlines(keepends=True):
                log.debug("Патч для file_id=%d не вносит изменений", file_id)
                return res_success(user_name,
                                   f"Файл @attach#{file_id} не изменён, было {old_lines_count} строк, " + \
                                   f"осталось {old_lines_count} строк",
                                   agent_messages=agent_messages)

            result = self.save_file(file_id, file_name, ''.join(new_lines), project_id, user_name)
            result.agent_messages = agent_messages
            return result
        except ProcessorError as e:
            return res_error(user_name, str(e), agent_messages=agent_messages)