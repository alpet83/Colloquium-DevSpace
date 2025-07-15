# /agent/routes/post_routes.py, updated 2025-07-14 19:39 EEST
from fastapi import APIRouter, Request, HTTPException
import logging
import sys
import datetime

def log_msg(message, tag="#INFO"):
    now = datetime.datetime.now()
    ts = now.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}]. {tag}: {message}", file=sys.stderr)

try:
    from managers.db import Database
    log_msg("Успешно импортирован Database из managers.db", "#DEBUG")
except Exception as e:
    log_msg(f"Ошибка импорта Database: {str(e)}", "#ERROR")
    raise

import globals

router = APIRouter()
try:
    SESSION_DB = Database()
    log_msg("Успешно создан экземпляр SESSION_DB", "#DEBUG")
except Exception as e:
    log_msg(f"Ошибка создания SESSION_DB: {str(e)}", "#ERROR")
    raise

@router.get("/chat/test")
async def test(chat_id: int = 0):
    logging.info(f"#INFO: Тестовый маршрут chat/test с chat_id={chat_id}")
    return {"status": "PASSED", "chat_id": chat_id}

@router.get("/chat/get")
async def get_chat(chat_id: int, request: Request):
    logging.debug(f"#DEBUG: Запрос GET /chat/get с chat_id={chat_id}, IP={request.client.host}, Cookies={request.cookies}")
    try:
        if not isinstance(chat_id, int) or chat_id <= 0:
            logging.error(f"#ERROR: Некорректный chat_id={chat_id} для IP {request.client.host}")
            raise HTTPException(status_code=400, detail="Invalid chat_id")
        session_id = request.cookies.get("session_id")
        if not session_id:
            logging.info(f"#INFO: Отсутствует session_id для IP {request.client.host}")
            raise HTTPException(status_code=401, detail="No session")
        user_id = SESSION_DB.fetch_one(
            'SELECT user_id FROM sessions WHERE session_id = :session_id',
            {'session_id': session_id}
        )
        if not user_id:
            logging.info(f"#INFO: Неверный session_id={session_id} для IP {request.client.host}")
            raise HTTPException(status_code=401, detail="Invalid session")
        user_id = user_id[0]
        logging.debug(f"#DEBUG: Найден user_id={user_id} для session_id={session_id}")
        history = globals.post_manager.get_history(chat_id)
        logging.debug(f"#DEBUG: Возвращена история для chat_id={chat_id}: {len(history)} сообщений")
        return history
    except HTTPException as e:
        logging.error(f"#ERROR: HTTP ошибка в GET /chat/get для chat_id={chat_id}: {str(e)}")
        raise
    except Exception as e:
        logging.error(f"#ERROR: Ошибка сервера в GET /chat/get для chat_id={chat_id}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Server error: {str(e)}")

@router.post("/chat/post")
async def post_chat(request: Request):
    logging.debug(f"#DEBUG: Запрос POST /chat/post, IP={request.client.host}, Cookies={request.cookies}")
    try:
        session_id = request.cookies.get("session_id")
        if not session_id:
            logging.info(f"#INFO: Отсутствует session_id для IP {request.client.host}")
            raise HTTPException(status_code=401, detail="No session")
        user_id = SESSION_DB.fetch_one(
            'SELECT user_id FROM sessions WHERE session_id = :session_id',
            {'session_id': session_id}
        )
        if not user_id:
            logging.info(f"#INFO: Неверный session_id для IP {request.client.host}")
            raise HTTPException(status_code=401, detail="Invalid session")
        user_id = user_id[0]
        data = await request.json()
        chat_id = data.get('chat_id')
        message = data.get('message')
        if not chat_id or not message:
            logging.info(f"#INFO: Неверные параметры chat_id={chat_id}, message={message} для IP {request.client.host}")
            raise HTTPException(status_code=400, detail="Missing chat_id or message")
        result = globals.post_manager.add_message(chat_id, user_id, message)
        logging.debug(f"#DEBUG: Сообщение добавлено для chat_id={chat_id}, user_id={user_id}")
        return result
    except HTTPException as e:
        logging.error(f"#ERROR: HTTP ошибка в POST /chat/post: {str(e)}")
        raise
    except Exception as e:
        logging.error(f"#ERROR: Ошибка сервера в POST /chat/post: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Server error: {str(e)}")

@router.post("/chat/delete_post")
async def delete_post(request: Request):
    logging.debug(f"#DEBUG: Запрос POST /chat/delete_post, IP={request.client.host}, Cookies={request.cookies}")
    try:
        session_id = request.cookies.get("session_id")
        if not session_id:
            logging.info(f"#INFO: Отсутствует session_id для IP {request.client.host}")
            raise HTTPException(status_code=401, detail="No session")
        user_id = SESSION_DB.fetch_one(
            'SELECT user_id FROM sessions WHERE session_id = :session_id',
            {'session_id': session_id}
        )
        if not user_id:
            logging.info(f"#INFO: Неверный session_id для IP {request.client.host}")
            raise HTTPException(status_code=401, detail="Invalid session")
        user_id = user_id[0]
        data = await request.json()
        post_id = data.get('post_id')
        if not post_id:
            logging.info(f"#INFO: Неверный параметр post_id={post_id} для IP {request.client.host}")
            raise HTTPException(status_code=400, detail="Missing post_id")
        result = globals.post_manager.delete_post(post_id, user_id)
        logging.debug(f"#DEBUG: Сообщение post_id={post_id} удалено для user_id={user_id}")
        return result
    except HTTPException as e:
        logging.error(f"#ERROR: HTTP ошибка в POST /chat/delete_post: {str(e)}")
        raise
    except Exception as e:
        logging.error(f"#ERROR: Ошибка сервера в POST /chat/delete_post: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Server error: {str(e)}")
