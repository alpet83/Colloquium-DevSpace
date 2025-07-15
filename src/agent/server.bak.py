from fastapi import FastAPI, Request, Response, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
import asyncio
import logging
import socket
import os
import signal
import json
from multichat import MultiChat
import toml
import uvicorn
from logging import FileHandler
import sqlite3
import uuid

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://vps.vpn:8008", "http://localhost:8008"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "Cookie", "Set-Cookie", "Accept"],
    expose_headers=["Set-Cookie"]
)

LOG_FILE = "/app/logs/colloqium_core.log"
log_dir = "/app/logs"
if not os.path.exists(log_dir):
    os.makedirs(log_dir)

logging.basicConfig(level=logging.INFO, format='[%(asctime)s] #%(levelname)s: %(message)s', filename=LOG_FILE, filemode='a')

CONFIG_PATH = "/app/data/colloqium_config.toml"
SESSION_DB = "/app/data/sessions.db"

conn = sqlite3.connect(SESSION_DB)
conn.execute('CREATE TABLE IF NOT EXISTS sessions (session_id TEXT PRIMARY KEY, user_id INTEGER, chat_id INTEGER)')
conn.commit()

chat_manager = MultiChat()
shutdown_event = asyncio.Event()

async def lifespan(app: FastAPI):
    config = toml.load(CONFIG_PATH)
    local_ip = socket.gethostbyname(socket.gethostname())
    logging.info(f"#INFO: Ядро запущено на IP {local_ip}:8080")
    asyncio.create_task(chat_loop())
    yield
    logging.info("#INFO: Ядро остановлено")
    conn.close()

app.lifespan = lifespan

async def chat_loop():
    while not shutdown_event.is_set():
        history = chat_manager.get_history(chat_id=1, limit=1)
        if history:
            last_timestamp = history[0]["timestamp"]
            logging.debug(f"#DEBUG: Чат проверен, последнее: #post_{last_timestamp}")
        await asyncio.sleep(5)

@app.middleware("http")
async def log_requests(request: Request, call_next):
    logging.info(f"#INFO: Входящий запрос с IP {request.client.host}: {request.method} {request.url}, Cookies: {request.cookies}")
    response = await call_next(request)
    return response

@app.get("/chat/list")
async def list_chats(request: Request):
    session_id = request.cookies.get("session_id")
    if not session_id:
        logging.info(f"#INFO: Отсутствует session_id для IP {request.client.host}")
        return {"error": "No session"}
    cur = conn.cursor()
    cur.execute('SELECT user_id FROM sessions WHERE session_id = ?', (session_id,))
    user_id = cur.fetchone()
    if not user_id:
        logging.info(f"#INFO: Неверный session_id для IP {request.client.host}")
        return {"error": "Invalid session"}
    user_id = user_id[0]
    chats = chat_manager.list_chats(user_id)
    return chats

@app.post("/chat/create")
async def create_chat(request: Request):
    session_id = request.cookies.get("session_id")
    if not session_id:
        logging.info(f"#INFO: Отсутствует session_id для IP {request.client.host}")
        return {"error": "No session"}
    cur = conn.cursor()
    cur.execute('SELECT user_id FROM sessions WHERE session_id = ?', (session_id,))
    user_id = cur.fetchone()
    if not user_id:
        logging.info(f"#INFO: Неверный session_id для IP {request.client.host}")
        return {"error": "Invalid session"}
    user_id = user_id[0]
    data = await request.json()
    description = data.get('description', 'New Chat')
    parent_msg_id = data.get('parent_msg_id')
    chat_id = chat_manager.create_chat(description, user_id, parent_msg_id)
    cur.execute('UPDATE sessions SET chat_id = ? WHERE session_id = ?', (chat_id, session_id))
    conn.commit()
    return {"chat_id": chat_id}

@app.get("/chat/get")
async def get_chat(chat_id: int, request: Request):
    session_id = request.cookies.get("session_id")
    if not session_id:
        logging.info(f"#INFO: Отсутствует session_id для IP {request.client.host}")
        return {"error": "No session"}
    history = chat_manager.get_history(chat_id=chat_id)
    return history

