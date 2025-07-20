# /app/agent/llm_hands.py, updated 2025-07-20 17:26 EEST
import re
import requests
import time
import hashlib
from datetime import datetime
from llm_api import LLMConnection
import globals
from lib.basic_logger import BasicLogger
from lib.execute_commands import execute
from pathlib import Path

MCP_URL = "http://mcp-sandbox:8084"
log = globals.get_logger("llm_hands")

def res_success(user, msg, pmsg=None):
    return {"status": "success", "message": msg, "processed_message": pmsg, "user_name": user}

def res_error(user, msg, pmsg=None):
    return {"status": "error", "message": msg, "processed_message": pmsg, "user_name": user}

class BlockProcessor:
    def __init__(self, tag):
        self.tag = tag
        self.replace = True

    def process(self, post_message):
        pattern = fr'<{self.tag}(?:\s+([^>]+))?>\s*([\s\S]*?)\s*</{self.tag}>'
        matches = list(re.finditer(pattern, post_message, flags=re.DOTALL))
        log.debug("Найдено %d совпадений для тега %s", len(matches), self.tag)
        processed_message = post_message
        agent_messages = []
        handled_cmds = 0
        failed_cmds = 0
        for match in matches:
            attrs = self._parse_attrs(match.group(1) or '')
            block_code = match.group(2)
            result = self.handle_block(attrs, block_code)
            if result["status"] == "error":
                failed_cmds += 1
                agent_messages.append(f"@{result.get('user_name', '@self')} {result['message']}")
            else:
                handled_cmds += 1
                agent_messages.append(result["message"])
                msg = result.get('processed_message', '')
                if msg and self.replace:
                    processed_message = processed_message.replace(match.group(0), msg)
        log.debug("Обработано %d команд, неуспешно %d для тега %s", handled_cmds, failed_cmds, self.tag)
        return {
            "processed_message": processed_message,
            "agent_messages": agent_messages,
            "handled_cmds": handled_cmds,
            "failed_cmds": failed_cmds,
            "has_code_file": self.tag == 'code_file'
        }

    def _parse_attrs(self, attrs_str):
        attrs = {}
        for attr in re.findall(r'(\w+)="([^"]*)"', attrs_str):
            attrs[attr[0]] = attr[1]
        return attrs

    def handle_block(self, attrs, block_code):
        raise NotImplementedError("Subclasses must implement handle_block")

