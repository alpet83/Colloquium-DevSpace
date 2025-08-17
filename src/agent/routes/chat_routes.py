# /app/agent/routes/chat_routes.py, updated 2025-07-26 15:15 EEST
import math

from fastapi import APIRouter, Request, HTTPException
import asyncio
import time
import re
import os
from managers.db import Database
import globals as g
from globals import check_session, handle_exception

router = APIRouter()
log = g.get_logger("chatman")

@router.get("/chat/list")
async def list_chats(request: Request):
    log.debug("Запрос GET /chat/list, IP=%s, Cookies=~%s", request.client.host, str(request.cookies))
    try:
        user_id = check_session(request)
        chats = g.chat_manager.list_chats(user_id)
        log.debug("Возвращено %d чатов для user_id=%d", len(chats), user_id)
        return chats
    except Exception as e:
        handle_exception("Ошибка в GET /chat/list", e)
        raise

@router.post("/chat/create")
async def create_chat(request: Request):
    log.debug("Запрос POST /chat/create, IP=%s, Cookies=~%s", request.client.host, str(request.cookies))
    try:
        user_id = check_session(request)
        data = await request.json()
        description = data.get('description', 'New Chat')
        parent_msg_id = data.get('parent_msg_id')
        chat_id = g.chat_manager.create_chat(description, user_id, parent_msg_id)
        log.debug("Создан чат chat_id=%d для user_id=%d", chat_id, user_id)
        return {"chat_id": chat_id}
    except Exception as e:
        handle_exception("Ошибка в POST /chat/create", e)
        raise

@router.post("/chat/delete")
async def delete_chat(request: Request):
    log.debug("Запрос POST /chat/delete, IP=%s, Cookies=~%s", request.client.host, str(request.cookies))
    try:
        user_id = check_session(request)
        data = await request.json()
        chat_id = data.get('chat_id')
        if not chat_id:
            log.info("Неверный параметр chat_id=%s для IP=%s", str(chat_id) if chat_id is not None else "None", request.client.host)
            raise HTTPException(status_code=400, detail="Missing chat_id")
        result = g.chat_manager.delete_chat(chat_id, user_id)
        log.debug("Удалён чат chat_id=%d для user_id=%d", chat_id, user_id)
        return result
    except Exception as e:
        handle_exception("Ошибка в POST /chat/delete", e)
        raise

@router.get("/chat/get")
async def get_chat(request: Request, chat_id: int, wait_changes: int = 0):
    try:
        # NOLOG!: постоянное логирование запрещено из-за флуда
        user_id = check_session(request)
        status = {'status': "nope"}
        if wait_changes:
            max_wait = 15
            _elps = 0
            _loops = 0
            _start = time.time()
            while _elps < max_wait:
                _elps = time.time() - _start
                _loops += 1
                status = g.chat_manager.chat_status(chat_id)
                if status['status'] == 'busy' and max_wait > 1:
                    log.debug(" Ожидание сокращено, поскольку чат занят пользователем %s ", status['actor'])
                    max_wait = 1
                active = g.chat_manager.active_chat(user_id)
                history = g.post_manager.get_history(chat_id, wait_changes == 1)
                if history != {"chat_history": "no changes"}:
                    quotes = g.post_manager.get_quotes(history)
                    return {"posts": history, "chat_id": chat_id, "quotes": quotes, "status": status}
                if active != chat_id:
                    log.debug("Chat switch detected for user_id=%d, chat_id=%d, active=%d", user_id, chat_id, active)
                    return {"chat_id": active, "posts": {"chat_history": "chat switch"}, "status": status}
                await asyncio.sleep(0.1)
            return {"chat_id": chat_id, "posts": {"chat_history": "no changes"}, "quotes": {}, "status": status, "wait_loops": _loops, "elapsed": "%.1f" % _elps}
        else:
            log.debug("Статус обработки для user_id=%d, chat_id=%d: %s", user_id, chat_id, status)
            history = g.post_manager.get_history(chat_id, only_changes=False)
            quotes = g.post_manager.get_quotes(history)
            return {"chat_id": chat_id, "posts": history, "quotes": quotes, "status": status}
    except Exception as e:
        handle_exception("Ошибка в GET /chat/get", e)
        raise

