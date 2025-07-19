# /app/agent/llm_hands.py, updated 2025-07-19 23:30 EEST
import re
import requests
import time
from datetime import datetime
from llm_api import LLMConnection
import globals
from lib.basic_logger import BasicLogger
from lib.execute_commands import execute
from pathlib import Path

MCP_URL = "http://mcp-sandbox:8084"
log = globals.get_logger("llm_hands")


class BlockProcessor:
    def __init__(self, tag):
        self.tag = tag

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
                agent_messages.append(f"@{result.get('user_name', 'Unknown')} {result['message']}")
            else:
                handled_cmds += 1
                agent_messages.append(result["message"])
                msg = result.get('processed_message', '')
                if msg:
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
        super().__init__('command')

    def handle_block(self, attrs, block_code):
        command = block_code.strip().split()[0].lower() if block_code.strip().split() else None
        user_name = attrs.get('user_name', 'Unknown')
        log.debug("Обработка команды: %s", command or "None")
        if not command:
            log.error("Пустая команда")
            return {"status": "error", "message": "Error: Empty command", "user_name": user_name}
        if command == 'ping':
            return {"status": "success", "message": f"@{user_name} pong", "user_name": user_name}
        elif command == 'run_test':
            params = {'project_name': 'default', 'test_name': 'test'}
            resp = requests.get(f"{MCP_URL}/run_test", params=params,
                                headers={'Authorization': 'Bearer Grok-xAI-Agent-The-Best'})
            response = resp.text if resp.status_code == 200 else f"Ошибка: {resp.status_code}"
            return {"status": "success" if resp.status_code == 200 else "error",
                    "message": f"@{user_name} {response}", "user_name": user_name}
        elif command == 'commit':
            data = {'project_name': 'default', 'msg': 'commit msg'}
            resp = requests.post(f"{MCP_URL}/commit", json=data,
                                 headers={'Authorization': 'Bearer Grok-xAI-Agent-The-Best'})
            response = resp.text if resp.status_code == 200 else f"Ошибка: {resp.status_code}"
            return {"status": "success" if resp.status_code == 200 else "error",
                    "message": f"@{user_name} {response}", "user_name": user_name}
        log.error("Неподдерживаемая команда: %s", command)
        return {"status": "error", "message": f"Error: Unsupported command '{command}'", "user_name": user_name}


class FileEditProcessor(BlockProcessor):
    def __init__(self):
        super().__init__('code_file')

    def handle_block(self, attrs, block_code):
        file_name = attrs.get('name')
        if not file_name:
            log.error("Отсутствует атрибут name в code_file")
            return {"status": "error", "message": "Error: Missing file name", "user_name": attrs.get('user_name')}

        log.debug("Обработка code_file: file_name=%s, content_length=%d", file_name, len(block_code))
        project_manager = globals.project_manager
        project_id = project_manager.project_id if project_manager and hasattr(project_manager, 'project_id') else None
        if project_id is None:
            log.error("Нет активного проекта для обработки code_file")
            return {"status": "error", "message": "Error: No active project selected"}

        try:
            safe_path = (Path('/app/projects') / file_name).resolve()
            if not str(safe_path).startswith('/app/projects'):
                log.error("Недопустимый путь файла: %s", file_name)
                return {"status": "error", "message": "Error: File path outside /app/projects"}
        except Exception as e:
            log.excpt("Ошибка проверки пути файла %s: %s", file_name, str(e),
                      exc_info=(type(e), e, e.__traceback__))
            return {"status": "error", "message": "Error: Invalid file path"}

        file_manager = globals.file_manager
        content_bytes = block_code.encode('utf-8')
        file_id = file_manager.exists(file_name, project_id)
        action = "сохранён" if file_id else "создан"
        if file_id:
            try:
                file_manager.update_file(
                    file_id=file_id,
                    content=content_bytes,
                    file_name=file_name,
                    timestamp=int(time.time()),
                    project_id=project_id
                )
            except Exception as e:
                log.excpt("Ошибка обновления файла file_id=%d: %s", file_id, str(e),
                          exc_info=(type(e), e, e.__traceback__))
                return {"status": "error", "message": f"Error: Failed to update file {file_name}"}
        else:
            file_id = file_manager.add_file(
                content=content_bytes,
                file_name=file_name,
                timestamp=int(time.time()),
                project_id=project_id
            )
        return {
            "status": "success",
            "processed_message": f"@attach#{file_id}",
            "message": f"Файл {file_id} успешно {action}",
            "user_name": attrs.get('user_name')
        }


