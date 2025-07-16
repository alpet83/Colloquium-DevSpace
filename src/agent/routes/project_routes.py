# /agent/routes/project_routes.py, created 2025-07-16 09:32 EEST
from fastapi import APIRouter, Request, HTTPException
import logging
import globals
from managers.db import Database

router = APIRouter()
SESSION_DB = Database()

@router.get("/project/list")
async def list_projects(request: Request):
    try:
        session_id = request.cookies.get("session_id")
        if not session_id:
            logging.info(f"Отсутствует session_id для IP {request.client.host}")
            raise HTTPException(status_code=401, detail="No session")
        user_id = SESSION_DB.fetch_one(
            'SELECT user_id FROM sessions WHERE session_id = :session_id',
            {'session_id': session_id}
        )
        if not user_id:
            logging.info(f"Неверный session_id для IP {request.client.host}")
            raise HTTPException(status_code=401, detail="Invalid session")
        user_id = user_id[0]
        projects = globals.project_manager.list_projects()
        logging.debug(f"Возвращено {len(projects)} проектов для user_id={user_id}")
        return projects
    except HTTPException as e:
        logging.error(f"HTTP ошибка в GET /project/list: {str(e)}")
        raise
    except Exception as e:
        logging.error(f"Ошибка сервера в GET /project/list: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Server error: {str(e)}")

@router.post("/project/create")
async def create_project(request: Request):
    try:
        session_id = request.cookies.get("session_id")
        if not session_id:
            logging.info(f"Отсутствует session_id для IP {request.client.host}")
            raise HTTPException(status_code=401, detail="No session")
        user_id = SESSION_DB.fetch_one(
            'SELECT user_id FROM sessions WHERE session_id = :session_id',
            {'session_id': session_id}
        )
        if not user_id:
            logging.info(f"Неверный session_id для IP {request.client.host}")
            raise HTTPException(status_code=401, detail="Invalid session")
        user_id = user_id[0]
        data = await request.json()
        project_name = data.get('project_name')
        description = data.get('description', '')
        local_git = data.get('local_git')
        public_git = data.get('public_git')
        dependencies = data.get('dependencies')
        if not project_name:
            logging.info(f"Неверный параметр project_name для IP {request.client.host}")
            raise HTTPException(status_code=400, detail="Missing project_name")
        project_id = globals.project_manager.create_project(project_name, description, local_git, public_git, dependencies)
        logging.debug(f"Создан проект project_id={project_id} для user_id={user_id}")
        return {"project_id": project_id}
    except HTTPException as e:
        logging.error(f"HTTP ошибка в POST /project/create: {str(e)}")
        raise
    except Exception as e:
        logging.error(f"Ошибка сервера в POST /project/create: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Server error: {str(e)}")

@router.post("/project/update")
async def update_project(request: Request):
    try:
        session_id = request.cookies.get("session_id")
        if not session_id:
            logging.info(f"Отсутствует session_id для IP {request.client.host}")
            raise HTTPException(status_code=401, detail="No session")
        user_id = SESSION_DB.fetch_one(
            'SELECT user_id FROM sessions WHERE session_id = :session_id',
            {'session_id': session_id}
        )
        if not user_id:
            logging.info(f"Неверный session_id для IP {request.client.host}")
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
            logging.info(f"Неверные параметры project_id={project_id}, project_name={project_name} для IP {request.client.host}")
            raise HTTPException(status_code=400, detail="Missing project_id or project_name")
        globals.project_manager.db.execute(
            '''
            UPDATE projects SET project_name = :project_name, description = :description,
            local_git = :local_git, public_git = :public_git, dependencies = :dependencies
            WHERE id = :project_id
            ''',
            {
                'project_id': project_id,
                'project_name': project_name,
                'description': description,
                'local_git': local_git,
                'public_git': public_git,
                'dependencies': dependencies
            }
        )
        logging.debug(f"Обновлён проект project_id={project_id} для user_id={user_id}")
        return {"status": "ok"}
    except HTTPException as e:
        logging.error(f"HTTP ошибка в POST /project/update: {str(e)}")
        raise
    except Exception as e:
        logging.error(f"Ошибка сервера в POST /project/update: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Server error: {str(e)}")