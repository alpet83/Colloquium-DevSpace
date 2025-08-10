# /app/agent/processors/block_processor.py, updated 2025-07-23 18:32 EEST
import re
import traceback
import requests
import globals
import hashlib
import time
from pathlib import Path

MCP_URL = "http://mcp-sandbox:8084"
log = globals.get_logger("llm_proc")

class ProcessorError(Exception):
    def __init__(self, message, user_name='Unknown'):
        super().__init__(message)
        self.user_name = user_name

class ProcessResult:
    def __init__(self, status, message, user_name, processed_message=None):
        self.status = status
        self.message = message
        self.user_name = user_name
        self.processed_message = processed_message
        self.call_stack = None
        self.agent_messages = []

    def is_ok(self):
        return self.status == "success"

    def is_error(self):
        return self.status == "error"

    @property
    def handled_cmds(self):
        return 1 if self.is_ok() else 0

    @property
    def failed_cmds(self):
        return 1 if self.is_error() else 0

def res_success(user, msg, pmsg=None, agent_messages=None):
    result = ProcessResult("success", msg, user, pmsg)
    result.agent_messages = agent_messages or []
    return result

def res_error(user, msg, pmsg=None, agent_messages=None):
    result = ProcessResult("error", msg, user, pmsg)
    result.agent_messages = agent_messages or []
    result.call_stack = "  ".join(traceback.format_stack(limit=5)).strip()
    return result

class BlockProcessor:
    def __init__(self, tag):
        self.tag = tag
        self.replace = True

    def process(self, post_message: str, user_name: str = '@self'):
        pattern = fr'<{self.tag}(?:\s+([^>]+))?>\s*([\s\S\n\r]*?)\s*</{self.tag}>'
        matches = list(re.finditer(pattern, post_message, flags=re.DOTALL))
        count = len(matches)
        if 0 == count:
            pattern = fr'<{self.tag}(?:\s+([^>]+))?(/)>'  # short tag form
            matches = list(re.finditer(pattern, post_message, flags=re.DOTALL))
            count = len(matches)

        if count > 0:
            log.debug("Найдено %d совпадений для тега %s", count, self.tag)
        processed_message = post_message
        agent_messages = []
        handled_cmds = 0
        failed_cmds = 0
        for match in matches:
            attrs = self._parse_attrs(match.group(1) or '')
            block_code = match.group(2) or ''
            try:
                if attrs.get('user_name', None) is None:
                    attrs['user_name'] = user_name
                result = self.handle_block(attrs, block_code)
            except ProcessorError as e:
                failed_cmds += 1
                exc_info = (type(e), e, e.__traceback__)
                backtrace = "".join(traceback.format_exception(*exc_info))
                agent_messages.append(f"@{e.user_name}: {str(e)}\n<traceback>{backtrace}</traceback>")
                continue
            handled_cmds += result.handled_cmds
            failed_cmds += result.failed_cmds
            agent_messages.append(result.message)
            agent_messages.extend(result.agent_messages)
            if result.is_error() and result.call_stack:
                agent_messages.append(f"<traceback>{result.call_stack}</traceback>")
            if result.processed_message and self.replace:
                processed_message = processed_message.replace(match.group(0), result.processed_message)
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

    def validate_file_id(self, file_id, user_name):
        if not file_id:
            log.error("Отсутствует атрибут file_id в %s", self.tag)
            raise ProcessorError("Error: Missing file_id", user_name)
        try:
            file_id = int(file_id)
            log.debug("Validated file_id=%d", file_id)
            return file_id
        except ValueError:
            log.error("Неверный формат file_id: %s", file_id)
            raise ProcessorError("Error: Invalid file_id format", user_name)

    def get_file_data(self, file_id, user_name):
        project_manager = globals.project_manager
        project_id = project_manager.project_id if project_manager and hasattr(project_manager, 'project_id') else None
        if project_id is None:
            log.error("Нет активного проекта для обработки %s", self.tag)
            raise ProcessorError("Error: No active project selected", user_name)

        file_manager = globals.file_manager
        file_data = file_manager.get_file(file_id)
        if not file_data:
            log.error("Файл file_id=%d не найден", file_id)
            raise ProcessorError(f"Error: File {file_id} not found", user_name)

        file_name = file_data['file_name']
        source = file_data['content']
        if source is None:
            log.error("Файл file_id=%d не считывается", file_id)
            raise ProcessorError(f"Error: File @attach#{file_id} has no contents", user_name)

        if isinstance(source, bytes):
            source = source.decode('utf-8', errors='replace')
        log.debug("Retrieved file data for file_id=%d, file_name=%s", file_id, file_name)
        return file_name, source, project_id

    def save_file(self, file_id: int, file_name: str, new_content: str, project_id, user_name, timestamp=None):
        try:
            assert(len(file_name) < 300), "Invalid file_name length"
            old_lines_count = len(new_content.splitlines())
            content_bytes = new_content.encode('utf-8')
            md5 = hashlib.md5(content_bytes).hexdigest()
            file_man = globals.file_manager
            res = file_man.update_file(
                file_id=file_id,
                content=new_content,
                timestamp=timestamp if timestamp is not None else int(time.time()),
                project_id=project_id
            )
            if res > 0:
                new_lines_count = len(new_content.splitlines())
                log.debug("Saved file_id=%d, file_name=%s, project_id=%s, old_lines=%d, new_lines=%d, MD5=%s",
                          file_id, file_name, str(project_id) if project_id is not None else "None", old_lines_count, new_lines_count, md5)
                return res_success(user_name, f"Файл @attach#{file_id} успешно модифицирован, MD5:{md5}, было {old_lines_count} строк, стало {new_lines_count} строк")
            else:
                log.error("Ошибка записи обновленного контента в %s, функция вернула %d", file_name, res)
                raise ProcessorError(f"Error: Failed to store @attach#{file_id}, returned code {res}", user_name)
        except Exception as e:
            log.excpt("Ошибка сохранения файла file_id=%d: ", file_id, e=e)
            raise ProcessorError(f"Error: Failed to save {file_name}: {e}", user_name)

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