class FilePatchProcessor(BlockProcessor):
    def __init__(self):
        super().__init__('code_patch')

    def handle_block(self, attrs, block_code):
        file_id = attrs.get('file_id')
        if not file_id:
            log.error("Отсутствует атрибут file_id в code_patch")
            return {"status": "error", "message": "Error: Missing file_id", "user_name": attrs.get('user_name')}

        try:
            file_id = int(file_id)
        except ValueError:
            log.error("Неверный формат file_id: %s", file_id)
            return {"status": "error", "message": "Error: Invalid file_id format", "user_name": attrs.get('user_name')}

        if isinstance(block_code, bytes):
            block_code = block_code.decode('utf-8', errors='replace')
            log.warn("patch_content был байтовым, декодирован: %s", block_code[:50])
        elif not isinstance(block_code, str):
            log.error("Неверный тип patch_content для file_id=%d: %s", file_id, type(block_code))
            return {"status": "error", "message": "Error: Invalid patch content type",
                    "user_name": attrs.get('user_name')}

        log.debug("Обработка code_patch: file_id=%d, patch_content=~C95%s~C00, type=%s",
                  file_id, block_code[:50], type(block_code))
        project_manager = globals.project_manager
        project_id = project_manager.project_id if project_manager and hasattr(project_manager, 'project_id') else None
        if project_id is None:
            log.error("Нет активного проекта для обработки code_patch")
            return {"status": "error", "message": "Error: No active project selected",
                    "user_name": attrs.get('user_name')}

        file_manager = globals.file_manager
        file_data = file_manager.get_file(file_id)
        if not file_data:
            log.error("Файл file_id=%d не найден", file_id)
            return {"status": "error", "message": f"Error: File {file_id} not found",
                    "user_name": attrs.get('user_name')}

        file_name = file_data['file_name']
        log.debug("Данные файла: ~C95%s~C00", str(file_data))
        current_content = file_data['content']
        if isinstance(current_content, bytes):
            if not current_content:
                log.warn("Файл file_id=%d не содержит контента, попытка загрузки с диска", file_id)
                current_content = project_manager.read_project_file(file_name)
                if not current_content:
                    log.error("Не удалось загрузить файл %s с диска", file_name)
                    return {"status": "error", "message": f"Error: Failed to read file {file_name}",
                            "user_name": attrs.get('user_name')}
            current_content = current_content.decode('utf-8', errors='replace')

        try:
            current_lines = current_content.splitlines(keepends=True)
            patch_lines = block_code.splitlines(keepends=True)
        except Exception as e:
            log.excpt("Ошибка разбиения строк для file_id=%d: %s", file_id, str(e),
                      exc_info=(type(e), e, e.__traceback__))
            return {"status": "error", "message": "Error: Failed to process patch content",
                    "user_name": attrs.get('user_name')}

        if not any(line.startswith('@@') for line in patch_lines):
            log.error("Невалидный формат патча для file_id=%d", file_id)
            return {"status": "error", "message": "Error: Invalid patch format", "user_name": attrs.get('user_name')}

        old_lines = [line[1:] for line in patch_lines if line.startswith('-') and not line.startswith('---')]
        old_text = ''.join(old_lines)
        if old_text and old_text not in ''.join(current_lines):
            log.error("Патч не соответствует содержимому файла file_id=%d: удаляемые строки=~C95%s~C00",
                      file_id, old_text)
            return {"status": "error", "message": "Error: Patch does not match file content",
                    "user_name": attrs.get('user_name')}

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
            if new_content == current_content:
                log.debug("Патч для file_id=%d не вносит изменений", file_id)
                return {"status": "success", "message": f"Файл {file_id} не изменён",
                        "user_name": attrs.get('user_name')}

            content_bytes = new_content.encode('utf-8')
            file_manager.update_file(
                file_id=file_id,
                content=content_bytes,
                file_name=file_name,
                timestamp=int(time.time()),
                project_id=project_id
            )
            log.debug("Применён патч для file_id=%d, file_name=%s, project_id=%s",
                      file_id, file_name, str(project_id) if project_id is not None else "None")
            return {"status": "success", "message": f"Файл {file_id} успешно сохранён",
                    "user_name": attrs.get('user_name')}
        except Exception as e:
            log.excpt("Ошибка применения патча для file_id=%d: %s", file_id, str(e),
                      exc_info=(type(e), e, e.__traceback__))
            return {"status": "error", "message": f"Error: Failed to apply patch to {file_name}",
                    "user_name": attrs.get('user_name')}