@app.post("/chat/post")
async def post_chat(request: Request):
    session_id = request.cookies.get("session_id")
    if not session_id:
        logging.info(f"#INFO: Отсутствует session_id для IP {request.client.host}")
        return {"error": "No session"}
    cur = conn.cursor()
    cur.execute('SELECT user_id FROM sessions WHERE session_id = ?', (session_id,))
    user_id = cur.fetchone()
    if not user_id:
        logging.info(f"#INFO: Неверный session_id для IP {request.client.host}")
        return {"error": "Invalid session"}
    user_id = user_id[0]
    data = await request.json()
    chat_id = data.get('chat_id')
    message = data.get('message')
    if not chat_id or not message:
        logging.info(f"#INFO: Неверные параметры chat_id={chat_id}, message={message} для IP {request.client.host}")
        return {"error": "Missing chat_id or message"}
    chat_manager.add_message(chat_id, user_id, message)
    return {"status": "ok"}

@app.post("/chat/delete_post")
async def delete_post(request: Request):
    session_id = request.cookies.get("session_id")
    if not session_id:
        logging.info(f"#INFO: Отсутствует session_id для IP {request.client.host}")
        return {"error": "No session"}
    cur = conn.cursor()
    cur.execute('SELECT user_id FROM sessions WHERE session_id = ?', (session_id,))
    user_id = cur.fetchone()
    if not user_id:
        logging.info(f"#INFO: Неверный session_id для IP {request.client.host}")
        return {"error": "Invalid session"}
    user_id = user_id[0]
    data = await request.json()
    post_id = data.get('post_id')
    if not post_id:
        logging.info(f"#INFO: Неверный параметр post_id={post_id} для IP {request.client.host}")
        return {"error": "Missing post_id"}
    return chat_manager.delete_post(post_id, user_id)

@app.post("/chat/delete")
async def delete_chat(request: Request):
    session_id = request.cookies.get("session_id")
    if not session_id:
        logging.info(f"#INFO: Отсутствует session_id для IP {request.client.host}")
        return {"error": "No session"}
    cur = conn.cursor()
    cur.execute('SELECT user_id FROM sessions WHERE session_id = ?', (session_id,))
    user_id = cur.fetchone()
    if not user_id:
        logging.info(f"#INFO: Неверный session_id для IP {request.client.host}")
        return {"error": "Invalid session"}
    user_id = user_id[0]
    data = await request.json()
    chat_id = data.get('chat_id')
    if not chat_id:
        logging.info(f"#INFO: Неверный параметр chat_id={chat_id} для IP {request.client.host}")
        return {"error": "Missing chat_id"}
    return chat_manager.delete_chat(chat_id, user_id)

@app.post("/chat/upload_file")
async def upload_file(request: Request, file: UploadFile = File(...), chat_id: int = Form(...), file_name: str = Form(...)):
    session_id = request.cookies.get("session_id")
    if not session_id:
        logging.info(f"#INFO: Отсутствует session_id для IP {request.client.host}")
        return {"error": "No session"}
    cur = conn.cursor()
    cur.execute('SELECT user_id FROM sessions WHERE session_id = ?', (session_id,))
    user_id = cur.fetchone()
    if not user_id:
        logging.info(f"#INFO: Неверный session_id для IP {request.client.host}")
        return {"error": "Invalid session"}
    user_id = user_id[0]
    content = await file.read()
    return chat_manager.upload_file(chat_id, user_id, content, file_name)

@app.post("/chat/update_file")
async def update_file(request: Request, file: UploadFile = File(...), file_id: int = Form(...), file_name: str = Form(...)):
    session_id = request.cookies.get("session_id")
    if not session_id:
        logging.info(f"#INFO: Отсутствует session_id для IP {request.client.host}")
        return {"error": "No session"}
    cur = conn.cursor()
    cur.execute('SELECT user_id FROM sessions WHERE session_id = ?', (session_id,))
    user_id = cur.fetchone()
    if not user_id:
        logging.info(f"#INFO: Неверный session_id для IP {request.client.host}")
        return {"error": "Invalid session"}
    user_id = user_id[0]
    content = await file.read()
    return chat_manager.update_file(file_id, user_id, content, file_name)

