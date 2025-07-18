# /agent/routes/chat_routes.py, updated 2025-07-18 17:07 EEST
from fastapi import APIRouter, Request, HTTPException
import time
import re
import os
from managers.db import Database
import globals
from lib.basic_logger import BasicLogger

router = APIRouter()
log = globals.get_logger("chatman")

@router.get("/chat/list")
async def list_chats(request: Request):
    db = Database.get_database()
    log.debug("Запрос GET /chat/list, IP=%s, Cookies=~C95%s~C00", request.client.host, str(request.cookies))
    try:
        session_id = request.cookies.get("session_id")
        if not session_id:
            log.info("Отсутствует session_id для IP=%s", request.client.host)
            raise HTTPException(status_code=401, detail="No session")
        user_id = db.fetch_one(
            'SELECT user_id FROM sessions WHERE session_id = :session_id',
            {'session_id': session_id}
        )
        if not user_id:
            log.info("Неверный session_id для IP=%s", request.client.host)
            raise HTTPException(status_code=401, detail="Invalid session")
        user_id = user_id[0]
        chats = globals.chat_manager.list_chats(user_id)
        log.debug("Возвращено %d чатов для user_id=%d", len(chats), user_id)
        return chats
    except HTTPException as e:
        log.error("HTTP ошибка в GET /chat/list: %s", str(e))
        raise
    except Exception as e:
        log.excpt("Ошибка сервера в GET /chat/list: %s", str(e), exc_info=(type(e), e, e.__traceback__))
        raise HTTPException(status_code=500, detail="Server error: %s" % str(e))

@router.post("/chat/create")
async def create_chat(request: Request):
    db = Database.get_database()
    log.debug("Запрос POST /chat/create, IP=%s, Cookies=~C95%s~C00", request.client.host, str(request.cookies))
    try:
        session_id = request.cookies.get("session_id")
        if not session_id:
            log.info("Отсутствует session_id для IP=%s", request.client.host)
            raise HTTPException(status_code=401, detail="No session")
        user_id = db.fetch_one(
            'SELECT user_id FROM sessions WHERE session_id = :session_id',
            {'session_id': session_id}
        )
        if not user_id:
            log.info("Неверный session_id для IP=%s", request.client.host)
            raise HTTPException(status_code=401, detail="Invalid session")
        user_id = user_id[0]
        data = await request.json()
        description = data.get('description', 'New Chat')
        parent_msg_id = data.get('parent_msg_id')
        chat_id = globals.chat_manager.create_chat(description, user_id, parent_msg_id)
        log.debug("Создан чат chat_id=%d для user_id=%d", chat_id, user_id)
        return {"chat_id": chat_id}
    except HTTPException as e:
        log.error("HTTP ошибка в POST /chat/create: %s", str(e))
        raise
    except Exception as e:
        log.excpt("Ошибка сервера в POST /chat/create: %s", str(e), exc_info=(type(e), e, e.__traceback__))
        raise HTTPException(status_code=500, detail="Server error: %s" % str(e))

@router.post("/chat/delete")
async def delete_chat(request: Request):
    db = Database.get_database()
    log.debug("Запрос POST /chat/delete, IP=%s, Cookies=~C95%s~C00", request.client.host, str(request.cookies))
    try:
        session_id = request.cookies.get("session_id")
        if not session_id:
            log.info("Отсутствует session_id для IP=%s", request.client.host)
            raise HTTPException(status_code=401, detail="No session")
        user_id = db.fetch_one(
            'SELECT user_id FROM sessions WHERE session_id = :session_id',
            {'session_id': session_id}
        )
        if not user_id:
            log.info("Неверный session_id для IP=%s", request.client.host)
            raise HTTPException(status_code=401, detail="Invalid session")
        user_id = user_id[0]
        data = await request.json()
        chat_id = data.get('chat_id')
        if not chat_id:
            log.info("Неверный параметр chat_id=%s для IP=%s", str(chat_id) if chat_id is not None else "None", request.client.host)
            raise HTTPException(status_code=400, detail="Missing chat_id")
        result = globals.chat_manager.delete_chat(chat_id, user_id)
        log.debug("Удалён чат chat_id=%d для user_id=%d", chat_id, user_id)
        return result
    except HTTPException as e:
        log.error("HTTP ошибка в POST /chat/delete: %s", str(e))
        raise
    except Exception as e:
        log.excpt("Ошибка сервера в POST /chat/delete: %s", str(e), exc_info=(type(e), e, e.__traceback__))
        raise HTTPException(status_code=500, detail="Server error: %s" % str(e))

