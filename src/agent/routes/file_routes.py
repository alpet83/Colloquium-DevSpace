# /agent/routes/file_routes.py, updated 2025-07-16 10:48 EEST
from fastapi import APIRouter, Request, UploadFile, File, Form, HTTPException, Query
import logging
from managers.db import Database
import globals

router = APIRouter()
SESSION_DB = Database()

@router.post("/chat/upload_file")
async def upload_file(request: Request, file: UploadFile = File(...), chat_id: int = Form(...), file_name: str = Form(...), project_id: int = Form(None)):
    logging.debug(f"#DEBUG: Запрос POST /chat/upload_file, IP={request.client.host}, Cookies={request.cookies}, project_id={project_id}")
    try:
        session_id = request.cookies.get("session_id")
        if not session_id:
            logging.info(f"#INFO: Отсутствует session_id для IP {request.client.host}")
            return {"error": "No session"}
        user_id = SESSION_DB.fetch_one(
            'SELECT user_id FROM sessions WHERE session_id = :session_id',
            {'session_id': session_id}
        )
        if not user_id:
            logging.info(f"#INFO: Неверный session_id для IP {request.client.host}")
            return {"error": "Invalid session"}
        user_id = user_id[0]
        content = await file.read()
        result = globals.file_manager.upload_file(chat_id, user_id, content, file_name, project_id)
        logging.debug(f"#DEBUG: Файл загружен для chat_id={chat_id}, user_id={user_id}, file_name={file_name}, project_id={project_id}")
        return result
    except HTTPException as e:
        logging.error(f"#ERROR: HTTP ошибка в POST /chat/upload_file: {str(e)}")
        raise
    except Exception as e:
        logging.error(f"#ERROR: Ошибка сервера в POST /chat/upload_file: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Server error: {str(e)}")

@router.post("/chat/update_file")
async def update_file(request: Request, file: UploadFile = File(...), file_id: int = Form(...), file_name: str = Form(...), project_id: int = Form(None)):
    logging.debug(f"#DEBUG: Запрос POST /chat/update_file, IP={request.client.host}, Cookies={request.cookies}, project_id={project_id}")
    try:
        session_id = request.cookies.get("session_id")
        if not session_id:
            logging.info(f"#INFO: Отсутствует session_id для IP {request.client.host}")
            return {"error": "No session"}
        user_id = SESSION_DB.fetch_one(
            'SELECT user_id FROM sessions WHERE session_id = :session_id',
            {'session_id': session_id}
        )
        if not user_id:
            logging.info(f"#INFO: Неверный session_id для IP {request.client.host}")
            return {"error": "Invalid session"}
        user_id = user_id[0]
        content = await file.read()
        result = globals.file_manager.update_file(file_id, user_id, content, file_name, project_id)
        logging.debug(f"#DEBUG: Файл file_id={file_id} обновлён для user_id={user_id}, file_name={file_name}, project_id={project_id}")
        return result
    except HTTPException as e:
        logging.error(f"#ERROR: HTTP ошибка в POST /chat/update_file: {str(e)}")
        raise
    except Exception as e:
        logging.error(f"#ERROR: Ошибка сервера в POST /chat/update_file: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Server error: {str(e)}")

@router.post("/chat/delete_file")
async def delete_file(request: Request):
    logging.debug(f"#DEBUG: Запрос POST /chat/delete_file, IP={request.client.host}, Cookies={request.cookies}")
    try:
        session_id = request.cookies.get("session_id")
        if not session_id:
            logging.info(f"#INFO: Отсутствует session_id для IP {request.client.host}")
            return {"error": "No session"}
        user_id = SESSION_DB.fetch_one(
            'SELECT user_id FROM sessions WHERE session_id = :session_id',
            {'session_id': session_id}
        )
        if not user_id:
            logging.info(f"#INFO: Неверный session_id для IP {request.client.host}")
            return {"error": "Invalid session"}
        user_id = user_id[0]
        data = await request.json()
        file_id = data.get('file_id')
        if not file_id:
            logging.info(f"#INFO: Неверный параметр file_id={file_id} для IP {request.client.host}")
            return {"error": "Missing file_id"}
        result = globals.file_manager.delete_file(file_id, user_id)
        logging.debug(f"#DEBUG: Файл file_id={file_id} удалён для user_id={user_id}")
        return result
    except HTTPException as e:
        logging.error(f"#ERROR: HTTP ошибка в POST /chat/delete_file: {str(e)}")
        raise
    except Exception as e:
        logging.error(f"#ERROR: Ошибка сервера в POST /chat/delete_file: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Server error: {str(e)}")

@router.get("/chat/list_files")
async def list_files(request: Request, project_id: int = Query(None)):
    logging.debug(f"#DEBUG: Запрос GET /chat/list_files, IP={request.client.host}, Cookies={request.cookies}, project_id={project_id}")
    try:
        session_id = request.cookies.get("session_id")
        if not session_id:
            logging.info(f"#INFO: Отсутствует session_id для IP {request.client.host}")
            return {"error": "No session"}
        user_id = SESSION_DB.fetch_one(
            'SELECT user_id FROM sessions WHERE session_id = :session_id',
            {'session_id': session_id}
        )
        if not user_id:
            logging.info(f"#INFO: Неверный session_id для IP {request.client.host}")
            return {"error": "Invalid session"}
        user_id = user_id[0]
        files = globals.file_manager.list_files(user_id, project_id)
        logging.debug(f"#DEBUG: Возвращено {len(files)} файлов для user_id={user_id}, project_id={project_id}")
        return files
    except HTTPException as e:
        logging.error(f"#ERROR: HTTP ошибка в GET /chat/list_files: {str(e)}")
        raise
    except Exception as e:
        logging.error(f"#ERROR: Ошибка сервера в GET /chat/list_files: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Server error: {str(e)}")