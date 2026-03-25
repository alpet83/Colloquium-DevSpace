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
            log.info(globals.with_session_tag(request, "Отсутствует session_id для IP %s"), request.client.host)
            raise HTTPException(status_code=401, detail="No session")
        user_id = db.fetch_one(
            'SELECT user_id FROM sessions WHERE session_id = :session_id',
            {'session_id': session_id}
        )
        if not user_id:
            log.info(globals.with_session_tag(request, "Неверный session_id для IP %s"), request.client.host)
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
                    log.debug(globals.with_session_tag(request, "Chat switch detected for user_id=%d, chat_id=%d"), user_id, chat_id)
                    switch_event.clear()
                    return {"chat_history": "chat switch"}
                await asyncio.sleep(1)
            return {"chat_history": "no changes"}
        else:
            history = globals.post_manager.get_history(chat_id, only_changes=False)
            return history
    except HTTPException as e:
        log.error(globals.with_session_tag(request, "HTTP ошибка в GET /chat/get: %s"), str(e))
        raise
    except Exception as e:
        log.excpt(globals.with_session_tag(request, "Ошибка сервера в GET /chat/get: %s"), str(e), exc_info=(type(e), e, e.__traceback__))
        raise HTTPException(status_code=500, detail=f"Server error: {str(e)}")

@router.post("/chat/post")
async def add_message(request: Request):
    db = Database.get_database()
    log.debug(globals.with_session_tag(request, "Запрос POST /chat/post, IP=%s, Cookies=%s"), request.client.host, str(request.cookies))
    try:
        session_id = request.cookies.get("session_id")
        if not session_id:
            log.info(globals.with_session_tag(request, "Отсутствует session_id для IP %s"), request.client.host)
            raise HTTPException(status_code=401, detail="No session")
        user_id = db.fetch_one(
            'SELECT user_id FROM sessions WHERE session_id = :session_id',
            {'session_id': session_id}
        )
        if not user_id:
            log.info(globals.with_session_tag(request, "Неверный session_id для IP %s"), request.client.host)
            raise HTTPException(status_code=401, detail="Invalid session")
        user_id = user_id[0]
        data = await request.json()
        chat_id = data.get('chat_id')
        message = data.get('message')
        if not chat_id or not message:
            log.info(globals.with_session_tag(request, "Неверные параметры chat_id=%s, message=%s для IP %s"), str(chat_id), str(message), request.client.host)
            raise HTTPException(status_code=400, detail="Missing chat_id or message")
        globals.post_manager.add_message(chat_id, user_id, message)
        log.debug(globals.with_session_tag(request, "Добавлено сообщение в chat_id=%d от user_id=%d: %s"), chat_id, user_id, message)
        return {"status": "Message added"}
    except HTTPException as e:
        log.error(globals.with_session_tag(request, "HTTP ошибка в POST /chat/post: %s"), str(e))
        raise
    except Exception as e:
        log.excpt(globals.with_session_tag(request, "Ошибка сервера в POST /chat/post: %s"), str(e), exc_info=(type(e), e, e.__traceback__))
        raise HTTPException(status_code=500, detail=f"Server error: {str(e)}")

@router.post("/chat/edit_post")
async def edit_post(request: Request):
    db = Database.get_database()
    log.debug(globals.with_session_tag(request, "Запрос POST /chat/edit_post, IP=%s, Cookies=%s"), request.client.host, str(request.cookies))
    try:
        session_id = request.cookies.get("session_id")
        if not session_id:
            log.info(globals.with_session_tag(request, "Отсутствует session_id для IP %s"), request.client.host)
            raise HTTPException(status_code=401, detail="No session")
        user_id = db.fetch_one(
            'SELECT user_id FROM sessions WHERE session_id = :session_id',
            {'session_id': session_id}
        )
        if not user_id:
            log.info(globals.with_session_tag(request, "Неверный session_id для IP %s"), request.client.host)
            raise HTTPException(status_code=401, detail="Invalid session")
        user_id = user_id[0]
        data = await request.json()
        post_id = data.get('post_id')
        message = data.get('message')
        if not post_id or not message:
            log.info(globals.with_session_tag(request, "Неверные параметры post_id=%s, message=%s для IP %s"), str(post_id), str(message), request.client.host)
            raise HTTPException(status_code=400, detail="Missing post_id or message")
        result = globals.post_manager.edit_post(post_id, message, user_id)
        if result.get("error"):
            raise HTTPException(status_code=403, detail=result["error"])
        log.debug(globals.with_session_tag(request, "Отредактировано сообщение post_id=%d от user_id=%d"), post_id, user_id)
        return {"status": "Post edited"}
    except HTTPException as e:
        log.error(globals.with_session_tag(request, "HTTP ошибка в POST /chat/edit_post: %s"), str(e))
        raise
    except Exception as e:
        log.excpt(globals.with_session_tag(request, "Ошибка сервера в POST /chat/edit_post: %s"), str(e), exc_info=(type(e), e, e.__traceback__))
        raise HTTPException(status_code=500, detail=f"Server error: {str(e)}")

