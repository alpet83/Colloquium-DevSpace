# /agent/globals.py, updated 2025-07-26 16:30 EEST
from lib.basic_logger import BasicLogger
from fastapi import Request, HTTPException
import threading
import contextvars
import base64
import hashlib
import hmac
import os

user_manager = None
""" user_manager (UserManager) - control users/actors"""
chat_manager = None
""" chat_manager (ChatManager) - control chats: adding, deleting, list"""
post_manager = None
""" post_manager (PostManager) - control posts: adding, editing, deleting, get_history"""
file_manager = None
""" file_manager (FileManager) - control file attaches, common operations"""
project_manager = None
""" project_manager (ProjectManager) - server-level default project manager (set at startup, read-only after) """
current_project_manager: contextvars.ContextVar = contextvars.ContextVar('current_project_manager', default=None)
""" current_project_manager (ContextVar[ProjectManager]) - per-request project manager, async-safe """
project_registry = {}
""" project_registry (dict[int, ProjectManager]) - cache of managers by project_id """
project_scan_state = {}
""" project_scan_state (dict[int, dict]) - scan freshness metadata by project_id """
project_index_epoch = {}
""" project_index_epoch (dict[int, int]) — монотонно растёт после каждого mark_scan_fresh (рескан файлов) """


def get_project_index_epoch(project_id: int) -> int:
    try:
        return int(project_index_epoch.get(int(project_id), 0))
    except (TypeError, ValueError):
        return 0


def bump_project_index_epoch(project_id: int) -> int:
    pid = int(project_id)
    n = int(project_index_epoch.get(pid, 0)) + 1
    project_index_epoch[pid] = n
    return n
replication_manager = None
""" replication_manager (ReplicationManager) - control replication: interaction with LLMs"""
post_processor = None

ADMIN_UID = 1
AGENT_UID = 2

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

_named_locks: dict[str, threading.RLock] = {}
_named_locks_guard = threading.Lock()
_token_secret_cache = None

sessions_table = None  # will be assumed DataTable object
session_runtime_options = {}


def get_logger(name, stdout=None):
    if name not in loggers:
        loggers[name] = BasicLogger(name, name, stdout)
    return loggers[name]


def set_session_option(session_id: str, key: str, value):
    if not session_id or not key:
        return
    opts = session_runtime_options.get(session_id)
    if opts is None:
        opts = {}
        session_runtime_options[session_id] = opts
    opts[key] = value


def get_session_option(session_id: str | None, key: str, default=None):
    if not session_id or not key:
        return default
    opts = session_runtime_options.get(session_id) or {}
    return opts.get(key, default)


def get_named_lock(name: str) -> threading.RLock:
    """Return a process-wide reentrant lock by logical name."""
    key = str(name or '').strip()
    if not key:
        key = '__default__'
    with _named_locks_guard:
        lock = _named_locks.get(key)
        if lock is None:
            lock = threading.RLock()
            _named_locks[key] = lock
        return lock


def request_session_tag(request: Request) -> str:
    """Return short session tag (8 chars) for request-bound logging."""
    if request is None:
        return ""
    state = getattr(request, 'state', None)
    if state is not None:
        tag = getattr(state, 'session_tag', None)
        if tag:
            return str(tag)
    session_id = request.cookies.get("session_id") if request.cookies else None
    if not session_id:
        return ""
    return str(session_id)[:8]


def with_session_tag(request: Request, fmt: str) -> str:
    """Prefix log format with dark-blue short session tag, if available."""
    tag = request_session_tag(request)
    if not tag:
        return fmt
    return f"~C34{tag}~C00 {fmt}"


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


def _load_token_secret() -> str | None:
    global _token_secret_cache
    if _token_secret_cache is not None:
        return _token_secret_cache

    secret = os.getenv('POSTGRES_ROOT_PASSWORD') or os.getenv('PGPASSWORD')
    if not secret:
        for env_name in ('POSTGRES_ROOT_PASSWORD_FILE', 'PGPASSWORD_FILE'):
            fpath = os.getenv(env_name)
            if fpath and os.path.exists(fpath):
                try:
                    with open(fpath, 'r', encoding='utf-8') as fh:
                        secret = (fh.read() or '').strip()
                        if secret:
                            break
                except Exception:
                    pass

    _token_secret_cache = secret or None
    return _token_secret_cache


def _token_key() -> bytes | None:
    secret = _load_token_secret()
    if not secret:
        return None
    return hashlib.sha256(secret.encode('utf-8')).digest()


def _keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < length:
        block = hmac.new(key, nonce + counter.to_bytes(8, 'big'), hashlib.sha256).digest()
        out.extend(block)
        counter += 1
    return bytes(out[:length])


def is_encrypted_token(value: str | None) -> bool:
    return isinstance(value, str) and value.startswith('enc:v1:')


def encrypt_token(value: str | None) -> str | None:
    if value is None or value == '':
        return value
    if is_encrypted_token(value):
        return value

    key = _token_key()
    if not key:
        get_logger('tokensec').warn('Token encryption key not configured; llm_token will remain plaintext')
        return value

    plain = value.encode('utf-8')
    nonce = os.urandom(16)
    cipher = bytes([a ^ b for a, b in zip(plain, _keystream(key, nonce, len(plain)))])
    tag = hmac.new(key, b'enc:v1:' + nonce + cipher, hashlib.sha256).digest()[:16]
    packed = base64.urlsafe_b64encode(nonce + tag + cipher).decode('ascii').rstrip('=')
    return 'enc:v1:' + packed


def decrypt_token(value: str | None) -> str | None:
    if value is None or value == '':
        return value
    if not is_encrypted_token(value):
        return value

    key = _token_key()
    if not key:
        get_logger('tokensec').warn('Token decryption key not configured')
        return None

    raw = value[len('enc:v1:'):]
    padding = '=' * (-len(raw) % 4)
    blob = base64.urlsafe_b64decode(raw + padding)
    if len(blob) < 32:
        raise ValueError('Encrypted token payload is too short')

    nonce = blob[:16]
    tag = blob[16:32]
    cipher = blob[32:]
    expected = hmac.new(key, b'enc:v1:' + nonce + cipher, hashlib.sha256).digest()[:16]
    if not hmac.compare_digest(tag, expected):
        raise ValueError('Encrypted token integrity check failed')
    plain = bytes([a ^ b for a, b in zip(cipher, _keystream(key, nonce, len(cipher)))])
    return plain.decode('utf-8')