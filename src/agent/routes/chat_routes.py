# /agent/routes/chat_routes.py, updated 2025-07-17 22:15 EEST
from fastapi import APIRouter, Request, HTTPException
import logging
import time
import re
import os
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

@router.get("/chat/get")
async def get_chat(request: Request, chat_id: int, wait_changes: int = 0):
    db = Database.get_database()
    logging.debug(f"Запрос GET /chat/get, IP={request.client.host}, Cookies={request.cookies}, chat_id={chat_id}, wait_changes={wait_changes}")
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
        if wait_changes:
            changes = globals.post_manager.get_changes(chat_id)
            if not changes:
                logging.debug(f"Нет изменений для chat_id={chat_id}, ожидание")
                return {"chat_history": "no changes"}
        history = globals.post_manager.get_history(chat_id, wait_changes == 1)
        logging.debug(f"Возвращена история для chat_id={chat_id}, {len(history)} сообщений")
        return history
    except HTTPException as e:
        logging.error(f"HTTP ошибка в GET /chat/get: {str(e)}")
        raise
    except Exception as e:
        logging.error(f"Ошибка сервера в GET /chat/get: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Server error: {str(e)}")

@router.post("/chat/notify_switch")
async def notify_chat_switch(request: Request):
    db = Database.get_database()
    logging.debug(f"Запрос POST /chat/notify_switch, IP={request.client.host}, Cookies={request.cookies}")
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
        globals.post_manager.get_changes(chat_id)
        logging.debug(f"Уведомление о смене чата chat_id={chat_id} для user_id={user_id}")
        return {"chat_history": "chat switch"}
    except HTTPException as e:
        logging.error(f"HTTP ошибка в POST /chat/notify_switch: {str(e)}")
        raise
    except Exception as e:
        logging.error(f"Ошибка сервера в POST /chat/notify_switch: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Server error: {str(e)}")

@router.post("/chat/post")
async def post_message(request: Request):
    db = Database.get_database()
    logging.debug(f"Запрос POST /chat/post, IP={request.client.host}, Cookies={request.cookies}")
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
        message = data.get('message')
        if not chat_id or not message:
            logging.info(f"Неверные параметры chat_id={chat_id} или message для IP {request.client.host}")
            raise HTTPException(status_code=400, detail="Missing chat_id or message")
        result = globals.post_manager.add_message(chat_id, user_id, message)
        logging.debug(f"Добавлено сообщение для chat_id={chat_id}, user_id={user_id}")
        return result
    except HTTPException as e:
        logging.error(f"HTTP ошибка в POST /chat/post: {str(e)}")
        raise
    except Exception as e:
        logging.error(f"Ошибка сервера в POST /chat/post: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Server error: {str(e)}")

@router.post("/chat/edit_post")
async def edit_post(request: Request):
    db = Database.get_database()
    logging.debug(f"Запрос POST /chat/edit_post, IP={request.client.host}, Cookies={request.cookies}")
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
        post_id = data.get('post_id')
        message = data.get('message')
        if not post_id or not message:
            logging.info(f"Неверные параметры post_id={post_id} или message для IP {request.client.host}")
            raise HTTPException(status_code=400, detail="Missing post_id or message")
        result = globals.post_manager.edit_post(post_id, message, user_id)
        logging.debug(f"Отредактировано сообщение post_id={post_id} для user_id={user_id}")
        return result
    except HTTPException as e:
        logging.error(f"HTTP ошибка в POST /chat/edit_post: {str(e)}")
        raise
    except Exception as e:
        logging.error(f"Ошибка сервера в POST /chat/edit_post: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Server error: {str(e)}")

@router.post("/chat/delete_post")
async def delete_post(request: Request):
    db = Database.get_database()
    logging.debug(f"Запрос POST /chat/delete_post, IP={request.client.host}, Cookies={request.cookies}")
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
        post_id = data.get('post_id')
        if not post_id:
            logging.info(f"Неверный параметр post_id={post_id} для IP {request.client.host}")
            raise HTTPException(status_code=400, detail="Missing post_id")
        result = globals.post_manager.delete_post(post_id, user_id)
        logging.debug(f"Удалено сообщение post_id={post_id} от user_id={user_id}")
        return result
    except HTTPException as e:
        logging.error(f"HTTP ошибка в POST /chat/delete_post: {str(e)}")
        raise
    except Exception as e:
        logging.error(f"Ошибка сервера в POST /chat/delete_post: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Server error: {str(e)}")