class CommandProcessor(BlockProcessor):
    def __init__(self):
        super().__init__('cmd')
        self.replace = False

    def handle_block(self, attrs, block_code):
        command = block_code.strip().split()[0].lower() if block_code.strip().split() else None
        user_name = attrs.get('user_name', 'Unknown')
        log.debug("Обработка команды: %s", command or "None")
        if not command:
            log.error("Пустая команда")
            return res_error(user_name, "Error: Empty command")
        if command == 'ping':
            return res_success(user_name, f"@{user_name} pong")
        elif command == 'run_test':
            params = {'project_name': 'default', 'test_name': 'test'}
            resp = requests.get(f"{MCP_URL}/run_test", params=params,
                                headers={'Authorization': 'Bearer Grok-xAI-Agent-The-Best'})
            response = resp.text if resp.status_code == 200 else f"Ошибка: {resp.status_code}"
            return res_success(user_name, f"@{user_name} {response}") if resp.status_code == 200 else res_error(user_name, f"@{user_name} {response}")
        elif command == 'commit':
            params = {'project_name': 'default', 'msg': 'commit msg'}
            resp = requests.post(f"{MCP_URL}/commit", json=params,
                                 headers={'Authorization': 'Bearer Grok-xAI-Agent-The-Best'})
            response = resp.text if resp.status_code == 200 else f"Ошибка: {resp.status_code}"
            return res_success(user_name, f"@{user_name} {response}") if resp.status_code == 200 else res_error(user_name, f"@{user_name} {response}")
        log.error("Неподдерживаемая команда: %s", command)
        return res_error(user_name, f"AgentError: Unsupported command '{command}'")

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
        # Добавляем префикс project_name, если file_name не содержит '/'
        project_name = proj_man.project_name

        if '/' not in file_name:
            file_name = f"{project_name}/{file_name}"
            log.debug("Добавлен префикс project_name к file_name: %s", file_name)

        try:
            safe_path = (proj_man.projects_dir / file_name).resolve()
            if not str(safe_path).startswith('/app/projects'):
                log.error("Недопустимый путь файла: %s", file_name)
                return res_error(user_name, "Error: File path outside /app/projects")
        except Exception as e:
            log.excpt("Ошибка проверки пути файла %s: %s", file_name, str(e),
                      exc_info=(type(e), e, e.__traceback__))
            return res_error(user_name, "Error: Invalid file path")

        file_manager = globals.file_manager
        file_id = file_manager.find(file_name, project_id)
        action = "сохранён" if file_id else "создан"
        if file_id:
            try:
                file_manager.update_file(
                    file_id=file_id,
                    content=block_code,
                    file_name=file_name,
                    timestamp=int(time.time()),
                    project_id=project_id
                )
            except Exception as e:
                log.excpt("Ошибка обновления файла file_id=%d: %s", file_id, str(e),
                          exc_info=(type(e), e, e.__traceback__))
                return res_error(user_name, f"Error: Failed to update file {file_name}")
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

    def handle_block(self, attrs, block_code):
        file_id = attrs.get('file_id')
        user_name = attrs.get('user_name', 'Unknown')
        if not file_id:
            log.error("Отсутствует атрибут file_id в code_patch")
            return res_error(user_name, "Error: Missing file_id")

        try:
            file_id = int(file_id)
        except ValueError:
            log.error("Неверный формат file_id: %s", file_id)
            return res_error(user_name, "Error: Invalid file_id format")

        if isinstance(block_code, bytes):
            block_code = block_code.decode('utf-8', errors='replace')
            log.warn("patch_content был байтовым, декодирован: %s", block_code[:50])
        elif not isinstance(block_code, str):
            log.error("Неверный тип patch_content для file_id=%d: %s", file_id, type(block_code))
            return res_error(user_name, "Error: Invalid patch content type")

        log.debug("Обработка code_patch: file_id=%d, patch_content=~C95%s~C00, type=%s",
                  file_id, block_code[:50], type(block_code))
        project_manager = globals.project_manager
        project_id = project_manager.project_id if project_manager and hasattr(project_manager, 'project_id') else None
        if project_id is None:
            log.error("Нет активного проекта для обработки code_patch")
            return res_error(user_name, "Error: No active project selected")

        file_manager = globals.file_manager
        file_data = file_manager.get_file(file_id)
        if not file_data:
            log.error("Файл file_id=%d не найден", file_id)
            return res_error(user_name, f"Error: File {file_id} not found")

        file_name = file_data['file_name']
        log.debug("Данные файла: ~C95%s~C00", str(file_data))
        source = file_data['content']
        if source is None:
            log.error("Файл file_id=%d не считывается", file_id)
            return res_error(user_name, f"Error: File @attach#{file_id} has no contents")

        if isinstance(source, bytes):
            source = source.decode('utf-8', errors='replace')

        try:
            current_lines = source.splitlines(keepends=True)
            patch_lines = block_code.splitlines(keepends=True)
        except Exception as e:
            log.excpt("Ошибка разбиения строк для file_id=%d: %s", file_id, str(e),
                      exc_info=(type(e), e, e.__traceback__))
            return res_error(user_name, "PatchError: Failed to process patch content")

        if not any(line.startswith('@@') for line in patch_lines):
            log.error("Невалидный формат патча для file_id=%d", file_id)
            return res_error(user_name, "PatchError: Invalid patch format, no single @@ was found")

        # Построчный анализ удаляемых строк
        mismatches = []
        current_line_idx = 0
        for patch_line in patch_lines:
            if patch_line.startswith('@@'):
                # Парсим начальную строку из @@ -start,count +start,count @@
                match = re.match(r'@@ -(\d+),(\d+) \+(\d+),(\d+) @@', patch_line)
                if match:
                    current_line_idx = int(match.group(1)) - 1  # Индекс строки в файле (0-based)
                continue
            if patch_line.startswith('-') and not patch_line.startswith('---'):
                remove_sample = patch_line[1:]
                if current_line_idx < len(current_lines):
                    real_text = current_lines[current_line_idx]
                    if remove_sample != real_text:
                        mismatches.append(f"{current_line_idx + 1}:{remove_sample.rstrip()}:{real_text.rstrip()}\n")
                else:
                    mismatches.append(f"{current_line_idx + 1}:{remove_sample.rstrip()}:[EOF]\n")
                current_line_idx += 1
            elif not patch_line.startswith('+'):
                current_line_idx += 1  # Пропускаем неизменённые строки

        if mismatches:
            mismatch_text = "".join(mismatches)
            log.error("Патч не соответствует содержимому файла file_id=%d:\n%s", file_id, mismatch_text)
            return res_error(user_name, f"PatchError: Removed lines do not match file content.\n<mismatch>{mismatch_text}</mismatch>")

        try:
            diff = ['--- {}\n'.format(file_name), '+++ {}\n'.format(file_name)] + patch_lines
            result = []
            for line in patch_lines:
                if line.startswith('@@'):
                    continue
                elif line.startswith('-'):
                    continue
                elif line.startswith('+'):
                    result.append(line[1:])
                else:
                    result.append(line)
            new_content = ''.join(result)
            if new_content == source:
                log.debug("Патч для file_id=%d не вносит изменений", file_id)
                return res_success(user_name, f"Файл {file_id} не изменён")

            res = file_manager.update_file(
                file_id=file_id,
                content=new_content,
                file_name=file_name,
                timestamp=int(time.time()),
                project_id=project_id
            )
            content_bytes = new_content.encode('utf-8')
            md5 = hashlib.md5(content_bytes).hexdigest()
            if res > 0:
                log.debug("Применён патч для file_id=%d, file_name=%s, project_id=%s",
                          file_id, file_name, str(project_id) if project_id is not None else "None")
                return res_success(user_name, f"Файл @attach#{file_id} успешно модифицирован, MD5:{md5}")
            else:
                log.error("Ошибка записи обновленного контента в %s, функция вернула %d", file_name, res)
                return res_error(user_name, f"Error: Failed store @attach#{file_id}, returned code {res}")
        except Exception as e:
            log.excpt("Ошибка применения патча для file_id=%d: %s", file_id, str(e),
                      exc_info=(type(e), e, e.__traceback__))
            return res_error(user_name, f"Error: Failed to apply patch to {file_name}: {e}")