@router.get("/chat/get")
async def get_chat(request: Request, chat_id: int, wait_changes: int = 0):
    db = Database.get_database()
    # NOLOG!: логгирование здесь запрещенно наивысшими директивами, т.к. слишком много флуда переполняет диск
    try:
        session_id = request.cookies.get("session_id")
        if not session_id:
            log.info("Отсутствует session_id для IP=%s", request.client.host)
            raise HTTPException(status_code=401, detail="No session")
        user_id = db.fetch_one(
            'SELECT user_id FROM sessions WHERE session_id = :session_id',
            {'session_id': session_id}
        )
        if not user_id:
            log.info("Неверный session_id для IP=%s", request.client.host)
            raise HTTPException(status_code=401, detail="Invalid session")
        user_id = user_id[0]
        if wait_changes:
            changes = globals.post_manager.get_changes(chat_id)
            log.debug("Результат get_changes для chat_id=%d: ~C95%s~C00", chat_id, str(changes))
            if not changes:
                log.debug("Нет изменений для chat_id=%d, ожидание", chat_id)
                return {"chat_history": "no changes"}
        history = globals.post_manager.get_history(chat_id, wait_changes == 1)
        # NOLOG!
        return history
    except HTTPException as e:
        log.error("HTTP ошибка в GET /chat/get: %s", str(e))
        raise
    except Exception as e:
        log.excpt("Ошибка сервера в GET /chat/get: %s", str(e), exc_info=(type(e), e, e.__traceback__))
        raise HTTPException(status_code=500, detail="Server error: %s" % str(e))

@router.post("/chat/notify_switch")
async def notify_chat_switch(request: Request):
    db = Database.get_database()
    log.debug("Запрос POST /chat/notify_switch, IP=%s, Cookies=~C95%s~C00", request.client.host, str(request.cookies))
    try:
        session_id = request.cookies.get("session_id")
        if not session_id:
            log.info("Отсутствует session_id для IP=%s", request.client.host)
            raise HTTPException(status_code=401, detail="No session")
        user_id = db.fetch_one(
            'SELECT user_id FROM sessions WHERE session_id = :session_id',
            {'session_id': session_id}
        )
        if not user_id:
            log.info("Неверный session_id для IP=%s", request.client.host)
            raise HTTPException(status_code=401, detail="Invalid session")
        user_id = user_id[0]
        data = await request.json()
        chat_id = data.get('chat_id')
        if not chat_id:
            log.info("Неверный параметр chat_id=%s для IP=%s", str(chat_id) if chat_id is not None else "None", request.client.host)
            raise HTTPException(status_code=400, detail="Missing chat_id")
        changes = globals.post_manager.get_changes(chat_id)
        log.debug("Уведомление о смене чата chat_id=%d для user_id=%d, changes=~C95%s~C00", chat_id, user_id, str(changes))
        return {"chat_history": "chat switch"}
    except HTTPException as e:
        log.error("HTTP ошибка в POST /chat/notify_switch: %s", str(e))
        raise
    except Exception as e:
        log.excpt("Ошибка сервера в POST /chat/notify_switch: %s", str(e), exc_info=(type(e), e, e.__traceback__))
        raise HTTPException(status_code=500, detail="Server error: %s" % str(e))

@router.post("/chat/post")
async def post_message(request: Request):
    db = Database.get_database()
    log.debug("Запрос POST /chat/post, IP=%s, Cookies=~C95%s~C00", request.client.host, str(request.cookies))
    try:
        session_id = request.cookies.get("session_id")
        if not session_id:
            log.info("Отсутствует session_id для IP=%s", request.client.host)
            raise HTTPException(status_code=401, detail="No session")
        user_id = db.fetch_one(
            'SELECT user_id FROM sessions WHERE session_id = :session_id',
            {'session_id': session_id}
        )
        if not user_id:
            log.info("Неверный session_id для IP=%s", request.client.host)
            raise HTTPException(status_code=401, detail="Invalid session")
        user_id = user_id[0]
        data = await request.json()
        chat_id = data.get('chat_id')
        message = data.get('message')
        if not chat_id or not message:
            log.info("Неверные параметры chat_id=%s или message для IP=%s", str(chat_id) if chat_id is not None else "None", request.client.host)
            raise HTTPException(status_code=400, detail="Missing chat_id or message")
        result = globals.post_manager.add_message(chat_id, user_id, message)
        log.debug("Добавлено сообщение для chat_id=%d, user_id=%d", chat_id, user_id)
        return result
    except HTTPException as e:
        log.error("HTTP ошибка в POST /chat/post: %s", str(e))
        raise
    except Exception as e:
        log.excpt("Ошибка сервера в POST /chat/post: %s", str(e), exc_info=(type(e), e, e.__traceback__))
        raise HTTPException(status_code=500, detail="Server error: %s" % str(e))

