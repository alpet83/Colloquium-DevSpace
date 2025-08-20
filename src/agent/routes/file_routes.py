# /agent/routes/file_routes.py, updated 2025-07-26 19:00 EEST
from fastapi import APIRouter, Request, Response, UploadFile, File, Form, HTTPException, Query
from fastapi.responses import PlainTextResponse, JSONResponse
import time, json
from managers.db import Database
import globals as g
from lib.basic_logger import BasicLogger

router = APIRouter()

log = g.get_logger("fileman")
db = Database.get_database()

@router.post("/chat/upload_file")
async def upload_file(request: Request, file: UploadFile = File(...), chat_id: int = Form(...), file_name: str = Form(...), project_id: int = Form(None)):
    log.debug("Запрос POST /chat/upload_file, IP=%s, Cookies=~%s, project_id=%s", request.client.host, str(request.cookies), str(project_id))
    try:
        user_id = g.check_session(request)
        content = await file.read()
        timestamp = int(time.time())
        file_id = g.file_manager.add_file(file_name, content, timestamp, project_id)
        if not file_id:
            log.error("Не удалось добавить файл file_name=%s, project_id=%s", file_name, str(project_id))
            raise HTTPException(status_code=500, detail="Failed to add file")
        log.debug("Файл загружен для chat_id=%d, user_id=%d, file_name=%s, project_id=%s, file_id=%d",
                  chat_id, user_id, file_name, str(project_id), file_id)
        return {"status": "ok", "file_id": file_id}
    except Exception as e:
        g.handle_exception("Ошибка в POST /chat/upload_file", e)
        raise

@router.post("/chat/update_file")
async def update_file(request: Request, file: UploadFile = File(...), file_id: int = Form(...), project_id: int = Form(None)):
    log.debug("Запрос POST /chat/update_file, IP=%s, Cookies=~%s, project_id=%s", request.client.host, str(request.cookies), str(project_id))
    try:
        user_id = g.check_session(request)
        content = await file.read()
        timestamp = int(time.time())
        g.file_manager.update_file(file_id, content, timestamp, project_id)
        log.debug("Файл file_id=%d обновлён для user_id=%d, project_id=%s",
                  file_id, user_id, str(project_id))
        return {"status": "ok", "file_id": file_id}
    except Exception as e:
        g.handle_exception("Ошибка в POST /chat/update_file", e)
        raise


@router.post("/chat/move_file")
async def move_file(request: Request):
    log.debug("Запрос POST /chat/move_file, IP=%s, Cookies=~%s", request.client.host, str(request.cookies))
    try:
        user_id = g.check_session(request)
        data = await request.json()
        file_id = data.get('file_id')
        new_name = data.get('new_name')
        project_id = data.get('project_id')
        if not file_id or not new_name:
            log.info("Неверные параметры file_id=%s, new_name=%s для IP=%s", str(file_id), str(new_name), request.client.host)
            raise HTTPException(status_code=400, detail="Missing file_id or new_name")
        result = g.file_manager.move_file(file_id, new_name, project_id)
        if result <= 0:
            log.error("Не удалось переименовать файл file_id=%d, new_name=%s, project_id=%s", file_id, new_name, str(project_id))
            raise HTTPException(status_code=500, detail=f"Failed to move file: code {result}")
        log.debug("Файл file_id=%d переименован в %s для user_id=%d, project_id=%s", file_id, new_name, user_id, str(project_id))
        return {"status": "ok", "file_id": file_id}
    except Exception as e:
        g.handle_exception("Ошибка в POST /chat/move_file", e)
        raise


@router.post("/chat/delete_file")
async def delete_file(request: Request):
    log.debug("Запрос POST /chat/delete_file, IP=%s, Cookies=~%s", request.client.host, str(request.cookies))
    try:
        user_id = g.check_session(request)
        data = await request.json()
        file_id = data.get('file_id')
        if not file_id:
            log.info("Неверный параметр file_id=%s для IP=%s", str(file_id), request.client.host)
            raise HTTPException(status_code=400, detail="Missing file_id")
        g.file_manager.unlink(file_id)
        log.debug("Файл file_id=%d удалён для user_id=%d", file_id, user_id)
        return {"status": "ok"}
    except Exception as e:
        g.handle_exception("Ошибка в POST /chat/delete_file", e)
        raise


@router.get("/chat/list_files")
async def list_files(request: Request, project_id: int = Query(None)):
    try:
        # Интерпретируем project_id <= 0 как запрос всех файлов
        project_id = None if (project_id is None) or (project_id <= 0) else project_id
        fm = g.file_manager
        files = fm.list_files(project_id, as_map=True)
        if project_id is not None:
            common = fm.list_files(sql_filter=('file_name', 'LIKE', '@.chat-meta%'), as_map=True)  # always list common index files
            log.debug("Common files: %s", str(common))
            files.update(common)
        assert isinstance(files, dict)
        files = list(files.values())
        files.sort(key=lambda x: x["file_name"])
        return files
    except Exception as e:
        g.handle_exception("Ошибка в GET /chat/list_files", e)
        raise


@router.get("/chat/file_contents")
async def get_file_contents(request: Request, file_id: int = Query(...)):
    log.debug("Запрос GET /chat/file_contents, IP=%s, Cookies=~%s, file_id=%d", request.client.host, str(request.cookies), file_id)
    try:
        user_id = g.check_session(request)
        file_data = g.file_manager.get_file(file_id)
        if not file_data or file_data['content'] is None:
            log.warn("Файл file_id=%d не найден или не содержит данных", file_id)
            raise HTTPException(status_code=404, detail="File not found or no content")

        file_name = str(file_data['file_name'])
        content = file_data.get('content', '')
        if file_name.lower().endswith('.json'):
            log.debug("Возвращен парсинг JSON file_id=%d для user_id=%d, content_length=%d", file_id, user_id, len(file_data['content']))
            return Response(json.loads(content), media_type="application/json")  # {"content": json.loads(content)}
        else:
            log.debug("Возвращено содержимое файла file_id=%d для user_id=%d, content_length=%d", file_id, user_id, len(file_data['content']))
            return PlainTextResponse(content)   # поскольку это API, приходится изворачиваться
    except Exception as e:
        g.handle_exception("Ошибка в GET /chat/file_contents", e)
        raise