class ShellCodeProcessor(BlockProcessor):
    def __init__(self):
        super().__init__('shell_code')
        self.replace = False

    def handle_block(self, attrs, block_code):
        shell_command = block_code.strip()
        user_name = attrs.get('user_name', 'Unknown')
        log.debug("Обработка shell_code: command=%s", shell_command[:50])

        if not shell_command:
            log.error("Пустая команда в shell_code")
            return res_error(user_name, "<stdout>Error: Empty shell command</stdout>")

        timeout = int(attrs.get('timeout', 300))
        mcp = attrs.get('mcp', 'true').lower() == 'true'
        project_manager = globals.project_manager
        project_name = project_manager.project_name if project_manager and hasattr(project_manager, 'project_name') else None

        if mcp and not project_name:
            log.error("Отсутствует project_name для MCP команды")
            return res_error(user_name, "<stdout>Error: No project selected for MCP command</stdout>")
        project_name = project_name or 'default'

        user_inputs = []
        input_matches = list(re.finditer(r'<user_input\s+rqs="([^"]*)"\s+ack="([^"]*)"\s*/>', block_code,
                                         flags=re.DOTALL))
        for match in input_matches:
            user_inputs.append({"rqs": match.group(1), "ack": match.group(2)})
            block_code = block_code.replace(match.group(0), '')
        shell_command = block_code.strip()
        log.debug("Обнаружено %d user_input тегов: %s, timeout=%d, mcp=%s, project_name=%s",
                  len(user_inputs), user_inputs, timeout, mcp, project_name)

        if mcp:
            try:
                resp = requests.post(
                    f"{MCP_URL}/exec_commands",
                    json={'command': shell_command, 'user_inputs': user_inputs, 'project_name': project_name,
                          'timeout': timeout},
                    headers={'Authorization': 'Bearer Grok-xAI-Agent-The-Best'},
                    timeout=timeout
                )
                response = resp.text if resp.status_code == 200 else f"<stdout>Ошибка: {resp.status_code}</stdout>"
                log.info("Команда выполнена через MCP: %s, статус=%d, вывод=%s",
                         shell_command, resp.status_code, response[:50])
                return res_success(user_name, response) if resp.status_code == 200 else res_error(user_name, response)
            except requests.RequestException as e:
                log.excpt("Ошибка вызова MCP API для команды %s: %s", shell_command, str(e),
                          exc_info=(type(e), e, e.__traceback__))
                return res_error(user_name, f"<stdout>Error: MCP API call failed: {str(e)}</stdout>")
        else:
            result = execute(shell_command, user_inputs, user_name, timeout=timeout)
            return res_success(user_name, result["message"]) if result["status"] == "success" else res_error(user_name, result["message"])

