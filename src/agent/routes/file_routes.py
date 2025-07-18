# /agent/routes/file_routes.py, updated 2025-07-18 14:28 EEST
from fastapi import APIRouter, Request, UploadFile, File, Form, HTTPException, Query
import time
from managers.db import Database
import globals
from lib.basic_logger import BasicLogger

router = APIRouter()
SESSION_DB = Database()
log = globals.get_logger("fileman")

@router.post("/chat/upload_file")
async def upload_file(request: Request, file: UploadFile = File(...), chat_id: int = Form(...), file_name: str = Form(...), project_id: int = Form(None)):
    log.debug("Запрос POST /chat/upload_file, IP=%s, Cookies=~C95%s~C00, project_id=%s", request.client.host, str(request.cookies), str(project_id))
    try:
        session_id = request.cookies.get("session_id")
        if not session_id:
            log.info("Отсутствует session_id для IP=%s", request.client.host)
            return {"error": "No session"}
        user_id = SESSION_DB.fetch_one(
            'SELECT user_id FROM sessions WHERE session_id = :session_id',
            {'session_id': session_id}
        )
        if not user_id:
            log.info("Неверный session_id для IP=%s", request.client.host)
            return {"error": "Invalid session"}
        user_id = user_id[0]
        content = await file.read()
        timestamp = int(time.time())
        file_id = globals.file_manager.add_file(content, file_name, timestamp, project_id)
        if not file_id:
            log.error("Не удалось добавить файл file_name=%s, project_id=%s", file_name, str(project_id))
            raise HTTPException(status_code=500, detail="Failed to add file")
        log.debug("Файл загружен для chat_id=%d, user_id=%d, file_name=%s, project_id=%s, file_id=%d",
                  chat_id, user_id, file_name, str(project_id), file_id)
        return {"status": "ok", "file_id": file_id}
    except HTTPException as e:
        log.error("HTTP ошибка в POST /chat/upload_file: %s", str(e))
        raise
    except Exception as e:
        log.excpt("Ошибка сервера в POST /chat/upload_file: %s", str(e), exc_info=(type(e), e, e.__traceback__))
        raise HTTPException(status_code=500, detail="Server error: %s" % str(e))

@router.post("/chat/update_file")
async def update_file(request: Request, file: UploadFile = File(...), file_id: int = Form(...), file_name: str = Form(...), project_id: int = Form(None)):
    log.debug("Запрос POST /chat/update_file, IP=%s, Cookies=~C95%s~C00, project_id=%s", request.client.host, str(request.cookies), str(project_id))
    try:
        session_id = request.cookies.get("session_id")
        if not session_id:
            log.info("Отсутствует session_id для IP=%s", request.client.host)
            return {"error": "No session"}
        user_id = SESSION_DB.fetch_one(
            'SELECT user_id FROM sessions WHERE session_id = :session_id',
            {'session_id': session_id}
        )
        if not user_id:
            log.info("Неверный session_id для IP=%s", request.client.host)
            return {"error": "Invalid session"}
        user_id = user_id[0]
        content = await file.read()
        timestamp = int(time.time())
        globals.file_manager.update_file(file_id, content, file_name, timestamp, project_id)
        log.debug("Файл file_id=%d обновлён для user_id=%d, file_name=%s, project_id=%s",
                  file_id, user_id, file_name, str(project_id))
        return {"status": "ok", "file_id": file_id}
    except HTTPException as e:
        log.error("HTTP ошибка в POST /chat/update_file: %s", str(e))
        raise
    except Exception as e:
        log.excpt("Ошибка сервера в POST /chat/update_file: %s", str(e), exc_info=(type(e), e, e.__traceback__))
        raise HTTPException(status_code=500, detail="Server error: %s" % str(e))

@router.post("/chat/delete_file")
async def delete_file(request: Request):
    log.debug("Запрос POST /chat/delete_file, IP=%s, Cookies=~C95%s~C00", request.client.host, str(request.cookies))
    try:
        session_id = request.cookies.get("session_id")
        if not session_id:
            log.info("Отсутствует session_id для IP=%s", request.client.host)
            return {"error": "No session"}
        user_id = SESSION_DB.fetch_one(
            'SELECT user_id FROM sessions WHERE session_id = :session_id',
            {'session_id': session_id}
        )
        if not user_id:
            log.info("Неверный session_id для IP=%s", request.client.host)
            return {"error": "Invalid session"}
        user_id = user_id[0]
        data = await request.json()
        file_id = data.get('file_id')
        if not file_id:
            log.info("Неверный параметр file_id=%s для IP=%s", str(file_id), request.client.host)
            return {"error": "Missing file_id"}
        globals.file_manager.unlink(file_id)
        log.debug("Файл file_id=%d удалён для user_id=%d", file_id, user_id)
        return {"status": "ok"}
    except HTTPException as e:
        log.error("HTTP ошибка в POST /chat/delete_file: %s", str(e))
        raise
    except Exception as e:
        log.excpt("Ошибка сервера в POST /chat/delete_file: %s", str(e), exc_info=(type(e), e, e.__traceback__))
        raise HTTPException(status_code=500, detail="Server error: %s" % str(e))

@router.get("/chat/list_files")
async def list_files(request: Request, project_id: int = Query(None)):
    log.debug("Запрос GET /chat/list_files, IP=%s, Cookies=~C95%s~C00, project_id=%s", request.client.host, str(request.cookies), str(project_id))
    try:
        session_id = request.cookies.get("session_id")
        if not session_id:
            log.info("Отсутствует session_id для IP=%s", request.client.host)
            return {"error": "No session"}
        user_id = SESSION_DB.fetch_one(
            'SELECT user_id FROM sessions WHERE session_id = :session_id',
            {'session_id': session_id}
        )
        if not user_id:
            log.info("Неверный session_id для IP=%s", request.client.host)
            return {"error": "Invalid session"}
        user_id = user_id[0]
        files = globals.file_manager.list_files(user_id, project_id)
        log.debug("Возвращено %d файлов для user_id=%d, project_id=%s", len(files), user_id, str(project_id))
        return files
    except HTTPException as e:
        log.error("HTTP ошибка в GET /chat/list_files: %s", str(e))
        raise
    except Exception as e:
        log.excpt("Ошибка сервера в GET /chat/list_files: %s", str(e), exc_info=(type(e), e, e.__traceback__))
        raise HTTPException(status_code=500, detail="Server error: %s" % str(e))