@router.post("/chat/notify_switch")
async def notify_chat_switch(request: Request):
    log.debug("Запрос POST /chat/notify_switch, IP=%s, Cookies=~%s", request.client.host, str(request.cookies))
    try:
        user_id = check_session(request)
        data = await request.json()
        chat_id = data.get('chat_id')
        if not chat_id:
            log.info("Неверный параметр chat_id=%s для IP=%s", str(chat_id) if chat_id is not None else "None", request.client.host)
            raise HTTPException(status_code=400, detail="Missing chat_id")
        session_id = request.cookies.get("session_id")
        if session_id:
            g.sessions_table.insert_or_replace({
                'session_id': session_id,
                'user_id': user_id,
                'active_chat': chat_id
            })
            log.debug("Обновлён active_chat=%d для session_id=%s, user_id=%d", chat_id, session_id, user_id)
        changes = g.post_manager.get_changes(chat_id)
        log.debug("Уведомление о смене чата chat_id=%d для user_id=%d, changes=~%s", chat_id, user_id, str(changes))
        return {"chat_history": "chat switch"}
    except Exception as e:
        handle_exception("Ошибка в POST /chat/notify_switch", e)
        raise


@router.post("/chat/post")
async def post_message(request: Request):
    log.debug("Запрос POST /chat/post, IP=%s, Cookies=~%s", request.client.host, str(request.cookies))
    try:
        user_id = check_session(request)
        data = await request.json()
        chat_id = data.get('chat_id')
        message = data.get('message')
        if not chat_id or not message:
            log.info("Неверные параметры chat_id=%s или message для IP=%s", str(chat_id) if chat_id is not None else "None", request.client.host)
            raise HTTPException(status_code=400, detail="Missing chat_id or message")
        post = g.post_manager.add_post(chat_id, user_id, message, rql=0, reply_to=None)
        log.debug("Добавлено сообщение для chat_id=%d, user_id=%d: %s, ожидание обработки", chat_id, user_id, str(post))
        await asyncio.sleep(0.2)   # дать шанс /get
        return await g.post_manager.process_post(post, True)
    except Exception as e:
        handle_exception("Ошибка в POST /chat/post", e)
        raise


@router.post("/chat/edit_post")
async def edit_post(request: Request):
    log.debug("Запрос POST /chat/edit_post, IP=%s, Cookies=~%s", request.client.host, str(request.cookies))
    try:
        user_id = check_session(request)
        data = await request.json()
        post_id = data.get('post_id')
        message = data.get('message')
        if not post_id or not message:
            log.info("Неверные параметры post_id=%s или message для IP=%s", str(post_id) if post_id is not None else "None", request.client.host)
            raise HTTPException(status_code=400, detail="Missing post_id or message")
        result = g.post_manager.edit_post(post_id, message, user_id)
        log.debug("Отредактировано сообщение post_id=%d для user_id=%d", post_id, user_id)
        return result
    except Exception as e:
        handle_exception("Ошибка в POST /chat/edit_post", e)
        raise


@router.post("/chat/delete_post")
async def delete_post(request: Request):
    log.debug("Запрос POST /chat/delete_post, IP=%s, Cookies=~%s", request.client.host, str(request.cookies))
    try:
        user_id = check_session(request)
        data = await request.json()
        post_id = data.get('post_id')
        if not post_id:
            log.info("Неверный параметр post_id=%s для IP=%s", str(post_id) if post_id is not None else "None", request.client.host)
            raise HTTPException(status_code=400, detail="Missing post_id")
        result = g.post_manager.delete_post(post_id, user_id)
        log.debug("Удалено сообщение post_id=%d от user_id=%d", post_id, user_id)
        return result
    except Exception as e:
        handle_exception("Ошибка в POST /chat/delete_post", e)
        raise