@router.get("/chat/get_stats")
async def get_chat_stats(request: Request, chat_id: int):
    db = Database.get_database()
    logging.debug(f"Запрос GET /chat/get_stats, IP={request.client.host}, Cookies={request.cookies}, chat_id={chat_id}")
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
        chat = db.fetch_one(
            'SELECT chat_id FROM chats WHERE chat_id = :chat_id',
            {'chat_id': chat_id}
        )
        if not chat:
            logging.info(f"Чат chat_id={chat_id} не найден для user_id={user_id}")
            raise HTTPException(status_code=404, detail="Chat not found")
        stats = {
            "chat_id": chat_id,
            "tokens": globals.replication_manager.last_sent_tokens,
            "num_sources_used": globals.replication_manager.last_num_sources_used
        }
        logging.debug(f"Возвращены статистики для chat_id={chat_id}, user_id={user_id}: {stats}")
        return stats
    except HTTPException as e:
        logging.error(f"HTTP ошибка в GET /chat/get_stats: {str(e)}")
        raise
    except Exception as e:
        logging.error(f"Ошибка сервера в GET /chat/get_stats: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Server error: {str(e)}")

@router.get("/chat/get_parent_msg")
async def get_parent_msg(request: Request, post_id: int):
    db = Database.get_database()
    logging.debug(f"Запрос GET /chat/get_parent_msg, post_id={post_id}, IP={request.client.host}, Cookies={request.cookies}")
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
        msg = db.fetch_one(
            'SELECT id, chat_id, user_id, message, timestamp FROM posts WHERE id = :post_id',
            {'post_id': post_id}
        )
        if not msg:
            logging.info(f"Сообщение post_id={post_id} не найдено для user_id={user_id}")
            # Проверяем, есть ли чаты, ссылающиеся на это сообщение
            affected_chats = db.fetch_all(
                'SELECT chat_id, parent_msg_id FROM chats WHERE parent_msg_id = :post_id',
                {'post_id': post_id}
            )
            for chat_id, parent_msg_id in affected_chats:
                # Ищем ближайшее доступное сообщение в том же чате
                chat = db.fetch_one(
                    'SELECT chat_id FROM chats WHERE chat_id = :chat_id',
                    {'chat_id': chat_id}
                )
                if chat:
                    new_parent_msg = db.fetch_one(
                        'SELECT id FROM posts WHERE chat_id = :chat_id AND id < :post_id ORDER BY id DESC LIMIT 1',
                        {'chat_id': chat[0], 'post_id': post_id}
                    )
                    new_parent_msg_id = new_parent_msg[0] if new_parent_msg else None
                    db.execute(
                        'UPDATE chats SET parent_msg_id = :new_parent_msg_id WHERE chat_id = :chat_id',
                        {'new_parent_msg_id': new_parent_msg_id, 'chat_id': chat_id}
                    )
                    logging.debug(f"Обновлён parent_msg_id для chat_id={chat_id}: {parent_msg_id} -> {new_parent_msg_id}")
            # Возвращаем null, так как сообщение не найдено
            return None
        result = {
            "id": msg[0],
            "chat_id": msg[1],
            "user_id": msg[2],
            "message": msg[3],
            "timestamp": msg[4]
        }
        logging.debug(f"Возвращено сообщение post_id={post_id}: {result}")
        return result
    except HTTPException as e:
        logging.error(f"HTTP ошибка в GET /chat/get_parent_msg: {str(e)}")
        raise
    except Exception as e:
        logging.error(f"Ошибка сервера в GET /chat/get_parent_msg: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Server error: {str(e)}")

@router.get("/chat/logs")
async def get_logs(request: Request):
    db = Database.get_database()
    logging.debug(f"Запрос GET /chat/logs, IP={request.client.host}, Cookies={request.cookies}")
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
        log_file = "/app/logs/colloquium_core.log"
        logs = []
        if os.path.exists(log_file):
            with open(log_file, "r", encoding="utf-8") as f:
                lines = f.readlines()[-100:]  # Последние 100 строк
                for line in lines:
                    if "#ERROR" in line or "#WARNING" in line:
                        match = re.match(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) #(\w+): (.+)", line)
                        if match:
                            timestamp, level, message = match.groups()
                            logs.append({
                                "timestamp": int(time.mktime(time.strptime(timestamp, "%Y-%m-%d %H:%M:%S,%f"))),
                                "level": level,
                                "message": message
                            })
        logging.debug(f"Возвращено {len(logs)} логов ошибок/предупреждений для user_id={user_id}")
        return {"logs": logs}
    except HTTPException as e:
        logging.error(f"HTTP ошибка в GET /chat/logs: {str(e)}")
        raise
    except Exception as e:
        logging.error(f"Ошибка сервера в GET /chat/logs: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Server error: {str(e)}")