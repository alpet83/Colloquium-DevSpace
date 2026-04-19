# /agent/server.py, updated 2025-07-19 09:55 EEST
from fastapi import FastAPI, Request
from starlette.requests import ClientDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
import asyncio
import logging
import time
import socket
import os
import signal
import subprocess
import sys
import toml
import datetime
import uvicorn
from logging import FileHandler
from lib.basic_logger import BasicLogger
from lib.file_watchdog import watch_files
from routes.auth_routes import router as auth_router
from routes.chat_routes import router as chat_router
from routes.file_routes import router as file_router
from routes.project_routes import router as project_router
from routes.config_routes import router as config_router
from routes.core_routes import router as core_router
from post_processor import PostProcessor
from managers.db import DataTable
from managers.users import UserManager
from managers.chats import ChatManager
from managers.posts import PostManager
from managers.files import FileManager
from managers.project import ProjectManager
from managers.replication import ReplicationManager
from managers.runtime_config import get_bool, is_runtime_config_set
import globals
from globals import CONFIG_FILE, LOG_DIR, LOG_FILE, LOG_SERV, LOG_FORMAT

class UnicornException(Exception):
    def __init__(self, name: str):
        self.name = name

app = FastAPI()
log = globals.get_logger("core")
_BOOT_T0 = time.perf_counter()
_maint_child_proc: subprocess.Popen | None = None


def get_maint_child_state() -> dict:
    """PID дочернего core_maint_loop.py (если есть) и признак, что процесс ещё жив."""
    global _maint_child_proc
    if _maint_child_proc is None:
        return {"pid": None, "alive": False}
    try:
        pid = int(_maint_child_proc.pid)
    except Exception:
        pid = None
    alive = False
    try:
        alive = _maint_child_proc.poll() is None
    except Exception:
        alive = False
    return {"pid": pid, "alive": bool(alive)}


def _boot_ms() -> float:
    """Milliseconds elapsed since process boot for startup profiling."""
    return (time.perf_counter() - _BOOT_T0) * 1000.0


def _log_boot_phase(phase: str, started_at: float) -> float:
    """Log one startup phase duration and global elapsed time."""
    dur_ms = (time.perf_counter() - started_at) * 1000.0
    log.info("CORE_BOOT phase=%s dur_ms=%.1f since_boot_ms=%.1f", phase, dur_ms, _boot_ms())
    return dur_ms

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://vps.vpn:8008", "http://localhost:8008"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "Cookie", "Set-Cookie", "Accept"],
    expose_headers=["Set-Cookie"]
)


@app.middleware("http")
async def session_context_middleware(request: Request, call_next):
    """Store short session tag in request.state for request-scoped logging."""
    session_id = request.cookies.get('session_id')
    request.state.session_tag = str(session_id)[:8] if session_id else ""
    response = await call_next(request)
    return response


@app.middleware("http")
async def log_requests_and_exceptions(request: Request, call_next):
    # NO_LOG: NEVER LOG HERE, NEVER AGAIN, IS PROHIBITED!
    try:
        response = await call_next(request)
        return response
    except RequestValidationError as exc:
        log.error("Валидационная ошибка для %s %s: ~C95%s~C00", request.method, str(request.url), str(exc.errors()))
        raise
    except ClientDisconnect:
        raise
    except Exception as exc:
        log.excpt("Ошибка сервера для %s %s: ", request.method, str(request.url), e=exc)
        raise


def _slow_request_threshold_ms() -> int:
    """CORE_SLOW_REQUEST_LOG_MS>0 — в colloquium_core.log пишутся запросы дольше порога (диагностика под нагрузкой)."""
    try:
        return max(0, int(os.getenv("CORE_SLOW_REQUEST_LOG_MS", "0")))
    except ValueError:
        return 0


_SLOW_REQ_MS = _slow_request_threshold_ms()
if _SLOW_REQ_MS > 0:

    @app.middleware("http")
    async def slow_request_middleware(request: Request, call_next):
        t0 = time.perf_counter()
        response = await call_next(request)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        if elapsed_ms >= _SLOW_REQ_MS:
            log.warn(
                "CORE_SLOW_REQ %s %s %.0fms status=%s",
                request.method,
                request.url.path,
                elapsed_ms,
                getattr(response, "status_code", "?"),
            )
        return response


@app.exception_handler(UnicornException)
async def unicorn_exception_handler(request: Request, exc: UnicornException):
    log.excpt("Unicorn raised ", e=exc)
    return JSONResponse(
        status_code=418,
        content={"message": f"Oops! {exc.name} did something. There goes a rainbow..."},
    )


