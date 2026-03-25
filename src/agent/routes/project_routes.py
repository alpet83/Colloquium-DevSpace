# /agent/routes/project_routes.py, updated 2025-07-18 14:28 EEST
from fastapi import APIRouter, Request, HTTPException, Query
from managers.db import Database
from managers.project import ProjectManager
from context_assembler import ContextAssembler
from lib.sandwich_pack import SandwichPack
import globals as g
import json
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


@router.get("/project/file_index")
async def file_index(
    request: Request,
    project_id: int = Query(None),
    modified_since: int = Query(None),
    file_ids: str = Query(None),
    include_size: int = Query(0),
):
    """Lightweight file index with optional filters.

    Selectors (all optional, combinable):
      project_id     — restrict to one project
      modified_since — Unix timestamp; return only files with ts >= value
      file_ids       — comma-separated DB file IDs, e.g. '42,57,103'
      include_size   — set to 1 to include size_bytes (slower: stat() per file)
    """
    db = Database.get_database()
    try:
        session_id = request.cookies.get("session_id")
        if not session_id:
            raise HTTPException(status_code=401, detail="No session")
        user_id = db.fetch_one(
            'SELECT user_id FROM sessions WHERE session_id = :session_id',
            {'session_id': session_id}
        )
        if not user_id:
            raise HTTPException(status_code=401, detail="Invalid session")
        project_id = None if not project_id or project_id <= 0 else project_id
        ids = [int(x.strip()) for x in file_ids.split(',')] if file_ids else None
        result = g.file_manager.file_index(project_id, modified_since, ids, include_size=bool(include_size))
        log.debug(
            "GET /project/file_index: project_id=%s modified_since=%s file_ids=%s include_size=%s → %d entries",
            project_id, modified_since, file_ids, include_size, len(result)
        )
        return result
    except HTTPException:
        raise
    except Exception as e:
        g.handle_exception("Ошибка в GET /project/file_index", e)
        raise


@router.get("/project/code_index")
async def code_index(
    request: Request,
    project_id: int = Query(...),
):
    """Build and return the rich entity index for a project on demand.

    Runs context assembly (assemble_files → SandwichPack.pack) without any
    LLM interaction. Returns the sandwiches_index.jsl format JSON with 'entities'
    (functions, classes, methods) and 'filelist', keyed by file_id.
    """
    try:
        g.check_session(request)
        # Resolve project_name from DB
        pm = ProjectManager(project_id)
        pm.load()
        if pm.project_name is None:
            raise HTTPException(status_code=404, detail=f"Project {project_id} not found")
        project_name = pm.project_name

        # Get all file IDs for the project
        file_entries = g.file_manager.file_index(project_id)
        if not file_entries:
            raise HTTPException(status_code=404, detail=f"No files in project {project_id}")
        file_ids_set = {entry['id'] for entry in file_entries}

        # Build ContentBlock list (same pipeline as LLM context assembly)
        assembler = ContextAssembler()
        file_map = {}
        blocks = assembler.assemble_files(file_ids_set, file_map)
        if not blocks:
            raise HTTPException(status_code=404, detail="No supported files to index in project")

        # Pack → index only (no token limit, compression to get entities)
        packer = SandwichPack(project_name, max_size=10_000_000, compression=True)
        result = packer.pack(blocks)
        entities_count = len(packer.entities) if packer.entities is not None else 0

        log.debug(
            "GET /project/code_index: project_id=%d, project_name=%s, files=%d, blocks=%d, entities=%d",
            project_id, project_name, len(file_ids_set), len(blocks), entities_count
        )
        return json.loads(result['index'])
    except HTTPException:
        raise
    except Exception as e:
        g.handle_exception("Ошибка в GET /project/code_index", e)
        raise