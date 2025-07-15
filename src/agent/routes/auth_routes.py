# /agent/routes/auth_routes.py, updated 2025-07-14 19:00 EEST
from fastapi import APIRouter, Request, Response, HTTPException
import logging
import uuid
import json
from managers.db import Database

router = APIRouter()
SESSION_DB = Database()

def init_sessions_table():
    logging.info("#INFO: Создание таблицы sessions")
    SESSION_DB.execute('''
        CREATE TABLE IF NOT EXISTS sessions (
            session_id TEXT PRIMARY KEY,
            user_id INTEGER,
            FOREIGN KEY (user_id) REFERENCES users(user_id)
        )
    ''')

init_sessions_table()

@router.post("/login")
async def login(request: Request):
    logging.debug(f"#DEBUG: Запрос POST /login, IP={request.client.host}, Cookies={request.cookies}")
    try:
        data = await request.json()
        ip = request.client.host
        username = data.get('username')
        password = data.get('password')
        logging.info(f"#INFO: Попытка логина с IP {ip}: username={username}")
        from globals import user_manager
        user_id = user_manager.check_auth(username, password)
        if not user_id:
            logging.info(f"#INFO: Неверные учетные данные для username={username}")
            return {"error": "Invalid username or password"}
        session_id = str(uuid.uuid4())
        SESSION_DB.execute(
            'INSERT INTO sessions (session_id, user_id) VALUES (:session_id, :user_id)',
            {'session_id': session_id, 'user_id': user_id}
        )
        response = Response(content=json.dumps({"status": "ok"}))
        response.set_cookie(key="session_id", value=session_id, samesite="Lax", secure=False)
        logging.info(f"#INFO: Успешный логин: username={username}, session_id={session_id}")
        return response
    except HTTPException as e:
        logging.error(f"#ERROR: HTTP ошибка в POST /login: {str(e)}")
        raise
    except Exception as e:
        logging.error(f"#ERROR: Ошибка сервера в POST /login: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Server error: {str(e)}")

@router.post("/logout")
async def logout(request: Request):
    logging.debug(f"#DEBUG: Запрос POST /logout, IP={request.client.host}, Cookies={request.cookies}")
    try:
        session_id = request.cookies.get("session_id")
        ip = request.client.host
        logging.info(f"#INFO: Попытка выхода с IP {ip}: session_id={session_id}")
        if not session_id:
            return {"error": "No session"}
        SESSION_DB.execute('DELETE FROM sessions WHERE session_id = :session_id', {'session_id': session_id})
        response = Response(content=json.dumps({"status": "ok"}))
        response.delete_cookie("session_id")
        logging.info(f"#INFO: Успешный выход: session_id={session_id}")
        return response
    except HTTPException as e:
        logging.error(f"#ERROR: HTTP ошибка в POST /logout: {str(e)}")
        raise
    except Exception as e:
        logging.error(f"#ERROR: Ошибка сервера в POST /logout: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Server error: {str(e)}")