class ShellCodeProcessor(BlockProcessor):
    def __init__(self):
        super().__init__('shell_code')

    def handle_block(self, attrs, block_code):
        shell_command = block_code.strip()
        log.debug("Обработка shell_code: command=%s", shell_command[:50])

        if not shell_command:
            log.error("Пустая команда в shell_code")
            return {"status": "error", "message": "<stdout>Error: Empty shell command</stdout>",
                    "user_name": attrs.get('user_name')}

        timeout = int(attrs.get('timeout', 300))
        mcp = attrs.get('mcp', 'true').lower() == 'true'
        user_name = attrs.get('user_name', 'Unknown')
        project_manager = globals.project_manager
        project_name = project_manager.project_name if project_manager and hasattr(project_manager,
                                                                                   'project_name') else None

        if mcp and not project_name:
            log.error("Отсутствует project_name для MCP команды")
            return {"status": "error", "message": "<stdout>Error: No project selected for MCP command</stdout>",
                    "user_name": user_name}
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
                return {"status": "success" if resp.status_code == 200 else "error",
                        "message": response, "user_name": user_name}
            except requests.RequestException as e:
                log.excpt("Ошибка вызова MCP API для команды %s: %s", shell_command, str(e),
                          exc_info=(type(e), e, e.__traceback__))
                return {"status": "error", "message": f"<stdout>Error: MCP API call failed: {str(e)}</stdout>",
                        "user_name": user_name}
        else:
            return execute(shell_command, user_inputs, user_name, timeout=timeout)


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

    if agent_requested and command_text.split():
        first_word = command_text.split()[0].lower()
        command_result = CommandProcessor().process(f"<command user_name=\"{user_name}\">{first_word}</command>")
        processed_msg = command_text[len(first_word):].strip()
        agent_reply.extend(command_result["agent_messages"])
        handled_cmds += command_result["handled_cmds"]
        failed_cmds += command_result["failed_cmds"]
        if command_result["handled_cmds"] or command_result["failed_cmds"]:
            log.debug("Процессор command обработал %d команд, неуспешно %d",
                      command_result["handled_cmds"], command_result["failed_cmds"])
        if handled_cmds == 0 and failed_cmds == 0:
            agent_reply.append(f"@{user_name or 'Unknown'} <stdout>Уточните команду</stdout>")
            log.warn("Ни одна команда не обработана для сообщения: %s", text[:50])
            failed_cmds += 1
    else:
        log.debug("Агент не запрошен в сообщении")


    agent_reply_text = "\n".join(agent_reply) if agent_reply else None
    return {"handled_cmds": handled_cmds, "failed_cmds": failed_cmds, "processed_msg": processed_msg,
            "agent_reply": agent_reply_text, "has_code_file": has_code_file}