import requests
import re
import globals
from lib.execute_commands import execute
from processors.block_processor import BlockProcessor, res_error, res_success, MCP_URL

log = globals.get_logger("llm_proc")

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
