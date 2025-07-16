# /agent/routes/post_routes.py, updated 2025-07-15 21:30 EEST
from fastapi import APIRouter, Request, HTTPException
import logging
import traceback
import sys
import datetime
from managers.db import Database

import globals

router = APIRouter()

@router.get("/chat/get")
async def get_chat(chat_id: int, request: Request):
    try:
        if not isinstance(chat_id, int) or chat_id <= 0:
            logging.error(f"Некорректный chat_id={chat_id} для IP {request.client.host}")
            raise HTTPException(status_code=400, detail="Invalid chat_id")
        session_id = request.cookies.get("session_id")
        if not session_id:
            logging.info(f" Отсутствует session_id для IP {request.client.host}")
            raise HTTPException(status_code=401, detail="No session")
        db = Database.get_database()
        user_id = db.fetch_one(
            'SELECT user_id FROM sessions WHERE session_id = :session_id',
            {'session_id': session_id}
        )
        if not user_id:
            logging.info(f"Неверный session_id={session_id} для IP {request.client.host}")
            raise HTTPException(status_code=401, detail="Invalid session")
        user_id = user_id[0]
        history = globals.post_manager.get_history(chat_id)
        return history
    except HTTPException as e:
        logging.error(f"HTTP ошибка в GET /chat/get для chat_id={chat_id}: {str(e)}")
        raise
    except Exception as e:
        logging.error(f"Ошибка сервера в GET /chat/get для chat_id={chat_id}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Server error: {str(e)}")

@router.post("/chat/post")
async def post_chat(request: Request):
    logging.debug(f"Запрос POST /chat/post, IP={request.client.host}, Cookies={request.cookies}")
    try:
        session_id = request.cookies.get("session_id")
        if not session_id:
            logging.info(f"Отсутствует session_id для IP {request.client.host}")
            raise HTTPException(status_code=401, detail="No session")
        db = Database.get_database()
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
            logging.info(f"Неверные параметры chat_id={chat_id}, message={message} для IP {request.client.host}")
            raise HTTPException(status_code=400, detail="Missing chat_id or message")
        result = globals.post_manager.add_message(chat_id, user_id, message)
        logging.debug(f"Сообщение добавлено для chat_id={chat_id}, user_id={user_id}")
        return result
    except HTTPException as e:
        logging.error(f"HTTP ошибка в POST /chat/post: {str(e)}")
        raise
    except Exception as e:
        logging.error(f"Ошибка сервера в POST /chat/post: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Server error: {str(e)}")

@router.post("/chat/delete_post")
async def delete_post(request: Request):
    logging.debug(f" Запрос POST /chat/delete_post, IP={request.client.host}, Cookies={request.cookies}")
    try:
        session_id = request.cookies.get("session_id")
        if not session_id:
            logging.info(f"Отсутствует session_id для IP {request.client.host}")
            raise HTTPException(status_code=401, detail="No session")
        db = Database.get_database()
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
        logging.debug(f"Сообщение post_id={post_id} удалено для user_id={user_id}")
        return result
    except HTTPException as e:
        logging.error(f"HTTP ошибка в POST /chat/delete_post: {str(e)}")
        raise
    except Exception as e:
        logging.error(f"Ошибка сервера в POST /chat/delete_post: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Server error: {str(e)}")

@router.post("/chat/edit_post")
async def edit_post(request: Request):
    logging.debug(f"Запрос POST /chat/edit_post, IP={request.client.host}, Cookies={request.cookies}")
    try:
        session_id = request.cookies.get("session_id")
        if not session_id:
            logging.info(f"Отсутствует session_id для IP {request.client.host}")
            raise HTTPException(status_code=401, detail="No session")
        db = Database.get_database()
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
            logging.info(f"Неверные параметры post_id={post_id}, message={message} для IP {request.client.host}")
            raise HTTPException(status_code=400, detail="Missing post_id or message")
        post = globals.post_manager.get_post(post_id)
        if not post:
            logging.info(f"Сообщение post_id={post_id} не найдено")
            raise HTTPException(status_code=404, detail="Post not found")
        if post['user_id'] != user_id and globals.user_manager.get_user_role(user_id) != 'admin':
            logging.info(f"Пользователь user_id={user_id} не имеет прав для редактирования post_id={post_id}")
            raise HTTPException(status_code=403, detail="Only post author or admin can edit")
        result = globals.post_manager.edit_post(post_id, message)
        logging.debug(f"Сообщение post_id={post_id} отредактировано для user_id={user_id}")
        return result
    except HTTPException as e:
        logging.error(f"HTTP ошибка в POST /chat/edit_post: {str(e)}")
        raise
    except Exception as e:
        logging.error(f"Ошибка сервера в POST /chat/edit_post: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Server error: {str(e)}")

@router.get("/user/info")
async def user_info(request: Request):
    # logging.debug(f"Запрос GET /user/info, IP={request.client.host}, Cookies={request.cookies}")
    try:
        session_id = request.cookies.get("session_id")
        if not session_id:
            logging.info(f"Отсутствует session_id для IP {request.client.host}")
            raise HTTPException(status_code=401, detail="No session")
        db = Database.get_database()
        user_id = db.fetch_one(
            'SELECT user_id FROM sessions WHERE session_id = :session_id',
            {'session_id': session_id}
        )
        if not user_id:
            logging.info(f"Неверный session_id для IP {request.client.host}")
            raise HTTPException(status_code=401, detail="Invalid session")
        user_id = user_id[0]
        username = globals.user_manager.get_user_name(user_id)
        role = globals.user_manager.get_user_role(user_id)
        if not username:
            logging.info(f" Пользователь user_id={user_id} не найден")
            raise HTTPException(status_code=404, detail="User not found")
        logging.debug(f"Возвращена информация о пользователе user_id={user_id}")
        return {
            "user_id": user_id,
            "username": username,
            "role": role
        }
    except HTTPException as e:
        logging.error(f"HTTP ошибка в GET /user/info: {str(e)}")
        raise
    except Exception as e:
        logging.error(f"Ошибка сервера в GET /user/info: {str(e)}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Server error: {str(e)}")