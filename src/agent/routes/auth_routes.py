# /agent/routes/auth_routes.py, updated 2025-07-16 22:17 EEST
from fastapi import APIRouter, Request, Response, HTTPException
import logging
from managers.db import Database
import globals
import uuid
import json

router = APIRouter()

@router.post("/login")
async def login(request: Request):
    db = Database.get_database()
    logging.debug(f"Запрос POST /login, IP={request.client.host}, Cookies={request.cookies}")
    try:
        data = await request.json()
        username = data.get("username")
        password = data.get("password")
        if not username or not password:
            logging.info(f"Отсутствует username или password для IP {request.client.host}")
            raise HTTPException(status_code=400, detail="Missing username or password")
        user_id = globals.user_manager.check_auth(username, password)
        if not user_id:
            logging.info(f"Неверные учетные данные для username={username}, IP={request.client.host}")
            raise HTTPException(status_code=401, detail="Invalid username or password")
        session_id = str(uuid.uuid4())
        db.execute(
            'INSERT INTO sessions (session_id, user_id) VALUES (:session_id, :user_id)',
            {'session_id': session_id, 'user_id': user_id}
        )
        logging.debug(f"Создана сессия session_id={session_id} для user_id={user_id}")
        response = Response(content="Login successful")
        response.set_cookie(key="session_id", value=session_id, httponly=True)
        return response
    except HTTPException as e:
        logging.error(f"HTTP ошибка в POST /login: {str(e)}")
        raise
    except Exception as e:
        logging.error(f"Ошибка сервера в POST /login: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Server error: {str(e)}")

@router.post("/logout")
async def logout(request: Request):
    db = Database.get_database()
    logging.debug(f"Запрос POST /logout, IP={request.client.host}, Cookies={request.cookies}")
    try:
        session_id = request.cookies.get("session_id")
        if not session_id:
            logging.info(f"Отсутствует session_id для IP {request.client.host}")
            raise HTTPException(status_code=401, detail="No session")
        db.execute(
            'DELETE FROM sessions WHERE session_id = :session_id',
            {'session_id': session_id}
        )
        logging.debug(f"Удалена сессия session_id={session_id}")
        response = Response(content="Logout successful")
        response.delete_cookie(key="session_id")
        return response
    except HTTPException as e:
        logging.error(f"HTTP ошибка в POST /logout: {str(e)}")
        raise
    except Exception as e:
        logging.error(f"Ошибка сервера в POST /logout: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Server error: {str(e)}")

@router.get("/user/info")
async def get_user_info(request: Request):
    db = Database.get_database()
    logging.debug(f"Запрос GET /user/info, IP={request.client.host}, Cookies={request.cookies}")
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
        user_name = globals.user_manager.get_user_name(user_id)
        role = 'admin' if user_name == 'admin' else 'mcp' if user_name == 'agent' else 'developer'
        if globals.user_manager.is_llm_user(user_id):
            role = 'LLM'
        logging.debug(f"Возвращена информация для user_id={user_id}: {user_name}, {role}")
        return {"user_id": user_id, "user_name": user_name, "role": role}
    except HTTPException as e:
        logging.error(f"HTTP ошибка в GET /user/info: {str(e)}")
        raise
    except Exception as e:
        logging.error(f"Ошибка сервера в GET /user/info: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Server error: {str(e)}")

@router.get("/user/settings")
async def get_user_settings(request: Request):
    db = Database.get_database()
    logging.debug(f"Запрос GET /user/settings, IP={request.client.host}, Cookies={request.cookies}")
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
        settings = db.fetch_one(
            'SELECT search_mode, search_sources, max_search_results, from_date, to_date FROM user_settings WHERE user_id = :user_id',
            {'user_id': user_id}
        )
        default_settings = {
            "mode": "off",
            "sources": ["web", "x", "news"],
            "max_search_results": 20,
            "from_date": None,
            "to_date": None
        }
        if not settings:
            logging.debug(f"Настройки не найдены для user_id={user_id}, возвращаются значения по умолчанию")
            return default_settings
        try:
            sources = json.loads(settings[1]) if settings[1] else default_settings["sources"]
            sources = [src for src in sources if src in ['web', 'x', 'news']]
        except json.JSONDecodeError:
            logging.warning(f"Некорректный JSON в search_sources для user_id={user_id}, возвращаются значения по умолчанию")
            sources = default_settings["sources"]
        result = {
            "mode": settings[0] or default_settings["mode"],
            "sources": sources,
            "max_search_results": settings[2] or default_settings["max_search_results"],
            "from_date": settings[3],
            "to_date": settings[4]
        }
        logging.debug(f"Возвращены настройки для user_id={user_id}: {result}")
        return result
    except HTTPException as e:
        logging.error(f"HTTP ошибка в GET /user/settings: {str(e)}")
        raise
    except Exception as e:
        logging.error(f"Ошибка сервера в GET /user/settings: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Server error: {str(e)}")

@router.post("/user/settings")
async def save_user_settings(request: Request):
    db = Database.get_database()
    logging.debug(f"Запрос POST /user/settings, IP={request.client.host}, Cookies={request.cookies}")
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
        mode = data.get("mode", "off")
        sources = json.dumps([src for src in data.get("sources", ["web", "x", "news"]) if src in ["web", "x", "news"]])
        max_search_results = data.get("max_search_results", 20)
        db.execute(
            'INSERT OR REPLACE INTO user_settings (user_id, search_mode, search_sources, max_search_results, from_date, to_date) '
            'VALUES (:user_id, :mode, :sources, :max_search_results, :from_date, :to_date)',
            {
                'user_id': user_id,
                'mode': mode,
                'sources': sources,
                'max_search_results': max_search_results,
                'from_date': data.get("from_date"),
                'to_date': data.get("to_date")
            }
        )
        logging.debug(f"Сохранены настройки для user_id={user_id}: mode={mode}, sources={sources}, max_search_results={max_search_results}")
        return {"status": "Settings saved"}
    except HTTPException as e:
        logging.error(f"HTTP ошибка в POST /user/settings: {str(e)}")
        raise
    except Exception as e:
        logging.error(f"Ошибка сервера в POST /user/settings: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Server error: {str(e)}")