@router.get("/chat/get_stats")
async def get_chat_stats(request: Request, chat_id: int):
    # NOLOG!
    try:
        user_id = check_session(request)
        chat = g.chat_manager.chats_table.select_row(
            conditions=[('chat_id', '=', chat_id)],
            columns=['chat_id']
        )
        if not chat:
            log.info("Чат chat_id=%d не найден для user_id=%d", chat_id, user_id)
            raise HTTPException(status_code=404, detail="Chat not found")
        stats_row = g.replication_manager.llm_usage_table.select_row(
            columns=['used_tokens', 'sources_used'],
            conditions=[('chat_id', '=', chat_id)],
            order_by='ts DESC'
        )
        stats = {
            "chat_id": chat_id,
            "tokens": stats_row[0] if stats_row else 0,
            "num_sources_used": stats_row[1] if stats_row else 0
        }
        return stats
    except Exception as e:
        handle_exception("Ошибка в GET /chat/get_stats", e)
        raise

@router.get("/chat/get_parent_msg")
async def get_parent_msg(request: Request, post_id: int):
    log.debug("Запрос GET /chat/get_parent_msg, post_id=%d, IP=%s, Cookies=~%s", post_id, request.client.host, str(request.cookies))
    try:
        user_id = check_session(request)
        ch_tab = g.chat_manager.chats_table
        ps_tab = g.post_manager.posts_table
        msg = ps_tab.select_row(
            columns=['id', 'chat_id', 'user_id', 'message', 'timestamp'],
            conditions=[('id', '=', post_id)]
        )
        if not msg:
            log.info("Сообщение post_id=%d не найден для user_id=%d", post_id, user_id)
            affected_chats = ch_tab.select_from(
                conditions=[('parent_msg_id', '=', post_id)],
                columns=['chat_id', 'parent_msg_id']
            )
            for chat_id, parent_msg_id in affected_chats:
                chat = ch_tab.select_row(
                    conditions=[('chat_id', '=', chat_id)],
                    columns=['chat_id']
                )
                if chat:
                    new_parent_msg = ps_tab.select_row(
                        conditions=[('chat_id', '=', chat[0]), ('id', '<', post_id)],
                        columns=['id'],
                        order_by='id DESC'
                    )
                    new_parent_msg_id = new_parent_msg[0] if new_parent_msg else None
                    ch_tab.update(
                        conditions={'chat_id': chat_id},
                        values={'parent_msg_id': new_parent_msg_id}
                    )
                    log.debug("Обновлён parent_msg_id для chat_id=%d: %d -> %s", chat_id, parent_msg_id,
                              str(new_parent_msg_id) if new_parent_msg_id is not None else "None")
            return None
        result = {
            "id": msg[0],
            "chat_id": msg[1],
            "user_id": msg[2],
            "message": msg[3],
            "timestamp": msg[4]
        }
        log.debug("Возвращено сообщение post_id=%d: ~%s", post_id, str(result))
        return result
    except Exception as e:
        handle_exception("Ошибка в GET /chat/get_parent_msg", e)
        raise

@router.get("/chat/logs")
async def get_logs(request: Request):
    # NOLOG!
    try:
        user_id = check_session(request)
        log_file = g.LOG_FILE
        logs = []
        if os.path.exists(log_file):
            with open(log_file, "r", encoding="utf-8") as f:
                lines = f.readlines()[-100:]  # Последние 100 строк
                for line in lines:
                    if "#ERROR" in line or "#WARNING" in line:
                        match = re.match(r"[(.+?)] #(\w+): (.+)", line)
                        if match:
                            timestamp, level, message = match.groups()
                            timestamp = timestamp.replace(',', '.')
                            ts = time.strptime(timestamp, g.SQL_TIMESTAMP6)
                            logs.append({
                                "timestamp": int(time.mktime(ts)),
                                "level": level,
                                "message": message
                            })
        return {"logs": logs}
    except Exception as e:
        handle_exception("Ошибка в GET /chat/logs", e)
        raise