@app.post("/chat/delete_file")
async def delete_file(request: Request):
    session_id = request.cookies.get("session_id")
    if not session_id:
        logging.info(f"#INFO: Отсутствует session_id для IP {request.client.host}")
        return {"error": "No session"}
    cur = conn.cursor()
    cur.execute('SELECT user_id FROM sessions WHERE session_id = ?', (session_id,))
    user_id = cur.fetchone()
    if not user_id:
        logging.info(f"#INFO: Неверный session_id для IP {request.client.host}")
        return {"error": "Invalid session"}
    user_id = user_id[0]
    data = await request.json()
    file_id = data.get('file_id')
    if not file_id:
        logging.info(f"#INFO: Неверный параметр file_id={file_id} для IP {request.client.host}")
        return {"error": "Missing file_id"}
    return chat_manager.delete_file(file_id, user_id)

@app.get("/chat/list_files")
async def list_files(request: Request):
    session_id = request.cookies.get("session_id")
    if not session_id:
        logging.info(f"#INFO: Отсутствует session_id для IP {request.client.host}")
        return {"error": "No session"}
    cur = conn.cursor()
    cur.execute('SELECT user_id FROM sessions WHERE session_id = ?', (session_id,))
    user_id = cur.fetchone()
    if not user_id:
        logging.info(f"#INFO: Неверный session_id для IP {request.client.host}")
        return {"error": "Invalid session"}
    user_id = user_id[0]
    return chat_manager.list_files(user_id)

@app.get("/chat/get_sandwiches_index")
async def get_sandwiches_index(request: Request):
    session_id = request.cookies.get("session_id")
    if not session_id:
        logging.info(f"#INFO: Отсутствует session_id для IP {request.client.host}")
        return {"error": "No session"}
    cur = conn.cursor()
    cur.execute('SELECT user_id FROM sessions WHERE session_id = ?', (session_id,))
    user_id = cur.fetchone()
    if not user_id:
        logging.info(f"#INFO: Неверный session_id для IP {request.client.host}")
        return {"error": "Invalid session"}
    user_id = user_id[0]
    return chat_manager.get_sandwiches_index()

@app.post("/login")
async def login(request: Request):
    data = await request.json()
    ip = request.client.host
    username = data.get('username')
    password = data.get('password')
    logging.info(f"#INFO: Попытка логина с IP {ip}: username={username}")
    user_id = chat_manager.check_auth(username, password)
    if not user_id:
        return {"error": "Invalid username or password"}
    session_id = str(uuid.uuid4())
    cur = conn.cursor()
    cur.execute('INSERT INTO sessions (session_id, user_id, chat_id) VALUES (?, ?, ?)', (session_id, user_id, 1))
    conn.commit()
    response = Response(content=json.dumps({"status": "ok"}))
    response.set_cookie(key="session_id", value=session_id, samesite="Lax", secure=False)
    logging.info(f"#INFO: Успешный логин: username={username}, session_id={session_id}")
    return response

@app.post("/logout")
async def logout(request: Request):
    session_id = request.cookies.get("session_id")
    ip = request.client.host
    logging.info(f"#INFO: Попытка выхода с IP {ip}: session_id={session_id}")
    if not session_id:
        return {"error": "No session"}
    cur = conn.cursor()
    cur.execute('DELETE FROM sessions WHERE session_id = ?', (session_id,))
    conn.commit()
    response = Response(content=json.dumps({"status": "ok"}))
    response.delete_cookie("session_id")
    logging.info(f"#INFO: Успешный выход: session_id={session_id}")
    return response

async def shutdown():
    logging.info("#INFO: Получен сигнал завершения, инициируется graceful shutdown")
    shutdown_event.set()
    conn.close()
    await server.shutdown()

def handle_shutdown(signum, frame):
    logging.info(f"#INFO: Получен сигнал {signum}, завершение работы")
    asyncio.create_task(shutdown())

signal.signal(signal.SIGTERM, handle_shutdown)
signal.signal(signal.SIGINT, handle_shutdown)
signal.signal(signal.SIGQUIT, handle_shutdown)

if __name__ == "__main__":
    uvicorn_config = uvicorn.config.LOGGING_CONFIG
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