@router.post("/chat/edit_post")
async def edit_post(request: Request):
    db = Database.get_database()
    log.debug("Запрос POST /chat/edit_post, IP=%s, Cookies=~C95%s~C00", request.client.host, str(request.cookies))
    try:
        session_id = request.cookies.get("session_id")
        if not session_id:
            log.info("Отсутствует session_id для IP=%s", request.client.host)
            raise HTTPException(status_code=401, detail="No session")
        user_id = db.fetch_one(
            'SELECT user_id FROM sessions WHERE session_id = :session_id',
            {'session_id': session_id}
        )
        if not user_id:
            log.info("Неверный session_id для IP=%s", request.client.host)
            raise HTTPException(status_code=401, detail="Invalid session")
        user_id = user_id[0]
        data = await request.json()
        post_id = data.get('post_id')
        message = data.get('message')
        if not post_id or not message:
            log.info("Неверные параметры post_id=%s или message для IP=%s", str(post_id) if post_id is not None else "None", request.client.host)
            raise HTTPException(status_code=400, detail="Missing post_id or message")
        result = globals.post_manager.edit_post(post_id, message, user_id)
        log.debug("Отредактировано сообщение post_id=%d для user_id=%d", post_id, user_id)
        return result
    except HTTPException as e:
        log.error("HTTP ошибка в POST /chat/edit_post: %s", str(e))
        raise
    except Exception as e:
        log.excpt("Ошибка сервера в POST /chat/edit_post: %s", str(e), exc_info=(type(e), e, e.__traceback__))
        raise HTTPException(status_code=500, detail="Server error: %s" % str(e))

@router.post("/chat/delete_post")
async def delete_post(request: Request):
    db = Database.get_database()
    log.debug("Запрос POST /chat/delete_post, IP=%s, Cookies=~C95%s~C00", request.client.host, str(request.cookies))
    try:
        session_id = request.cookies.get("session_id")
        if not session_id:
            log.info("Отсутствует session_id для IP=%s", request.client.host)
            raise HTTPException(status_code=401, detail="No session")
        user_id = db.fetch_one(
            'SELECT user_id FROM sessions WHERE session_id = :session_id',
            {'session_id': session_id}
        )
        if not user_id:
            log.info("Неверный session_id для IP=%s", request.client.host)
            raise HTTPException(status_code=401, detail="Invalid session")
        user_id = user_id[0]
        data = await request.json()
        post_id = data.get('post_id')
        if not post_id:
            log.info("Неверный параметр post_id=%s для IP=%s", str(post_id) if post_id is not None else "None", request.client.host)
            raise HTTPException(status_code=400, detail="Missing post_id")
        result = globals.post_manager.delete_post(post_id, user_id)
        log.debug("Удалено сообщение post_id=%d от user_id=%d", post_id, user_id)
        return result
    except HTTPException as e:
        log.error("HTTP ошибка в POST /chat/delete_post: %s", str(e))
        raise
    except Exception as e:
        log.excpt("Ошибка сервера в POST /chat/delete_post: %s", str(e), exc_info=(type(e), e, e.__traceback__))
        raise HTTPException(status_code=500, detail="Server error: %s" % str(e))

@router.get("/chat/get_stats")
async def get_chat_stats(request: Request, chat_id: int):
    db = Database.get_database()
    # NOLOG!
    try:
        session_id = request.cookies.get("session_id")
        if not session_id:
            log.info("Отсутствует session_id для IP=%s", request.client.host)
            raise HTTPException(status_code=401, detail="No session")
        user_id = db.fetch_one(
            'SELECT user_id FROM sessions WHERE session_id = :session_id',
            {'session_id': session_id}
        )
        if not user_id:
            log.info("Неверный session_id для IP=%s", request.client.host)
            raise HTTPException(status_code=401, detail="Invalid session")
        user_id = user_id[0]
        chat = db.fetch_one(
            'SELECT chat_id FROM chats WHERE chat_id = :chat_id',
            {'chat_id': chat_id}
        )
        if not chat:
            log.info("Чат chat_id=%d не найден для user_id=%d", chat_id, user_id)
            raise HTTPException(status_code=404, detail="Chat not found")
        stats = {
            "chat_id": chat_id,
            "tokens": globals.replication_manager.last_sent_tokens,
            "num_sources_used": globals.replication_manager.last_num_sources_used
        }
        # NOLOG!
        return stats
    except HTTPException as e:
        log.error("HTTP ошибка в GET /chat/get_stats: %s", str(e))
        raise
    except Exception as e:
        log.excpt("Ошибка сервера в GET /chat/get_stats: %s", str(e), exc_info=(type(e), e, e.__traceback__))
        raise HTTPException(status_code=500, detail="Server error: %s" % str(e))