def log_msg(message, tag="#INFO"):
    now = datetime.datetime.now()
    ts = now.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}]. {tag}: {message}", file=sys.stderr)

def log_init():
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    handler = logging.FileHandler(filename=LOG_FILE, mode='w', encoding='utf-8')
    handler.setLevel(logging.DEBUG)
    formatter = logging.Formatter(LOG_FORMAT)
    handler.setFormatter(formatter)
    root.addHandler(handler)

def server_init():
    try:
        _t_server_init = time.perf_counter()
        log_msg("Сервер Colloquium запускается...", "#INIT")
        if not os.path.exists(LOG_DIR):
            os.makedirs(LOG_DIR)
        log_init()
        log.info("CORE_BOOT phase=server_init_start since_boot_ms=%.1f", _boot_ms())
        _t_phase = time.perf_counter()
        globals.sessions_table = DataTable(table_name='sessions',
                                           template=[
                                               'session_id TEXT PRIMARY KEY',
                                               'user_id INTEGER',
                                               'active_chat INTEGER',
                                               'active_project INTEGER'
                                           ]
                                           )
        _log_boot_phase("sessions_table", _t_phase)

        log.info("Инициализация менеджеров")
        _t_phase = time.perf_counter()
        globals.user_manager = UserManager()
        _log_boot_phase("user_manager", _t_phase)
        _t_phase = time.perf_counter()
        globals.chat_manager = ChatManager()
        _log_boot_phase("chat_manager", _t_phase)
        _t_phase = time.perf_counter()
        globals.post_processor = PostProcessor()
        _log_boot_phase("post_processor", _t_phase)
        _t_phase = time.perf_counter()
        globals.post_manager = PostManager(globals.user_manager)
        _log_boot_phase("post_manager", _t_phase)
        globals.project_registry = {}
        _t_phase = time.perf_counter()
        globals.project_manager = ProjectManager()
        _log_boot_phase("project_manager_default", _t_phase)
        # Auto-load first project as default so processors work after restart
        try:
            _t_auto = time.perf_counter()
            first = globals.project_manager.projects_table.select_from(
                columns=['id'], limit=1
            )
            if first:
                globals.project_manager = ProjectManager(first[0][0])
                globals.project_registry[first[0][0]] = globals.project_manager
                log.info("Авто-загружен проект id=%d как дефолтный", first[0][0])
            _log_boot_phase("project_manager_autoload", _t_auto)
        except Exception as _e:
            log.warn("Не удалось авто-загрузить проект: %s", str(_e))
        _t_phase = time.perf_counter()
        globals.file_manager = FileManager(notify_heavy_ops=True)
        _log_boot_phase("file_manager", _t_phase)
        # Сканирование и проверка ссылок — только в фоне после поднятия uvicorn (см. _schedule_startup_file_maintenance).
        # Синхронный полный scan здесь блокировал bind :8080 и давал nginx 502 на /api/* при долгом скане.
        # Важно: не запускать scan_project_files и FileManager.check() параллельно — гонка по attached_files / missing_ttl.
        debug_enabled = get_bool("DEBUG_MODE", default=False)
        log.debug("DEBUG_MODE (config/env) parsed=%s", str(debug_enabled))
        _t_phase = time.perf_counter()
        globals.replication_manager = ReplicationManager(debug_mode=debug_enabled)
        _log_boot_phase("replication_manager", _t_phase)
        log.info("Менеджеры инициализированы")
        log.info("chown agent на /app/projects — в фоне (синхронный chown блокировал bind :8080 → nginx 502)")
        subprocess.Popen(
            ["chown", "agent", "-R", "/app/projects"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

        log.debug("Подключение auth_router")
        _t_phase = time.perf_counter()
        app.include_router(auth_router)
        _log_boot_phase("router_auth", _t_phase)
        log.debug("Подключение chat_router")
        _t_phase = time.perf_counter()
        app.include_router(chat_router)
        _log_boot_phase("router_chat", _t_phase)
        log.debug("Подключение file_router")
        _t_phase = time.perf_counter()
        app.include_router(file_router)
        _log_boot_phase("router_file", _t_phase)
        log.debug("Подключение project_router")
        _t_phase = time.perf_counter()
        app.include_router(project_router)
        _log_boot_phase("router_project", _t_phase)
        log.debug("Подключение config_router")
        _t_phase = time.perf_counter()
        app.include_router(config_router)
        _log_boot_phase("router_config", _t_phase)
        _t_phase = time.perf_counter()
        app.include_router(core_router)
        _log_boot_phase("router_core_status", _t_phase)

        _schedule_startup_file_maintenance()
        _schedule_core_scheduler()
        _schedule_maint_child()
        globals.CORE_SERVER_STARTED_AT = time.time()
        _log_boot_phase("startup_hooks_scheduled", _t_server_init)

    except Exception as e:
        log_msg("Ошибка инициализации сервера: %s" % str(e), "#ERROR")
        raise

shutdown_event = asyncio.Event()


def _maint_child_will_start() -> bool:
    """Те же условия, что и в _schedule_maint_child: будет ли запущен core_maint_loop.py."""
    if not get_bool("CORE_MAINT_CHILD_ENABLED", default=True):
        return False
    if not get_bool("CORE_MAINT_ENABLED", default=False):
        return False
    return os.path.isfile("/app/agent/scripts/core_maint_loop.py")


def _startup_file_maintenance_enabled() -> bool:
    """Полный scan проектов + FileManager.check() сразу после старта HTTP.

    Если ``CORE_STARTUP_FILE_MAINT_ENABLED`` задан явно (env/config) — только он.
    Иначе по умолчанию **не** гоняем тяжёлый проход, когда поднимается дочерний
    ``core_maint_loop`` (find/reconcile/ленивый scan там).
    """
    if is_runtime_config_set("CORE_STARTUP_FILE_MAINT_ENABLED"):
        return get_bool("CORE_STARTUP_FILE_MAINT_ENABLED", default=True)
    return not _maint_child_will_start()


def _schedule_startup_file_maintenance() -> None:
    """После поднятия HTTP: последовательно scan всех проектов, затем FileManager.check().

    Один фоновый проход вместо двух почти параллельных задач — меньше гонок между
    scan/add_file и массовой проверкой ссылок в attached_files.
    Отключается, если задано CORE_STARTUP_FILE_MAINT_ENABLED=0 или (по умолчанию)
    при активном дочернем CORE_MAINT — см. _startup_file_maintenance_enabled().
    """

    @app.on_event("startup")
    async def _startup_file_maintenance() -> None:
        async def _run() -> None:
            await asyncio.sleep(0.25)
            if not _startup_file_maintenance_enabled():
                log.info(
                    "CORE_STARTUP_FILE_MAINT off: skip startup scan+check "
                    "(set CORE_STARTUP_FILE_MAINT_ENABLED=1 to force, or disable maint child)"
                )
                return
            _t_total = time.perf_counter()
            try:
                pm0 = globals.project_manager
                if pm0 is not None:
                    all_projects = pm0.projects_table.select_from(columns=["id"], conditions="id > 0")
                    for proj_row in all_projects or []:
                        _t_proj = time.perf_counter()
                        pid = proj_row[0]
                        pm = ProjectManager.get(pid)
                        if pm is None:
                            continue
                        log.info("Фоновое сканирование файлов проекта id=%d после старта HTTP", pid)
                        await asyncio.to_thread(pm.scan_project_files)
                        _log_boot_phase(f"startup_scan_project_{pid}", _t_proj)
                fm = globals.file_manager
                if fm is not None:
                    _t_chk = time.perf_counter()
                    log.info("Фоновая проверка ссылок attached_files после сканов проектов")
                    await asyncio.to_thread(fm.check)
                    _log_boot_phase("startup_file_check_after_scans", _t_chk)
            except Exception as _e:
                log.warn("Ошибка фонового обслуживания файлов/проектов: %s", str(_e))
            finally:
                _log_boot_phase("startup_file_maintenance_total", _t_total)

        asyncio.create_task(_run())


def _schedule_core_scheduler() -> None:
    try:
        from managers.core_scheduler import start_core_scheduler, stop_core_scheduler
    except ImportError as e:
        log.warn(
            "Планировщик ядра отключён (импорт managers.core_scheduler): %s — "
            "в контейнере: пересоберите образ или дождитесь entrypoint pip install; локально: pip install -r agent/requirements-core.txt",
            str(e),
        )
        return

    @app.on_event("startup")
    async def _core_scheduler_startup() -> None:
        await asyncio.sleep(0.1)
        try:
            await start_core_scheduler()
        except Exception as e:
            log.warn("Планировщик ядра не запущен: %s", str(e))

    @app.on_event("shutdown")
    async def _core_scheduler_shutdown() -> None:
        try:
            await stop_core_scheduler()
        except Exception as e:
            log.warn("Остановка планировщика ядра: %s", str(e))


def _schedule_maint_child() -> None:
    """Запуск maintenance-цикла как дочернего процесса ядра (опционально)."""
    global _maint_child_proc

    @app.on_event("startup")
    async def _maint_child_startup() -> None:
        global _maint_child_proc
        child_on = get_bool("CORE_MAINT_CHILD_ENABLED", default=True)
        maint_on = get_bool("CORE_MAINT_ENABLED", default=False)
        if not child_on:
            log.info("CORE_MAINT child disabled (CORE_MAINT_CHILD_ENABLED=0)")
            return
        if not maint_on:
            log.info("CORE_MAINT child skipped (CORE_MAINT_ENABLED=0)")
            return
        if _maint_child_proc is not None and _maint_child_proc.poll() is None:
            log.info("CORE_MAINT child already running pid=%s", str(_maint_child_proc.pid))
            return
        script = "/app/agent/scripts/core_maint_loop.py"
        if not os.path.exists(script):
            log.warn("CORE_MAINT child not started: script missing %s", script)
            return
        env = os.environ.copy()
        env.setdefault("CORE_MAINT_ENABLED", "1")
        try:
            _maint_child_proc = subprocess.Popen(
                [sys.executable, script],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                env=env,
            )
            log.info(
                "CORE_MAINT child started pid=%d mutate=%s",
                _maint_child_proc.pid,
                env.get("CORE_MAINT_MUTATE", "0"),
            )
            # Быстрая sanity-проверка: процесс не должен завершаться сразу после старта.
            await asyncio.sleep(0.2)
            rc = _maint_child_proc.poll()
            if rc is not None:
                log.warn("CORE_MAINT child exited immediately rc=%s", str(rc))
        except Exception as e:
            log.warn("CORE_MAINT child start failed: %s", str(e))

    @app.on_event("shutdown")
    async def _maint_child_shutdown() -> None:
        global _maint_child_proc
        proc = _maint_child_proc
        if proc is None:
            return
        if proc.poll() is not None:
            _maint_child_proc = None
            return
        try:
            proc.terminate()
            await asyncio.to_thread(proc.wait, 3)
            log.info("CORE_MAINT child stopped pid=%d", proc.pid)
        except Exception:
            try:
                proc.kill()
                log.warn("CORE_MAINT child killed pid=%d", proc.pid)
            except Exception as e:
                log.warn("CORE_MAINT child stop failed: %s", str(e))
        finally:
            _maint_child_proc = None


async def lifespan(app: FastAPI):
    log_init()
    local_ip = socket.gethostbyname(socket.gethostname())
    log.info("Ядро запущено на IP=%s:8080", local_ip)
    asyncio.create_task(watch_files(shutdown_event))
    yield
    log.info("Ядро остановлено")

# app.lifespan = lifespan

async def shutdown():
    log.info("Получен сигнал завершения, инициируется graceful shutdown")
    for logger_name in ['core', 'llm_hands', 'postman', 'fileman', 'postproc']:
        logger = globals.get_logger(logger_name)
        log.info("Очистка логов для %s", logger_name)
        logger.cleanup()
    shutdown_event.set()
    await asyncio.sleep(1)  # Дать время на завершение операций
    log.info("Завершение работы сервера")

def handle_shutdown(signum, frame):
    log.info("Получен сигнал %d, завершение работы", signum)
    asyncio.create_task(shutdown())

signal.signal(signal.SIGTERM, handle_shutdown)
signal.signal(signal.SIGINT, handle_shutdown)
signal.signal(signal.SIGQUIT, handle_shutdown)

if __name__ == "__main__":
    server_init()
    uvicorn_config = uvicorn.Config(
        app=app,
        host="0.0.0.0",
        port=8080,
        log_level="debug",
        log_config={
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "default": {"format": LOG_FORMAT},
                "access": {"format": LOG_FORMAT},
            },
            "handlers": {
                "default": {
                    "class": "logging.FileHandler",
                    "formatter": "default",
                    "filename": LOG_SERV,
                    "mode": "w",
                },
                "access": {
                    "class": "logging.FileHandler",
                    "formatter": "access",
                    "filename": LOG_SERV,
                    "mode": "w",
                },
            },
            "loggers": {
                "uvicorn": {"handlers": ["default"], "level": "DEBUG"},
                "uvicorn.access": {"handlers": ["access"], "level": "DEBUG", "propagate": False},
            },
        },
        timeout_graceful_shutdown=2,
    )
    server = uvicorn.Server(config=uvicorn_config)
    asyncio.run(server.serve())