@router.post("/chat/delete_post")
async def delete_post(request: Request):
    db = Database.get_database()
    log.debug(globals.with_session_tag(request, "Запрос POST /chat/delete_post, IP=%s, Cookies=%s"), request.client.host, str(request.cookies))
    try:
        session_id = request.cookies.get("session_id")
        if not session_id:
            log.info(globals.with_session_tag(request, "Отсутствует session_id для IP %s"), request.client.host)
            raise HTTPException(status_code=401, detail="No session")
        user_id = db.fetch_one(
            'SELECT user_id FROM sessions WHERE session_id = :session_id',
            {'session_id': session_id}
        )
        if not user_id:
            log.info(globals.with_session_tag(request, "Неверный session_id для IP %s"), request.client.host)
            raise HTTPException(status_code=401, detail="Invalid session")
        user_id = user_id[0]
        data = await request.json()
        post_id = data.get('post_id')
        if not post_id:
            log.info(globals.with_session_tag(request, "Неверный параметр post_id=%s для IP %s"), str(post_id), request.client.host)
            raise HTTPException(status_code=400, detail="Missing post_id")
        result = globals.post_manager.delete_post(post_id, user_id)
        if result.get("error"):
            raise HTTPException(status_code=403, detail=result["error"])
        log.debug(globals.with_session_tag(request, "Удалено сообщение post_id=%d от user_id=%d"), post_id, user_id)
        return {"status": "Post deleted"}
    except HTTPException as e:
        log.error(globals.with_session_tag(request, "HTTP ошибка в POST /chat/delete_post: %s"), str(e))
        raise
    except Exception as e:
        log.excpt(globals.with_session_tag(request, "Ошибка сервера в POST /chat/delete_post: %s"), str(e), exc_info=(type(e), e, e.__traceback__))
        raise HTTPException(status_code=500, detail=f"Server error: {str(e)}")

@router.post("/chat/notify_switch")
async def notify_chat_switch(request: Request):
    db = Database.get_database()
    try:
        session_id = request.cookies.get("session_id")
        if not session_id:
            log.info(globals.with_session_tag(request, "Отсутствует session_id для IP %s"), request.client.host)
            raise HTTPException(status_code=401, detail="No session")
        user_id = db.fetch_one(
            'SELECT user_id FROM sessions WHERE session_id = :session_id',
            {'session_id': session_id}
        )
        if not user_id:
            log.info(globals.with_session_tag(request, "Неверный session_id для IP %s"), request.client.host)
            raise HTTPException(status_code=401, detail="Invalid session")
        user_id = user_id[0]
        data = await request.json()
        chat_id = data.get('chat_id')
        if not chat_id:
            log.info(globals.with_session_tag(request, "Неверный параметр chat_id=%s для IP %s"), str(chat_id), request.client.host)
            raise HTTPException(status_code=400, detail="Missing chat_id")
        # Устанавливаем событие переключения чата
        switch_key = f"{user_id}:{chat_id}"
        if switch_key not in globals.chat_switch_events:
            globals.chat_switch_events[switch_key] = asyncio.Event()
        globals.chat_switch_events[switch_key].set()
        log.debug(globals.with_session_tag(request, "Chat switch notified for user_id=%d, chat_id=%d"), user_id, chat_id)
        return {"status": "Chat switch notified"}
    except HTTPException as e:
        log.error(globals.with_session_tag(request, "HTTP ошибка в POST /chat/notify_switch: %s"), str(e))
        raise
    except Exception as e:
        log.excpt(globals.with_session_tag(request, "Ошибка сервера в POST /chat/notify_switch: %s"), str(e), exc_info=(type(e), e, e.__traceback__))
        raise HTTPException(status_code=500, detail=f"Server error: {str(e)}")