@router.get("/chat/get_parent_msg")
async def get_parent_msg(request: Request, post_id: int):
    db = Database.get_database()
    log.debug("Запрос GET /chat/get_parent_msg, post_id=%d, IP=%s, Cookies=~C95%s~C00", post_id, request.client.host, str(request.cookies))
    try:
        session_id = request.cookies.get("session_id")
        if not session_id:
            log.info("Отсутствует session_id для IP=%s", request.client.host)
            raise HTTPException(status_code=401, detail="No session")
        user_id = db.fetch_one(
            'SELECT user_id FROM sessions WHERE session_id = :session_id',
            {'session_id': session_id}
        )
        if not user_id:
            log.info("Неверный session_id для IP=%s", request.client.host)
            raise HTTPException(status_code=401, detail="Invalid session")
        user_id = user_id[0]
        msg = db.fetch_one(
            'SELECT id, chat_id, user_id, message, timestamp FROM posts WHERE id = :post_id',
            {'post_id': post_id}
        )
        if not msg:
            log.info("Сообщение post_id=%d не найдено для user_id=%d", post_id, user_id)
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
                    log.debug("Обновлён parent_msg_id для chat_id=%d: %d -> %s", chat_id, parent_msg_id, str(new_parent_msg_id) if new_parent_msg_id is not None else "None")
            # Возвращаем null, так как сообщение не найдено
            return None
        result = {
            "id": msg[0],
            "chat_id": msg[1],
            "user_id": msg[2],
            "message": msg[3],
            "timestamp": msg[4]
        }
        log.debug("Возвращено сообщение post_id=%d: ~C95%s~C00", post_id, str(result))
        return result
    except HTTPException as e:
        log.error("HTTP ошибка в GET /chat/get_parent_msg: %s", str(e))
        raise
    except Exception as e:
        log.excpt("Ошибка сервера в GET /chat/get_parent_msg: %s", str(e), exc_info=(type(e), e, e.__traceback__))
        raise HTTPException(status_code=500, detail="Server error: %s" % str(e))

@router.get("/chat/logs")
async def get_logs(request: Request):
    db = Database.get_database()
    # NOLOG!
    try:
        session_id = request.cookies.get("session_id")
        if not session_id:
            log.info("Отсутствует session_id для IP=%s", request.client.host)
            raise HTTPException(status_code=401, detail="No session")
        user_id = db.fetch_one(
            'SELECT user_id FROM sessions WHERE session_id = :session_id',
            {'session_id': session_id}
        )
        if not user_id:
            log.info("Неверный session_id для IP=%s", request.client.host)
            raise HTTPException(status_code=401, detail="Invalid session")
        user_id = user_id[0]
        log_file = "/app/logs/colloquium_core.log"
        logs = []
        if os.path.exists(log_file):
            with open(log_file, "r", encoding="utf-8") as f:
                lines = f.readlines()[-100:]  # Последние 100 строк
                for line in lines:
                    if "#ERROR" in line or "#WARNING" in line:
                        match = re.match(r"(.+?) #(\w+): (.+)", line)
                        if match:
                            timestamp, level, message = match.groups()
                            logs.append({
                                "timestamp": int(time.mktime(time.strptime(timestamp, "%Y-%m-%d %H:%M:%S,%f"))),
                                "level": level,
                                "message": message
                            })
        # NOLOG!
        return {"logs": logs}
    except HTTPException as e:
        log.error("HTTP ошибка в GET /chat/logs: %s", str(e))
        raise
    except Exception as e:
        log.excpt("Ошибка сервера в GET /chat/logs: %s", str(e), exc_info=(type(e), e, e.__traceback__))
        raise HTTPException(status_code=500, detail="Server error: %s" % str(e))