# /agent/routes/chat_routes.py, updated 2025-07-16 16:00 EEST
from fastapi import APIRouter, Request, HTTPException
import logging
from managers.db import Database
import globals

router = APIRouter()

@router.get("/chat/list")
async def list_chats(request: Request):
    db = Database.get_database()
    logging.debug(f"Запрос GET /chat/list, IP={request.client.host}, Cookies={request.cookies}")
    try:
        session_id = request.cookies.get("session_id")
        if not session_id:
            logging.info(f"Отсутствует session_id для IP {request.client.host}")
            raise HTTPException(status_code=401, detail="No session")
        user_id = db.fetch_one(
            'SELECT user_id FROM sessions WHERE session_id = :session_id',
            {'session_id': session_id}
        )
        if not user_id:
            logging.info(f"Неверный session_id для IP {request.client.host}")
            raise HTTPException(status_code=401, detail="Invalid session")
        user_id = user_id[0]
        chats = globals.chat_manager.list_chats(user_id)
        logging.debug(f"Возвращено {len(chats)} чатов для user_id={user_id}")
        return chats
    except HTTPException as e:
        logging.error(f"HTTP ошибка в GET /chat/list: {str(e)}")
        raise
    except Exception as e:
        logging.error(f"Ошибка сервера в GET /chat/list: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Server error: {str(e)}")

@router.post("/chat/create")
async def create_chat(request: Request):
    db = Database.get_database()
    logging.debug(f"Запрос POST /chat/create, IP={request.client.host}, Cookies={request.cookies}")
    try:
        session_id = request.cookies.get("session_id")
        if not session_id:
            logging.info(f"Отсутствует session_id для IP {request.client.host}")
            raise HTTPException(status_code=401, detail="No session")
        user_id = db.fetch_one(
            'SELECT user_id FROM sessions WHERE session_id = :session_id',
            {'session_id': session_id}
        )
        if not user_id:
            logging.info(f"Неверный session_id для IP {request.client.host}")
            raise HTTPException(status_code=401, detail="Invalid session")
        user_id = user_id[0]
        data = await request.json()
        description = data.get('description', 'New Chat')
        parent_msg_id = data.get('parent_msg_id')
        chat_id = globals.chat_manager.create_chat(description, user_id, parent_msg_id)
        logging.debug(f"Создан чат chat_id={chat_id} для user_id={user_id}")
        return {"chat_id": chat_id}
    except HTTPException as e:
        logging.error(f"HTTP ошибка в POST /chat/create: {str(e)}")
        raise
    except Exception as e:
        logging.error(f"Ошибка сервера в POST /chat/create: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Server error: {str(e)}")

@router.post("/chat/delete")
async def delete_chat(request: Request):
    db = Database.get_database()
    logging.debug(f"Запрос POST /chat/delete, IP={request.client.host}, Cookies={request.cookies}")
    try:
        session_id = request.cookies.get("session_id")
        if not session_id:
            logging.info(f"Отсутствует session_id для IP {request.client.host}")
            raise HTTPException(status_code=401, detail="No session")
        user_id = db.fetch_one(
            'SELECT user_id FROM sessions WHERE session_id = :session_id',
            {'session_id': session_id}
        )
        if not user_id:
            logging.info(f"Неверный session_id для IP {request.client.host}")
            raise HTTPException(status_code=401, detail="Invalid session")
        user_id = user_id[0]
        data = await request.json()
        chat_id = data.get('chat_id')
        if not chat_id:
            logging.info(f"Неверный параметр chat_id={chat_id} для IP {request.client.host}")
            raise HTTPException(status_code=400, detail="Missing chat_id")
        result = globals.chat_manager.delete_chat(chat_id, user_id)
        logging.debug(f"Удалён чат chat_id={chat_id} для user_id={user_id}")
        return result
    except HTTPException as e:
        logging.error(f"HTTP ошибка в POST /chat/delete: {str(e)}")
        raise
    except Exception as e:
        logging.error(f"Ошибка сервера в POST /chat/delete: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Server error: {str(e)}")

@router.get("/chat/get_stats")
async def get_chat_stats(request: Request, chat_id: int):
    db = Database.get_database()
    logging.debug(f"Запрос GET /chat/get_stats, IP={request.client.host}, chat_id={chat_id}, Cookies={request.cookies}")
    try:
        session_id = request.cookies.get("session_id")
        if not session_id:
            logging.info(f"Отсутствует session_id для IP {request.client.host}")
            raise HTTPException(status_code=401, detail="No session")
        user_id = db.fetch_one(
            'SELECT user_id FROM sessions WHERE session_id = :session_id',
            {'session_id': session_id}
        )
        if not user_id:
            logging.info(f"Неверный session_id для IP {request.client.host}")
            raise HTTPException(status_code=401, detail="Invalid session")
        user_id = user_id[0]
        # Проверяем, что чат существует
        chat = db.fetch_one(
            'SELECT chat_id FROM chats WHERE chat_id = :chat_id',
            {'chat_id': chat_id}
        )
        if not chat:
            logging.info(f"Чат chat_id={chat_id} не найден для user_id={user_id}")
            raise HTTPException(status_code=404, detail="Chat not found")
        stats = {
            "chat_id": chat_id,
            "tokens": globals.replication_manager.last_sent_tokens
        }
        logging.debug(f"Возвращена статистика для chat_id={chat_id}: {stats}")
        return stats
    except HTTPException as e:
        logging.error(f"HTTP ошибка в GET /chat/get_stats: {str(e)}")
        raise
    except Exception as e:
        logging.error(f"Ошибка сервера в GET /chat/get_stats: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Server error: {str(e)}")