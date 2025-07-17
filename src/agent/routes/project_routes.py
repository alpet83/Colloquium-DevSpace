# /agent/routes/project_routes.py, created 2025-07-17 17:03 EEST
import logging
from fastapi import APIRouter, Request, HTTPException
from managers.db import Database
from managers.project import ProjectManager
import globals

router = APIRouter()

@router.get("/project/list")
async def list_projects(request: Request):
    db = Database.get_database()
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
        projects = globals.project_manager.list_projects()
        logging.debug(f"Возвращено {len(projects)} проектов для user_id={user_id[0]}")
        return projects
    except HTTPException as e:
        logging.error(f"HTTP ошибка в GET /project/list: {str(e)}")
        raise
    except Exception as e:
        logging.error(f"Ошибка сервера в GET /project/list: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Server error: {str(e)}")

@router.post("/project/create")
async def create_project(request: Request):
    db = Database.get_database()
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
        data = await request.json()
        project_name = data.get('project_name')
        description = data.get('description', '')
        local_git = data.get('local_git')
        public_git = data.get('public_git')
        dependencies = data.get('dependencies')
        if not project_name:
            logging.info(f"Неверный параметр project_name={project_name} для IP {request.client.host}")
            raise HTTPException(status_code=400, detail="Missing project_name")
        project_id = globals.project_manager.create_project(project_name, description, local_git, public_git, dependencies)
        logging.debug(f"Создан проект project_id={project_id}, project_name={project_name} для user_id={user_id[0]}")
        return {"project_id": project_id}
    except HTTPException as e:
        logging.error(f"HTTP ошибка в POST /project/create: {str(e)}")
        raise
    except Exception as e:
        logging.error(f"Ошибка сервера в POST /project/create: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Server error: {str(e)}")

@router.post("/project/update")
async def update_project(request: Request):
    db = Database.get_database()
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
        data = await request.json()
        project_id = data.get('project_id')
        project_name = data.get('project_name')
        description = data.get('description')
        local_git = data.get('local_git')
        public_git = data.get('public_git')
        dependencies = data.get('dependencies')
        if not project_id or not project_name:
            logging.info(f"Неверные параметры project_id={project_id}, project_name={project_name} для IP {request.client.host}")
            raise HTTPException(status_code=400, detail="Missing project_id or project_name")
        project_manager = ProjectManager(project_id)
        project_manager.update(project_name, description, local_git, public_git, dependencies)
        logging.debug(f"Обновлён проект project_id={project_id}, project_name={project_name} для user_id={user_id[0]}")
        return {"status": "Project updated"}
    except HTTPException as e:
        logging.error(f"HTTP ошибка в POST /project/update: {str(e)}")
        raise
    except Exception as e:
        logging.error(f"Ошибка сервера в POST /project/update: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Server error: {str(e)}")

@router.post("/project/select")
async def select_project(request: Request):
    db = Database.get_database()
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
        project_id = data.get('project_id')
        if project_id is not None:
            globals.project_manager = ProjectManager(project_id)
            logging.debug(f"Selected project project_id={project_id} для user_id={user_id}")
        else:
            globals.project_manager = ProjectManager()
            logging.debug(f"Cleared project selection для user_id={user_id}")
        return {"status": "Project selected"}
    except HTTPException as e:
        logging.error(f"HTTP ошибка в POST /project/select: {str(e)}")
        raise
    except Exception as e:
        logging.error(f"Ошибка сервера в POST /project/select: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Server error: {str(e)}")