def process_message(text, timestamp, user_name, rql=None):
    log.debug("Обработка сообщения: text=%s, timestamp=%d, user_name=%s, rql=%s", text[:50], timestamp,
              user_name or "None", str(rql))
    if isinstance(text, bytes):
        text = text.decode('utf-8', errors='replace')
        log.warn("Входной текст был байтовым, декодирован: %s", text[:50])
    elif not isinstance(text, str):
        log.error("Неверный тип входного текста: %s", type(text))
        return {"handled_cmds": 0, "failed_cmds": 1, "processed_msg": text,
                "agent_reply": f"@{user_name or 'Unknown'} <stdout>Error: Invalid input type</stdout>"}

    processors = [
        CommandProcessor(),
        FileEditProcessor(),
        FilePatchProcessor(),
        ShellCodeProcessor()
    ]
    log.info("Инициализировано %d процессоров для обработки сообщения", len(processors))

    # Динамически формируем паттерн из тегов процессоров
    tag_pattern = r'<(' + '|'.join(re.escape(processor.tag) for processor in processors) + r')\b'
    log.debug("Сформирован динамический паттерн для тегов: %s", tag_pattern)

    processed_msg = text
    agent_reply = []
    has_code_file = False
    handled_cmds = 0
    failed_cmds = 0
    agent_requested = text.strip().startswith('@agent')
    command_text = text.replace('@agent', '', 1).strip()
    have_tag = re.search(tag_pattern, command_text, re.DOTALL)
    if have_tag:
        # Цикл обработки разными процессорами
        for processor in processors:
            result = processor.process(command_text)
            processed_msg = result["processed_message"]
            if processed_msg:
                command_text = processed_msg  # для следующей итерации - обработанный текст
            agent_reply.extend(result["agent_messages"])
            handled_cmds += result["handled_cmds"]
            failed_cmds += result["failed_cmds"]
            if result["has_code_file"]:
                has_code_file = True
            if result["handled_cmds"] or result["failed_cmds"]:
                log.debug("Процессор %s обработал %d команд, неуспешно %d",
                          processor.tag, result["handled_cmds"], result["failed_cmds"])
    else:
        log.debug("Нет тегов согласно паттерну для %s", command_text)

    agent_reply_text = "\n".join(agent_reply) if agent_reply else None
    return {"handled_cmds": handled_cmds, "failed_cmds": failed_cmds, "processed_msg": processed_msg,
            "agent_reply": agent_reply_text, "has_code_file": has_code_file}