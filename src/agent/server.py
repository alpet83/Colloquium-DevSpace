# /agent/server.py, updated 2025-07-18 14:50 EEST
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.exceptions import RequestValidationError
import asyncio
import logging
import socket
import os
import signal
import sys
import toml
import datetime
import uvicorn
from logging import FileHandler
from lib.basic_logger import BasicLogger
from routes.auth_routes import router as auth_router
from routes.chat_routes import router as chat_router
from routes.post_routes import router as post_router
from routes.file_routes import router as file_router
from routes.project_routes import router as project_router
from post_processor import PostProcessor
from managers.users import UserManager
from managers.chats import ChatManager
from managers.posts import PostManager
from managers.files import FileManager
from managers.project import ProjectManager
from managers.replication import ReplicationManager
import globals
from globals import CONFIG_FILE, LOG_DIR, LOG_FILE, LOG_SERV, LOG_FORMAT

app = FastAPI()
log = globals.get_logger("core")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://vps.vpn:8008", "http://localhost:8008"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "Cookie", "Set-Cookie", "Accept"],
    expose_headers=["Set-Cookie"]
)

@app.middleware("http")
async def log_requests_and_exceptions(request: Request, call_next):
    # NO_LOG: NEVER LOG HERE, NEVER AGAIN, IS PROHIBITED!
    try:
        response = await call_next(request)
        return response
    except RequestValidationError as exc:
        log.error("Валидационная ошибка для %s %s: ~C95%s~C00", request.method, str(request.url), str(exc.errors()))
        raise
    except Exception as exc:
        log.excpt("Ошибка сервера для %s %s: %s", request.method, str(request.url), str(exc),
                  exc_info=(type(exc), exc, exc.__traceback__))
        raise

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
        log_msg("Сервер Colloquium запускается...", "#INIT")
        if not os.path.exists(LOG_DIR):
            os.makedirs(LOG_DIR)
        log_init()
        log.debug("Подключение auth_router")
        app.include_router(auth_router)
        log.debug("Подключение chat_router")
        app.include_router(chat_router)
        log.debug("Подключение post_router")
        app.include_router(post_router)
        log.debug("Подключение file_router")
        app.include_router(file_router)
        log.debug("Подключение project_router")
        app.include_router(project_router)

        globals.post_processor = PostProcessor()
        log.info("Инициализация менеджеров")
        globals.user_manager = UserManager()
        globals.chat_manager = ChatManager()
        globals.post_manager = PostManager(globals.user_manager)
        globals.project_manager = ProjectManager()
        globals.file_manager = FileManager()
        dbg = os.getenv("DEBUG_MODE", "0").lower()
        log.debug("ENV DEBUG_MODE=%s", dbg)
        globals.replication_manager = ReplicationManager(
            globals.user_manager,
            globals.chat_manager,
            globals.post_manager,
            globals.file_manager,
            debug_mode=(dbg != "0")
        )
        log.info("Менеджеры инициализированы")
    except Exception as e:
        log_msg("Ошибка инициализации сервера: %s" % str(e), "#ERROR")
        raise

shutdown_event = asyncio.Event()

async def lifespan(app: FastAPI):
    config = toml.load(CONFIG_FILE)
    local_ip = socket.gethostbyname(socket.gethostname())
    log.info("Ядро запущено на IP=%s:8080", local_ip)
    asyncio.create_task(chat_loop())
    yield
    log.info("Ядро остановлено")

app.lifespan = lifespan

async def chat_loop():
    while not shutdown_event.is_set():
        for chat_id in globals.chat_manager.list_chats(0):
            await globals.replication_manager.replicate_to_llm(chat_id['chat_id'])
        await asyncio.sleep(5)

async def shutdown():
    log.info("Получен сигнал завершения, инициируется graceful shutdown")
    shutdown_event.set()
    await server.shutdown()

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