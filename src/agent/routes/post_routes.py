# /agent/routes/post_routes.py, updated 2025-07-18 12:45 EEST
import asyncio
from fastapi import APIRouter, Request, HTTPException
from managers.db import Database
import globals
from lib.basic_logger import BasicLogger

router = APIRouter()
log = globals.get_logger("postman")

@router.get("/chat/get")
async def get_chat(request: Request, chat_id: int, wait_changes: int = 0):
    db = Database.get_database()
    try:
        session_id = request.cookies.get("session_id")
        if not session_id:
            log.info(f"Отсутствует session_id для IP {request.client.host}")
            raise HTTPException(status_code=401, detail="No session")
        user_id = db.fetch_one(
            'SELECT user_id FROM sessions WHERE session_id = :session_id',
            {'session_id': session_id}
        )
        if not user_id:
            log.info(f"Неверный session_id для IP {request.client.host}")
            raise HTTPException(status_code=401, detail="Invalid session")
        user_id = user_id[0]

        if wait_changes:
            # Ожидаем изменений с таймаутом 15 секунд
            switch_key = f"{user_id}:{chat_id}"
            if switch_key not in globals.chat_switch_events:
                globals.chat_switch_events[switch_key] = asyncio.Event()
            switch_event = globals.chat_switch_events[switch_key]
            for _ in range(15):  # Проверяем каждую секунду, всего 15 секунд
                history = globals.post_manager.get_history(chat_id, only_changes=True)
                if history != {"chat_history": "no changes"}:
                    return history
                if switch_event.is_set():
                    log.debug(f"Chat switch detected for user_id={user_id}, chat_id={chat_id}")
                    switch_event.clear()
                    return {"chat_history": "chat switch"}
                await asyncio.sleep(1)
            return {"chat_history": "no changes"}
        else:
            history = globals.post_manager.get_history(chat_id, only_changes=False)
            return history
    except HTTPException as e:
        log.error(f"HTTP ошибка в GET /chat/get: {str(e)}")
        raise
    except Exception as e:
        log.excpt(f"Ошибка сервера в GET /chat/get: {str(e)}", exc_info=(type(e), e, e.__traceback__))
        raise HTTPException(status_code=500, detail=f"Server error: {str(e)}")

@router.post("/chat/post")
async def add_message(request: Request):
    db = Database.get_database()
    log.debug(f"Запрос POST /chat/post, IP={request.client.host}, Cookies={request.cookies}")
    try:
        session_id = request.cookies.get("session_id")
        if not session_id:
            log.info(f"Отсутствует session_id для IP {request.client.host}")
            raise HTTPException(status_code=401, detail="No session")
        user_id = db.fetch_one(
            'SELECT user_id FROM sessions WHERE session_id = :session_id',
            {'session_id': session_id}
        )
        if not user_id:
            log.info(f"Неверный session_id для IP {request.client.host}")
            raise HTTPException(status_code=401, detail="Invalid session")
        user_id = user_id[0]
        data = await request.json()
        chat_id = data.get('chat_id')
        message = data.get('message')
        if not chat_id or not message:
            log.info(f"Неверные параметры chat_id={chat_id}, message={message} для IP {request.client.host}")
            raise HTTPException(status_code=400, detail="Missing chat_id or message")
        globals.post_manager.add_message(chat_id, user_id, message)
        log.debug(f"Добавлено сообщение в chat_id={chat_id} от user_id={user_id}: {message}")
        return {"status": "Message added"}
    except HTTPException as e:
        log.error(f"HTTP ошибка в POST /chat/post: {str(e)}")
        raise
    except Exception as e:
        log.excpt(f"Ошибка сервера в POST /chat/post: {str(e)}", exc_info=(type(e), e, e.__traceback__))
        raise HTTPException(status_code=500, detail=f"Server error: {str(e)}")

