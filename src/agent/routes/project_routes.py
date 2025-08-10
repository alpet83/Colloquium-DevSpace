# /agent/routes/project_routes.py, updated 2025-07-18 14:28 EEST
from fastapi import APIRouter, Request, HTTPException
from managers.db import Database
from managers.project import ProjectManager
import globals as g
from lib.basic_logger import BasicLogger

router = APIRouter()
log = g.get_logger("projectman")

@router.get("/project/list")
async def list_projects(request: Request):
    db = Database.get_database()
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
        projects = g.project_manager.list_projects()
        log.debug("Возвращено %d проектов для user_id=%d", len(projects), user_id)
        return projects
    except HTTPException as e:
        log.error("HTTP ошибка в GET /project/list: %s", str(e))
        raise
    except Exception as e:
        g.handle_exception("Ошибка сервера в GET /project/list: ", e)

@router.post("/project/create")
async def create_project(request: Request):
    db = Database.get_database()
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
        project_name = data.get('project_name')
        description = data.get('description', '')
        local_git = data.get('local_git')
        public_git = data.get('public_git')
        dependencies = data.get('dependencies')
        if not project_name:
            log.info("Неверный параметр project_name=%s для IP=%s", str(project_name), request.client.host)
            raise HTTPException(status_code=400, detail="Missing project_name")
        project_id = g.project_manager.create_project(project_name, description, local_git, public_git, dependencies)
        log.debug("Создан проект project_id=%d, project_name=%s для user_id=%d", project_id, project_name, user_id)
        return {"project_id": project_id}
    except HTTPException as e:
        log.error("HTTP ошибка в POST /project/create: %s", str(e))
        raise
    except Exception as e:
        g.handle_exception("Ошибка сервера в POST /project/create: ", e)

@router.post("/project/update")
async def update_project(request: Request):
    db = Database.get_database()
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
        project_id = data.get('project_id')
        project_name = data.get('project_name')
        description = data.get('description')
        local_git = data.get('local_git')
        public_git = data.get('public_git')
        dependencies = data.get('dependencies')
        if not project_id or not project_name:
            log.info("Неверные параметры project_id=%s, project_name=%s для IP=%s",
                     str(project_id), str(project_name), request.client.host)
            raise HTTPException(status_code=400, detail="Missing project_id or project_name")
        project_manager = ProjectManager(project_id)
        project_manager.update(project_name, description, local_git, public_git, dependencies)
        log.debug("Обновлён проект project_id=%d, project_name=%s для user_id=%d", project_id, project_name, user_id)
        return {"status": "Project updated"}
    except HTTPException as e:
        log.error("HTTP ошибка в POST /project/update: %s", str(e))
        raise
    except Exception as e:
        g.handle_exception("Ошибка сервера в POST /project/update: ", e)

@router.post("/project/select")
async def select_project(request: Request):
    db = Database.get_database()
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
        project_id = data.get('project_id')
        if project_id is not None:
            g.project_manager = ProjectManager(project_id)
            log.debug("Выбран проект project_id=%d для user_id=%d", project_id, user_id)
        else:
            g.project_manager = ProjectManager()
            log.debug("Очищена выборка проекта для user_id=%d", user_id)
        return {"status": "Project selected"}
    except HTTPException as e:
        log.error("HTTP ошибка в POST /project/select: %s", str(e))
        raise
    except Exception as e:
        g.handle_exception("Ошибка сервера в POST /project/select: ", e)