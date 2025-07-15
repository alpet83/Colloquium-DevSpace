# /agent/server.py, updated 2025-07-14 19:39 EEST
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
import uvicorn
import datetime
from logging import FileHandler
from routes.auth_routes import router as auth_router
from routes.chat_routes import router as chat_router
from routes.post_routes import router as post_router
from routes.file_routes import router as file_router
from managers.users import UserManager
from managers.chats import ChatManager
from managers.posts import PostManager
from managers.files import FileManager
from managers.replication import ReplicationManager
import globals
from globals import CONFIG_FILE, LOG_DIR, LOG_FILE

app = FastAPI()

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
    logging.debug(
        f"#DEBUG: Запрос {request.method} {request.url}, Query: {request.query_params}, Cookies: {request.cookies}")
    try:
        response = await call_next(request)
        logging.debug(f"#DEBUG: Ответ для {request.method} {request.url}: Status {response.status_code}")
        return response
    except RequestValidationError as exc:
        logging.error(f"#ERROR: Валидационная ошибка для {request.method} {request.url}: {exc.errors()}")
        raise
    except Exception as exc:
        logging.error(f"#ERROR: Ошибка сервера для {request.method} {request.url}: {str(exc)}")
        raise





def log_msg(message, tag="#INFO"):
    now = datetime.datetime.now()
    ts = now.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}]. {tag}: {message}", file=sys.stderr)


def server_init():
    try:
        log_msg(f"Сервер Colloquium запускается...", "#INIT")
        if not os.path.exists(LOG_DIR):
            os.makedirs(LOG_DIR)
        logging.basicConfig(level=logging.DEBUG, format='[%(asctime)s] #%(levelname)s: %(message)s',
                            filename=LOG_FILE, filemode='a')

        log_msg("Подключение auth_router", "#DEBUG")
        app.include_router(auth_router)
        log_msg("Подключение chat_router", "#DEBUG")
        app.include_router(chat_router)
        log_msg("Подключение post_router", "#DEBUG")
        app.include_router(post_router)
        log_msg("Подключение file_router", "#DEBUG")
        app.include_router(file_router)

        log_msg("Инициализация менеджеров")
        globals.user_manager = UserManager()
        globals.chat_manager = ChatManager()
        globals.post_manager = PostManager(globals.user_manager)
        globals.file_manager = FileManager()
        globals.replication_manager = ReplicationManager(globals.user_manager, globals.chat_manager,
                                                         globals.post_manager)
        log_msg("Менеджеры инициализированы")
    except Exception as e:
        log_msg(f"Ошибка инициализации сервера: {str(e)}", "#ERROR")
        raise



shutdown_event = asyncio.Event()


async def lifespan(app: FastAPI):
    config = toml.load(CONFIG_FILE)
    local_ip = socket.gethostbyname(socket.gethostname())
    logging.info(f"#INFO: Ядро запущено на IP {local_ip}:8080")
    asyncio.create_task(chat_loop())
    yield
    logging.info("#INFO: Ядро остановлено")


app.lifespan = lifespan


async def chat_loop():
    while not shutdown_event.is_set():
        history = globals.post_manager.get_history(chat_id=1, limit=1)
        if history and not isinstance(history, dict):
            last_timestamp = history[0]["timestamp"]
            logging.debug(f"#DEBUG: Чат проверен, последнее: #post_{last_timestamp}")
        await asyncio.sleep(5)


async def shutdown():
    logging.info("#INFO: Получен сигнал завершения, инициируется graceful shutdown")
    shutdown_event.set()
    await server.shutdown()


def handle_shutdown(signum, frame):
    logging.info(f"#INFO: Получен сигнал {signum}, завершение работы")
    asyncio.create_task(shutdown())


signal.signal(signal.SIGTERM, handle_shutdown)
signal.signal(signal.SIGINT, handle_shutdown)
signal.signal(signal.SIGQUIT, handle_shutdown)

if __name__ == "__main__":
    server_init()
    uvicorn_config = uvicorn.config.LOGGING_CONFIG
    uvicorn_config["formatters"]["default"] = {
        "format": "[%(asctime)s] #%(levelname)s: %(message)s"
    }
    uvicorn_config["formatters"]["access"] = {
        "format": "[%(asctime)s] #%(levelname)s: %(message)s"
    }
    uvicorn_config["handlers"]["default"] = {
        "class": "logging.FileHandler",
        "formatter": "default",
        "filename": LOG_FILE,
        "mode": "a"
    }
    uvicorn_config["handlers"]["access"] = {
        "class": "logging.FileHandler",
        "formatter": "access",
        "filename": LOG_FILE,
        "mode": "a"
    }
    uv_cfg = uvicorn.Config(
        app=app,
        host="0.0.0.0",
        port=8080,
        log_level="debug",
        log_config=uvicorn_config,
        timeout_graceful_shutdown=2,
    )
    server = uvicorn.Server(config=uv_cfg)
    asyncio.run(server.serve())