@router.post("/chat/edit_post")
async def edit_post(request: Request):
    db = Database.get_database()
    log.debug(f"Запрос POST /chat/edit_post, IP={request.client.host}, Cookies={request.cookies}")
    try:
        session_id = request.cookies.get("session_id")
        if not session_id:
            log.info(f"Отсутствует session_id для IP {request.client.host}")
            raise HTTPException(status_code=401, detail="No session")
        user_id = db.fetch_one(
            'SELECT user_id FROM sessions WHERE session_id = :session_id',
            {'session_id': session_id}
        )
        if not user_id:
            log.info(f"Неверный session_id для IP {request.client.host}")
            raise HTTPException(status_code=401, detail="Invalid session")
        user_id = user_id[0]
        data = await request.json()
        post_id = data.get('post_id')
        message = data.get('message')
        if not post_id or not message:
            log.info(f"Неверные параметры post_id={post_id}, message={message} для IP {request.client.host}")
            raise HTTPException(status_code=400, detail="Missing post_id or message")
        result = globals.post_manager.edit_post(post_id, message, user_id)
        if result.get("error"):
            raise HTTPException(status_code=403, detail=result["error"])
        log.debug(f"Отредактировано сообщение post_id={post_id} от user_id={user_id}")
        return {"status": "Post edited"}
    except HTTPException as e:
        log.error(f"HTTP ошибка в POST /chat/edit_post: {str(e)}")
        raise
    except Exception as e:
        log.excpt(f"Ошибка сервера в POST /chat/edit_post: {str(e)}", exc_info=(type(e), e, e.__traceback__))
        raise HTTPException(status_code=500, detail=f"Server error: {str(e)}")

@router.post("/chat/delete_post")
async def delete_post(request: Request):
    db = Database.get_database()
    log.debug(f"Запрос POST /chat/delete_post, IP={request.client.host}, Cookies={request.cookies}")
    try:
        session_id = request.cookies.get("session_id")
        if not session_id:
            log.info(f"Отсутствует session_id для IP {request.client.host}")
            raise HTTPException(status_code=401, detail="No session")
        user_id = db.fetch_one(
            'SELECT user_id FROM sessions WHERE session_id = :session_id',
            {'session_id': session_id}
        )
        if not user_id:
            log.info(f"Неверный session_id для IP {request.client.host}")
            raise HTTPException(status_code=401, detail="Invalid session")
        user_id = user_id[0]
        data = await request.json()
        post_id = data.get('post_id')
        if not post_id:
            log.info(f"Неверный параметр post_id={post_id} для IP {request.client.host}")
            raise HTTPException(status_code=400, detail="Missing post_id")
        result = globals.post_manager.delete_post(post_id, user_id)
        if result.get("error"):
            raise HTTPException(status_code=403, detail=result["error"])
        log.debug(f"Удалено сообщение post_id={post_id} от user_id={user_id}")
        return {"status": "Post deleted"}
    except HTTPException as e:
        log.error(f"HTTP ошибка в POST /chat/delete_post: {str(e)}")
        raise
    except Exception as e:
        log.excpt(f"Ошибка сервера в POST /chat/delete_post: {str(e)}", exc_info=(type(e), e, e.__traceback__))
        raise HTTPException(status_code=500, detail=f"Server error: {str(e)}")

@router.post("/chat/notify_switch")
async def notify_chat_switch(request: Request):
    db = Database.get_database()
    try:
        session_id = request.cookies.get("session_id")
        if not session_id:
            log.info(f"Отсутствует session_id для IP {request.client.host}")
            raise HTTPException(status_code=401, detail="No session")
        user_id = db.fetch_one(
            'SELECT user_id FROM sessions WHERE session_id = :session_id',
            {'session_id': session_id}
        )
        if not user_id:
            log.info(f"Неверный session_id для IP {request.client.host}")
            raise HTTPException(status_code=401, detail="Invalid session")
        user_id = user_id[0]
        data = await request.json()
        chat_id = data.get('chat_id')
        if not chat_id:
            log.info(f"Неверный параметр chat_id={chat_id} для IP {request.client.host}")
            raise HTTPException(status_code=400, detail="Missing chat_id")
        # Устанавливаем событие переключения чата
        switch_key = f"{user_id}:{chat_id}"
        if switch_key not in globals.chat_switch_events:
            globals.chat_switch_events[switch_key] = asyncio.Event()
        globals.chat_switch_events[switch_key].set()
        log.debug(f"Chat switch notified for user_id={user_id}, chat_id={chat_id}")
        return {"status": "Chat switch notified"}
    except HTTPException as e:
        log.error(f"HTTP ошибка в POST /chat/notify_switch: {str(e)}")
        raise
    except Exception as e:
        log.excpt(f"Ошибка сервера в POST /chat/notify_switch: {str(e)}", exc_info=(type(e), e, e.__traceback__))
        raise HTTPException(status_code=500, detail=f"Server error: {str(e)}")