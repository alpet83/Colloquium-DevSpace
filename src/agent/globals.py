# /agent/globals.py, updated 2025-07-26 16:30 EEST
from lib.basic_logger import BasicLogger
from fastapi import Request, HTTPException

user_manager = None
chat_manager = None
post_manager = None
file_manager = None
project_manager = None
replication_manager = None
post_processor = None

# Хранилище для событий переключения чата (user_id:chat_id -> asyncio.Event)
chat_switch_events = {}

LOG_DIR = "/app/logs"
LOG_FILE = LOG_DIR + "/colloquium_core.log"
LOG_SERV = LOG_DIR + "/colloquium_serv.log"
LOG_FORMAT = '[%(asctime)s]. #%(levelname)s(%(name)s): %(message)s'

CONFIG_FILE = "/app/data/colloquium_config.toml"
PRE_PROMPT_PATH = "/app/docs/llm_pre_prompt.md"
CHAT_META_DIR = "/app/projects/.chat-meta"

MCP_AUTH_TOKEN = "Grok-xAI-Agent-The-Best"

ATTACHES_REGEX = r"@attach_dir[#:]([\w\/\"']+)|@attached_file[#:](\d+)|@attach_index[#:](\d+)|@attached_files[#:]\[([\"'\d+, ]+)]"

# форматирование строк даты-времени
SQL_TIMESTAMP = "%Y-%m-%d %H:%M:%S"
SQL_TIMESTAMP6 = "%Y-%m-%d %H:%M:%S.%f"

loggers = {}

sessions_table = None

def get_logger(name, stdout=None):
    if name not in loggers:
        loggers[name] = BasicLogger(name, name, stdout)
    return loggers[name]

def check_session(request: Request) -> int:
    """Проверяет сессию и возвращает user_id или вызывает HTTPException."""
    log = get_logger('session')
    session_id = request.cookies.get("session_id")
    if not session_id:
        log.info(f"Отсутствует session_id для IP={request.client.host}")
        raise HTTPException(status_code=401, detail="No session")

    row = sessions_table.select_row(columns=['user_id'], conditions={'session_id': session_id})
    if not row:
        log.info(f"Неверный session_id для IP={request.client.host}")
        raise HTTPException(status_code=401, detail="Invalid session")
    uid = row[0]
    return uid


def handle_exception(message: str, e: Exception, _raise: bool = True):  # TODO: надо будет куда-то переместить
    """Общая функция для обработки исключений сервера."""
    log = get_logger("exception")
    if isinstance(e, HTTPException):
        log.error(f"HTTP ошибка: {message}: {str(e)}")
        if _raise:
            raise e
    else:
        log.excpt(f"Ошибка сервера: {message}: ", e=e)
        if _raise:
            raise HTTPException(status_code=500, detail=f"Server error: {str(e)}")


def unitext(content):
    if isinstance(content, bytes):
        return content.decode('utf-8', errors='replace')
    return content