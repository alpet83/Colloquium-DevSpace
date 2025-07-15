# /agent/routes/chat_routes.py, updated 2025-07-14 19:12 EEST
from fastapi import APIRouter, Request, HTTPException
import logging
from managers.db import Database
import globals

router = APIRouter()
SESSION_DB = Database()

@router.get("/chat/list")
async def list_chats(request: Request):
    logging.debug(f"#DEBUG: Запрос GET /chat/list, IP={request.client.host}, Cookies={request.cookies}")
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
        chats = globals.chat_manager.list_chats(user_id)
        logging.debug(f"#DEBUG: Возвращено {len(chats)} чатов для user_id={user_id}")
        return chats
    except HTTPException as e:
        logging.error(f"#ERROR: HTTP ошибка в GET /chat/list: {str(e)}")
        raise
    except Exception as e:
        logging.error(f"#ERROR: Ошибка сервера в GET /chat/list: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Server error: {str(e)}")

@router.post("/chat/create")
async def create_chat(request: Request):
    logging.debug(f"#DEBUG: Запрос POST /chat/create, IP={request.client.host}, Cookies={request.cookies}")
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
        description = data.get('description', 'New Chat')
        parent_msg_id = data.get('parent_msg_id')
        chat_id = globals.chat_manager.create_chat(description, user_id, parent_msg_id)
        logging.debug(f"#DEBUG: Создан чат chat_id={chat_id} для user_id={user_id}")
        return {"chat_id": chat_id}
    except HTTPException as e:
        logging.error(f"#ERROR: HTTP ошибка в POST /chat/create: {str(e)}")
        raise
    except Exception as e:
        logging.error(f"#ERROR: Ошибка сервера в POST /chat/create: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Server error: {str(e)}")

@router.post("/chat/delete")
async def delete_chat(request: Request):
    logging.debug(f"#DEBUG: Запрос POST /chat/delete, IP={request.client.host}, Cookies={request.cookies}")
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
        if not chat_id:
            logging.info(f"#INFO: Неверный параметр chat_id={chat_id} для IP {request.client.host}")
            raise HTTPException(status_code=400, detail="Missing chat_id")
        result = globals.chat_manager.delete_chat(chat_id, user_id)
        logging.debug(f"#DEBUG: Удалён чат chat_id={chat_id} для user_id={user_id}")
        return result
    except HTTPException as e:
        logging.error(f"#ERROR: HTTP ошибка в POST /chat/delete: {str(e)}")
        raise
    except Exception as e:
        logging.error(f"#ERROR: Ошибка сервера в POST /chat/delete: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Server error: {str(e)}")
