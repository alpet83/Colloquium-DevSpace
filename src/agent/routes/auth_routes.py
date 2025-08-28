# /agent/routes/auth_routes.py, updated 2025-07-18 14:28 EEST
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import JSONResponse
from managers.db import Database
import globals
import uuid
import json
from lib.basic_logger import BasicLogger

router = APIRouter()
log = globals.get_logger("auth")

@router.post("/login")
async def login(request: Request):
    db = Database.get_database()
    log.debug("Запрос POST /login, IP=%s, Cookies=~C95%s~C00", request.client.host, str(request.cookies))
    try:
        data = await request.json()
        username = data.get("username")
        password = data.get("password")
        if not username or not password:
            log.info("Отсутствует username или password для IP=%s", request.client.host)
            raise HTTPException(status_code=400, detail="Missing username or password")
        user_id = globals.user_manager.check_auth(username, password)
        if not user_id:
            log.info("Неверные учетные данные для username=%s, IP=%s", username, request.client.host)
            raise HTTPException(status_code=401, detail="Invalid username or password")
        session_id = str(uuid.uuid4())
        db.execute(
            'INSERT INTO sessions (session_id, user_id) VALUES (:session_id, :user_id)',
            {'session_id': session_id, 'user_id': user_id}
        )
        log.debug("Создана сессия session_id=%s для user_id=%d", session_id, user_id)
        response = JSONResponse(content="Login successful")
        response.set_cookie(key="session_id", value=session_id, httponly=True)
        return response

    except HTTPException as e:
        log.error("HTTP ошибка в POST /login: %s", str(e))
        raise
    except Exception as e:
        log.excpt("Ошибка сервера в POST /login: ", e=e)
        raise HTTPException(status_code=500, detail="Server error: %s" % str(e))

@router.post("/logout")
async def logout(request: Request):
    db = Database.get_database()
    log.debug("Запрос POST /logout, IP=%s, Cookies=~C95%s~C00", request.client.host, str(request.cookies))
    try:
        session_id = request.cookies.get("session_id")
        if not session_id:
            log.info("Отсутствует session_id для IP=%s", request.client.host)
            raise HTTPException(status_code=401, detail="No session")
        db.execute(
            'DELETE FROM sessions WHERE session_id = :session_id',
            {'session_id': session_id}
        )
        log.debug("Удалена сессия session_id=%s", session_id)
        response = JSONResponse(content="Logout successful")
        response.delete_cookie(key="session_id")
        return response
    except HTTPException as e:
        log.error("HTTP ошибка в POST /logout: %s", str(e))
        raise
    except Exception as e:
        log.excpt("Ошибка сервера в POST /logout: ", e=e)
        raise HTTPException(status_code=500, detail="Server error: %s" % str(e))

@router.get("/user/info")
async def get_user_info(request: Request):
    db = Database.get_database()
    log.debug("Запрос GET /user/info, IP=%s, Cookies=~C95%s~C00", request.client.host, str(request.cookies))
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
        user_name = globals.user_manager.get_user_name(user_id)
        role = 'admin' if user_name == 'admin' else 'mcp' if user_name == 'agent' else 'developer'
        if globals.user_manager.is_llm_user(user_id):
            role = 'LLM'
        log.debug("Возвращена информация для user_id=%d: %s, %s", user_id, user_name, role)
        return {"user_id": user_id, "user_name": user_name, "role": role}
    except HTTPException as e:
        log.error("HTTP ошибка в GET /user/info: %s", str(e))
        raise
    except Exception as e:
        log.excpt("Ошибка сервера в GET /user/info: ", e=e)
        raise HTTPException(status_code=500, detail="Server error: %s" % str(e))

@router.get("/user/settings")
async def get_user_settings(request: Request):
    db = Database.get_database()
    log.debug("Запрос GET /user/settings, IP=%s, Cookies=~C95%s~C00", request.client.host, str(request.cookies))
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
            log.debug("Настройки не найдены для user_id=%d, возвращаются значения по умолчанию", user_id)
            return default_settings
        try:
            sources = json.loads(settings[1]) if settings[1] else default_settings["sources"]
            sources = [src for src in sources if src in ['web', 'x', 'news']]
        except json.JSONDecodeError:
            log.warn("Некорректный JSON в search_sources для user_id=%d, возвращаются значения по умолчанию", user_id)
            sources = default_settings["sources"]
        result = {
            "mode": settings[0] or default_settings["mode"],
            "sources": sources,
            "max_search_results": settings[2] or default_settings["max_search_results"],
            "from_date": settings[3],
            "to_date": settings[4]
        }
        log.debug("Возвращены настройки для user_id=%d: ~C95%s~C00", user_id, str(result))
        return result
    except HTTPException as e:
        log.error("HTTP ошибка в GET /user/settings: %s", str(e))
        raise
    except Exception as e:
        log.excpt("Ошибка сервера в GET /user/settings: ", e=e)
        raise HTTPException(status_code=500, detail="Server error: %s" % str(e))

@router.post("/user/settings")
async def save_user_settings(request: Request):
    db = Database.get_database()
    log.debug("Запрос POST /user/settings, IP=%s, Cookies=~C95%s~C00", request.client.host, str(request.cookies))
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
        log.debug("Сохранены настройки для user_id=%d: mode=%s, sources=%s, max_search_results=%d",
                  user_id, mode, sources, max_search_results)
        return {"status": "Settings saved"}
    except HTTPException as e:
        log.error("HTTP ошибка в POST /user/settings: %s", str(e))
        raise
    except Exception as e:
        log.excpt("Ошибка сервера в POST /user/settings: ", e=e)
        raise HTTPException(status_code=500, detail="Server error: %s" % str(e))