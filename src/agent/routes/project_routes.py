# /agent/routes/project_routes.py, updated 2026-03-26 — simplified sync indexing + cache
import json
import re
import time
import asyncio
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Request, HTTPException, Query
from managers.db import Database
from managers.project import ProjectManager
from context_assembler import ContextAssembler
from lib.sandwich_pack import SandwichPack
from fnmatch import fnmatch
import globals as g
from lib.basic_logger import BasicLogger

router = APIRouter()
log = g.get_logger("projectman")

SMART_GREP_MODES = {
    'all': None,
    'code': ['*.py', '*.js', '*.ts', '*.tsx', '*.vue', '*.php', '*.java', '*.go', '*.rs', '*.sh', '*.json', '*.yml', '*.yaml'],
    'logs': ['*.log', '*.out', '*.err', '*.trace', '*.txt', 'logs/*', '*/logs/*'],
    'docs': ['*.md', '*.rst', '*.adoc', '*.txt'],
}

SMART_GREP_PROFILES = {
    'all': None,
    'backend': ['backend/**', 'src/agent/**', 'agent/**', '**/*route*.py', '**/*controller*.*', '**/*service*.*'],
    'frontend': ['frontend/**', 'admin/**', '**/*.vue', '**/*.tsx', '**/*.ts', '**/*.js', '**/*.css'],
    'docs': ['docs/**', '**/*.md', '**/*.rst', '**/*.adoc', 'README*', 'readme*'],
    'infra': ['docker/**', 'scripts/**', '**/Dockerfile*', '**/*.yml', '**/*.yaml', '**/*.toml', '**/*.ini'],
    'tests': ['**/test/**', '**/tests/**', '**/*test*.*', '**/*spec*.*'],
    'logs': ['logs/**', '**/logs/**', '**/*.log', '**/*.out', '**/*.err', '**/*.trace', '**/*.txt'],
}


def project_index_cache_path(project_name: str) -> Path:
    return Path('/app/projects/.cache') / f'{project_name}_index.jsl'


def read_project_cached_index(project_name: str) -> dict | None:
    cache_path = project_index_cache_path(project_name)
    if not cache_path.exists():
        return None
    return json.loads(cache_path.read_text(encoding='utf-8'))


def get_project_index_status(project_id: int, project_name: str) -> dict:
    cache_path = project_index_cache_path(project_name)
    cache_exists = cache_path.exists()
    cache_mtime = int(cache_path.stat().st_mtime) if cache_exists else None
    file_entries = g.file_manager.file_index(project_id)
    latest_file_ts = max((int(entry.get('ts') or 0) for entry in file_entries), default=0) or None
    stale = bool(cache_exists and latest_file_ts and cache_mtime and latest_file_ts > cache_mtime)
    return {
        'project_id': project_id,
        'project_name': project_name,
        'status': 'ready' if cache_exists and not stale else ('stale' if cache_exists else 'missing'),
        'cache_path': str(cache_path),
        'cache_exists': cache_exists,
        'cache_mtime': cache_mtime,
        'latest_file_ts': latest_file_ts,
        'stale': stale,
        'running': False,
        'started_at': None,
        'finished_at': None,
        'error': None,
        'files': None,
        'blocks': None,
        'entities': None,
    }


def _resolve_project(project_id: int) -> tuple[ProjectManager, str]:
    pm = ProjectManager.get(project_id)
    if pm is None or pm.project_name is None:
        raise HTTPException(status_code=404, detail=f"Project {project_id} not found")
    return pm, pm.project_name


def _build_project_index_sync(project_id: int, project_name: str) -> tuple[dict, int, int, int, str]:
    file_entries = g.file_manager.file_index(project_id)
    if not file_entries:
        raise HTTPException(status_code=404, detail=f"No files in project {project_id}")
    file_ids_set = {entry['id'] for entry in file_entries}

    assembler = ContextAssembler()
    file_map = {}
    blocks = assembler.assemble_files(file_ids_set, file_map)
    if not blocks:
        raise HTTPException(status_code=404, detail="No supported files to index in project")

    packer = SandwichPack(project_name, max_size=10_000_000, compression=True)
    result = packer.pack(blocks)
    entities_count = len(packer.entities) if packer.entities is not None else 0

    cache_path = project_index_cache_path(project_name)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(result['index'], encoding='utf-8')

    return json.loads(result['index']), len(file_ids_set), len(blocks), entities_count, str(cache_path)


def _is_mode_match(file_name: str, mode: str) -> bool:
    mode = mode if mode in SMART_GREP_MODES else 'code'
    globs = SMART_GREP_MODES.get(mode)
    if not globs:
        return True
    path = file_name.replace('\\', '/').lstrip('/')
    return any(fnmatch(path, p) for p in globs)


def _is_profile_match(file_name: str, profile: str) -> bool:
    profile = profile if profile in SMART_GREP_PROFILES else 'all'
    globs = SMART_GREP_PROFILES.get(profile)
    if not globs:
        return True
    path = file_name.replace('\\', '/').lstrip('/')
    return any(fnmatch(path, p) for p in globs)


def _project_name_map() -> dict:
    db = Database.get_database()
    rows = db.fetch_all('SELECT id, project_name FROM projects', {})
    return {row[0]: row[1] for row in rows}


def _sort_tree_nodes(nodes: dict) -> dict:
    sorted_items = sorted(
        nodes.items(),
        key=lambda item: (0 if item[1].get('type') == 'directory' else 1, item[0].lower())
    )
    result = {}
    for name, node in sorted_items:
        if node.get('type') == 'directory':
            node['children'] = _sort_tree_nodes(node.get('children', {}))
        result[name] = node
    return result


def _build_tree_nodes(file_entries: list, base_path: str, depth: int, project_id: int | None) -> dict:
    root = {}
    normalized_base = str(base_path or '').lstrip('/').rstrip('/')
    normalized_prefix = f'{normalized_base}/' if normalized_base else ''

    for entry in file_entries:
        file_name = str(entry.get('file_name') or '').lstrip('/')
        if normalized_prefix:
            if not file_name.startswith(normalized_prefix):
                continue
            relative_name = file_name[len(normalized_prefix):]
        else:
            relative_name = file_name

        parts = [part for part in relative_name.split('/') if part]
        if not parts:
            continue

        node = root
        for index, part in enumerate(parts):
            is_terminal = index == len(parts) - 1
            at_boundary = index == depth - 1

            if is_terminal and index < depth:
                node.setdefault(part, {
                    'type': 'file',
                    'id': entry.get('id'),
                    'path': file_name,
                    'project_id': project_id,
                })
                break

            full_path = normalized_prefix + '/'.join(parts[:index + 1])
            dir_node = node.get(part)
            if not dir_node or dir_node.get('type') != 'directory':
                dir_node = {
                    'type': 'directory',
                    'children': {},
                    'path': f'{full_path}/',
                    'project_id': project_id,
                    'has_more': False,
                    'isLoaded': not at_boundary,
                }
                node[part] = dir_node

            if at_boundary:
                dir_node['has_more'] = True
                dir_node['isLoaded'] = False
                break

            node = dir_node['children']

    return _sort_tree_nodes(root)


def _parse_dt_to_ts(value: str) -> int:
    value = value.strip()
    if re.fullmatch(r'\d{10,13}', value):
        iv = int(value)
        return iv // 1000 if iv > 2_000_000_000_000 else iv

    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%Y-%m-%d'):
        try:
            dt = datetime.strptime(value, fmt)
            return int(dt.timestamp())
        except ValueError:
            continue
    raise ValueError(
        "Unsupported datetime format. Use YYYY-MM-DD or YYYY-MM-DD HH:MM[:SS] or unix timestamp"
    )


def _parse_time_strict(expr: str) -> tuple[str, str, int]:
    m = re.match(r'^\s*(mtime|ctime|ts)\s*(>=|<=|>|<|=)\s*(.+?)\s*$', expr or '', re.IGNORECASE)
    if not m:
        raise ValueError("Invalid time_strict. Example: mtime>2026-03-25 21:00")
    field = m.group(1).lower()
    op = m.group(2)
    rhs = _parse_dt_to_ts(m.group(3))
    return field, op, rhs


def _cmp(left: int, op: str, right: int) -> bool:
    if op == '>':
        return left > right
    if op == '>=':
        return left >= right
    if op == '<':
        return left < right
    if op == '<=':
        return left <= right
    return left == right


def _scan_state_store() -> dict:
    state = getattr(g, 'project_scan_state', None)
    if not isinstance(state, dict):
        state = {}
        g.project_scan_state = state
    return state


def _scan_state(project_id: int, project_name: str | None = None) -> dict:
    state = _scan_state_store().get(project_id, {})
    return {
        'project_id': project_id,
        'project_name': project_name,
        'stale': bool(state.get('stale', True)),
        'running': bool(state.get('running', False)),
        'reason': state.get('reason'),
        'updated_at': state.get('updated_at'),
        'started_at': state.get('started_at'),
        'finished_at': state.get('finished_at'),
        'duration_sec': state.get('duration_sec'),
        'files_count': state.get('files_count'),
        'error': state.get('error'),
    }


async def _run_project_scan_refresh(project_id: int):
    state = _scan_state_store()
    current = state.get(project_id, {})
    current.update({
        'running': True,
        'error': None,
        'started_at': int(time.time()),
    })
    state[project_id] = current

    try:
        pm = ProjectManager.get(project_id)
        if pm is None or pm.project_name is None:
            raise ValueError(f"Project {project_id} not found")
        await asyncio.to_thread(pm.scan_project_files)
        fresh = _scan_state_store().get(project_id, {})
        fresh.update({
            'running': False,
            'finished_at': int(time.time()),
            'error': None,
        })
        state[project_id] = fresh
    except Exception as e:
        failed = _scan_state_store().get(project_id, {})
        failed.update({
            'running': False,
            'finished_at': int(time.time()),
            'error': str(e),
            'stale': True,
        })
        state[project_id] = failed
        log.excpt("Ошибка refresh scan для project_id=%d: ", project_id, e=e)

@router.get("/project/list")
async def list_projects(request: Request):
    db = Database.get_database()
    try:
        session_id = request.cookies.get("session_id")
        if not session_id:
            log.info(g.with_session_tag(request, "Отсутствует session_id для IP=%s"), request.client.host)
            raise HTTPException(status_code=401, detail="No session")
        user_id = db.fetch_one(
            'SELECT user_id FROM sessions WHERE session_id = :session_id',
            {'session_id': session_id}
        )
        if not user_id:
            log.info(g.with_session_tag(request, "Неверный session_id для IP=%s"), request.client.host)
            raise HTTPException(status_code=401, detail="Invalid session")
        user_id = user_id[0]
        projects = g.project_manager.list_projects()
        log.debug(g.with_session_tag(request, "Возвращено %d проектов для user_id=%d"), len(projects), user_id)
        return projects
    except HTTPException as e:
        log.error(g.with_session_tag(request, "HTTP ошибка в GET /project/list: %s"), str(e))
        raise
    except Exception as e:
        g.handle_exception("Ошибка сервера в GET /project/list: ", e)

@router.post("/project/create")
async def create_project(request: Request):
    db = Database.get_database()
    try:
        session_id = request.cookies.get("session_id")
        if not session_id:
            log.info(g.with_session_tag(request, "Отсутствует session_id для IP=%s"), request.client.host)
            raise HTTPException(status_code=401, detail="No session")
        user_id = db.fetch_one(
            'SELECT user_id FROM sessions WHERE session_id = :session_id',
            {'session_id': session_id}
        )
        if not user_id:
            log.info(g.with_session_tag(request, "Неверный session_id для IP=%s"), request.client.host)
            raise HTTPException(status_code=401, detail="Invalid session")
        user_id = user_id[0]
        data = await request.json()
        project_name = data.get('project_name')
        description = data.get('description', '')
        local_git = data.get('local_git')
        public_git = data.get('public_git')
        dependencies = data.get('dependencies')
        if not project_name:
            log.info(g.with_session_tag(request, "Неверный параметр project_name=%s для IP=%s"), str(project_name), request.client.host)
            raise HTTPException(status_code=400, detail="Missing project_name")
        project_id = g.project_manager.create_project(project_name, description, local_git, public_git, dependencies)
        log.debug(g.with_session_tag(request, "Создан проект project_id=%d, project_name=%s для user_id=%d"), project_id, project_name, user_id)
        return {"project_id": project_id}
    except HTTPException as e:
        log.error(g.with_session_tag(request, "HTTP ошибка в POST /project/create: %s"), str(e))
        raise
    except Exception as e:
        g.handle_exception("Ошибка сервера в POST /project/create: ", e)

@router.post("/project/update")
async def update_project(request: Request):
    db = Database.get_database()
    try:
        session_id = request.cookies.get("session_id")
        if not session_id:
            log.info(g.with_session_tag(request, "Отсутствует session_id для IP=%s"), request.client.host)
            raise HTTPException(status_code=401, detail="No session")
        user_id = db.fetch_one(
            'SELECT user_id FROM sessions WHERE session_id = :session_id',
            {'session_id': session_id}
        )
        if not user_id:
            log.info(g.with_session_tag(request, "Неверный session_id для IP=%s"), request.client.host)
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
            log.info(g.with_session_tag(request, "Неверные параметры project_id=%s, project_name=%s для IP=%s"),
                     str(project_id), str(project_name), request.client.host)
            raise HTTPException(status_code=400, detail="Missing project_id or project_name")
        project_manager = ProjectManager.get(project_id)
        if project_manager is None:
            raise HTTPException(status_code=404, detail=f"Project {project_id} not found")
        project_manager.update(project_name, description, local_git, public_git, dependencies)
        log.debug(g.with_session_tag(request, "Обновлён проект project_id=%d, project_name=%s для user_id=%d"), project_id, project_name, user_id)
        return {"status": "Project updated"}
    except HTTPException as e:
        log.error(g.with_session_tag(request, "HTTP ошибка в POST /project/update: %s"), str(e))
        raise
    except Exception as e:
        g.handle_exception("Ошибка сервера в POST /project/update: ", e)

@router.post("/project/select")
async def select_project(request: Request):
    db = Database.get_database()
    try:
        session_id = request.cookies.get("session_id")
        if not session_id:
            log.info(g.with_session_tag(request, "Отсутствует session_id для IP=%s"), request.client.host)
            raise HTTPException(status_code=401, detail="No session")
        user_id = db.fetch_one(
            'SELECT user_id FROM sessions WHERE session_id = :session_id',
            {'session_id': session_id}
        )
        if not user_id:
            log.info(g.with_session_tag(request, "Неверный session_id для IP=%s"), request.client.host)
            raise HTTPException(status_code=401, detail="Invalid session")
        user_id = user_id[0]
        data = await request.json()
        project_id = data.get('project_id')
        if project_id is not None:
            g.chat_manager.select_project(session_id, user_id, int(project_id))
            log.debug(g.with_session_tag(request, "Выбран проект project_id=%d для session_id=%s, user_id=%d"), project_id, session_id, user_id)
        else:
            g.chat_manager.select_project(session_id, user_id, None)
            log.debug(g.with_session_tag(request, "Очищена выборка проекта для session_id=%s, user_id=%d"), session_id, user_id)
        return {"status": "Project selected"}
    except HTTPException as e:
        log.error(g.with_session_tag(request, "HTTP ошибка в POST /project/select: %s"), str(e))
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
            g.with_session_tag(request, "GET /project/file_index: project_id=%s modified_since=%s file_ids=%s include_size=%s → %d entries"),
            project_id, modified_since, file_ids, include_size, len(result)
        )
        return result
    except HTTPException:
        raise
    except Exception as e:
        g.handle_exception("Ошибка в GET /project/file_index", e)
        raise


@router.get("/project/file_tree")
async def file_tree(
    request: Request,
    project_id: int = Query(None),
    path: str = Query(''),
    depth: int = Query(3),
):
    started = time.monotonic()
    try:
        g.check_session(request)
        effective_project_id = None if project_id is None or project_id < 0 else project_id
        normalized_path = str(path or '').lstrip('/').rstrip('/')
        depth = max(1, min(int(depth or 3), 6))

        file_entries = g.file_manager.file_index(effective_project_id)
        grouped = {}
        for entry in file_entries:
            proj_id = entry.get('project_id')
            grouped.setdefault(proj_id, []).append(entry)

        project_names = _project_name_map()
        trees = []
        for grouped_project_id, entries in grouped.items():
            nodes = _build_tree_nodes(entries, normalized_path, depth, grouped_project_id)
            if not nodes:
                continue
            if grouped_project_id == 0:
                project_name = '.chat-meta'
            elif grouped_project_id is None:
                project_name = 'Global'
            else:
                project_name = project_names.get(grouped_project_id) or f'project_{grouped_project_id}'
            trees.append({
                'project_id': grouped_project_id,
                'project_name': project_name,
                'path': normalized_path,
                'nodes': nodes,
            })

        trees.sort(key=lambda tree: ((tree.get('project_id') or 0) <= 0, str(tree.get('project_name') or '').lower()))
        duration = time.monotonic() - started
        if duration >= 2:
            log.warn(
                g.with_session_tag(request, 'PERF_WARN GET /project/file_tree project_id=%s path=%s depth=%s took=%.2fs trees=%d'),
                str(effective_project_id), normalized_path or '/', depth, duration, len(trees)
            )
        else:
            log.debug(
                g.with_session_tag(request, 'GET /project/file_tree project_id=%s path=%s depth=%s took=%.2fs trees=%d'),
                str(effective_project_id), normalized_path or '/', depth, duration, len(trees)
            )
        return {
            'project_id': effective_project_id,
            'path': normalized_path,
            'depth': depth,
            'trees': trees,
        }
    except HTTPException:
        raise
    except Exception as e:
        g.handle_exception('Ошибка в GET /project/file_tree', e)
        raise


@router.get("/project/scan_state")
async def project_scan_state(request: Request, project_id: int = Query(...)):
    try:
        g.check_session(request)
        pm = ProjectManager.get(project_id)
        if pm is None or pm.project_name is None:
            raise HTTPException(status_code=404, detail=f"Project {project_id} not found")
        return _scan_state(project_id, pm.project_name)
    except HTTPException:
        raise
    except Exception as e:
        g.handle_exception("Ошибка в GET /project/scan_state", e)
        raise


@router.post("/project/scan_refresh")
async def project_scan_refresh(request: Request):
    try:
        g.check_session(request)
        data = await request.json()
        project_id = int(data.get('project_id') or 0)
        background = bool(data.get('background', True))
        if project_id <= 0:
            raise HTTPException(status_code=400, detail="Missing or invalid project_id")

        pm = ProjectManager.get(project_id)
        if pm is None or pm.project_name is None:
            raise HTTPException(status_code=404, detail=f"Project {project_id} not found")

        state = _scan_state_store()
        current = state.get(project_id, {})
        if current.get('running'):
            return _scan_state(project_id, pm.project_name)

        if background:
            asyncio.create_task(_run_project_scan_refresh(project_id), name=f"scan-refresh-{project_id}")
            current = state.get(project_id, {})
            current.update({
                'running': True,
                'started_at': int(time.time()),
                'error': None,
            })
            state[project_id] = current
            return _scan_state(project_id, pm.project_name)

        started = time.monotonic()
        pm.scan_project_files()
        done = _scan_state_store().get(project_id, {})
        done.update({
            'running': False,
            'finished_at': int(time.time()),
            'duration_sec': round(time.monotonic() - started, 3),
            'error': None,
        })
        state[project_id] = done
        return _scan_state(project_id, pm.project_name)
    except HTTPException:
        raise
    except Exception as e:
        g.handle_exception("Ошибка в POST /project/scan_refresh", e)
        raise


@router.get("/project/code_index")
def code_index(
    request: Request,
    project_id: int = Query(...),
    timeout: int = Query(default=300, description="Max seconds the caller is willing to wait (informational — not enforced server-side; enforced by nginx/proxy)."),
):
    """Build and return the rich entity index for a project synchronously.

    Always saves result to cache file for MCP-tool to read.
    Background queuing handled by MCP-tool (background=1 parameter no longer needed).
    `timeout` is a client hint; actual cutoff is the nginx proxy_read_timeout.
    """
    try:
        g.check_session(request)
        _, project_name = _resolve_project(project_id)
        index_data, files_count, blocks_count, entities_count, cache_path = _build_project_index_sync(
            project_id,
            project_name,
        )

        log.debug(
            g.with_session_tag(request, "GET /project/code_index: project_id=%d, project_name=%s, timeout=%ds, files=%d, blocks=%d, entities=%d, cache=%s"),
            project_id, project_name, timeout, files_count, blocks_count, entities_count, cache_path
        )
        return index_data
    except HTTPException:
        raise
    except Exception as e:
        g.handle_exception("Ошибка в GET /project/code_index", e)
        raise


@router.post("/project/exec")
async def exec_project_command(request: Request):
    """Execute a shell command in a project's working directory. Returns stdout/stderr without LLM involvement."""
    try:
        user_id = g.check_session(request)
        data = await request.json()
        project_id = data.get('project_id')
        command = data.get('command', '').strip()
        timeout = int(data.get('timeout', 30))
        if not command:
            log.info(g.with_session_tag(request, "Пустая команда в POST /project/exec для user_id=%d"), user_id)
            raise HTTPException(status_code=400, detail="Missing command")
        timeout = min(max(timeout, 1), 300)
        pm = ProjectManager.get(project_id)
        if pm is None or pm.project_name is None:
            log.info(g.with_session_tag(request, "Проект не найден project_id=%s для user_id=%d"), str(project_id), user_id)
            raise HTTPException(status_code=404, detail=f"Project {project_id} not found")
        project_dir = f'/app/projects/{pm.project_name}'
        log.info(g.with_session_tag(request, "POST /project/exec project=%s cmd=%s"), pm.project_name, command[:60])
        from lib.execute_commands import execute as shell_execute
        result = await shell_execute(command, [], 'mcp_exec', cwd=project_dir, timeout=timeout)
        log.debug(g.with_session_tag(request, "exec status=%s output_len=%d"), result["status"], len(result["message"]))
        return {"status": result["status"], "output": result["message"], "project": pm.project_name}
    except HTTPException:
        raise
    except Exception as e:
        g.handle_exception("Ошибка в POST /project/exec", e)
        raise


@router.post("/project/smart_grep")
async def smart_grep(request: Request):
    """Search query across predefined file sets in a project (code/logs/docs/all)."""
    try:
        user_id = g.check_session(request)
        data = await request.json()
        project_id = int(data.get('project_id') or 0)
        query = str(data.get('query', '')).strip()
        mode = str(data.get('mode', 'code')).strip().lower()
        profile = str(data.get('profile', 'all')).strip().lower()
        is_regex = bool(data.get('is_regex', False))
        case_sensitive = bool(data.get('case_sensitive', False))
        max_results = min(max(int(data.get('max_results', 100)), 1), 500)
        context_lines = min(max(int(data.get('context_lines', 0)), 0), 3)
        time_strict = str(data.get('time_strict', '') or '').strip()
        include_glob = data.get('include_glob') or []
        if isinstance(include_glob, str):
            include_glob = [x.strip() for x in include_glob.split(',') if x.strip()]

        if project_id <= 0:
            raise HTTPException(status_code=400, detail="Missing or invalid project_id")
        if not query:
            raise HTTPException(status_code=400, detail="Missing query")
        if profile not in SMART_GREP_PROFILES:
            raise HTTPException(status_code=400, detail=f"Unknown profile '{profile}'")

        time_filter = None
        if time_strict:
            try:
                time_filter = _parse_time_strict(time_strict)
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))

        pm = ProjectManager.get(project_id)
        if pm is None:
            raise HTTPException(status_code=404, detail=f"Project {project_id} not found")

        entries = g.file_manager.file_index(project_id)
        hits = []
        truncated = False
        flags = 0 if case_sensitive else re.IGNORECASE
        pattern = re.compile(query, flags) if is_regex else None

        for entry in entries:
            file_id = entry['id']
            file_name = entry['file_name']
            if not _is_mode_match(file_name, mode):
                continue
            if not _is_profile_match(file_name, profile):
                continue
            if include_glob and not any(fnmatch(file_name, gpat) for gpat in include_glob):
                continue

            if time_filter:
                field, op, rhs = time_filter
                lhs = None
                if field in ('mtime', 'ts'):
                    lhs = int(entry.get('ts') or 0)
                elif field == 'ctime':
                    try:
                        qfn = pm.locate_file(file_name, project_id)
                        if qfn and qfn.exists():
                            lhs = int(qfn.stat().st_ctime)
                    except Exception:
                        lhs = None
                if lhs is None or not _cmp(lhs, op, rhs):
                    continue

            file_data = g.file_manager.get_file(file_id)
            if not file_data or file_data.get('content') is None:
                continue
            lines = str(file_data.get('content') or '').splitlines()

            for i, line in enumerate(lines, start=1):
                if pattern is not None:
                    m = pattern.search(line)
                    matched = m is not None
                    matched_text = m.group(0)[:200] if m else ''
                else:
                    haystack = line if case_sensitive else line.lower()
                    needle = query if case_sensitive else query.lower()
                    matched = needle in haystack
                    matched_text = query[:200] if matched else ''

                if not matched:
                    continue

                before = lines[max(0, i - 1 - context_lines):i - 1] if context_lines else []
                after = lines[i:i + context_lines] if context_lines else []
                hits.append({
                    'file_id': file_id,
                    'file_name': file_name,
                    'line': i,
                    'line_text': line[:400],
                    'match': matched_text,
                    'context_before': before,
                    'context_after': after,
                })
                if len(hits) >= max_results:
                    truncated = True
                    break

            if truncated:
                break

        log.debug(
            g.with_session_tag(request, "POST /project/smart_grep user_id=%d project_id=%d mode=%s profile=%s regex=%s time_strict=%s results=%d truncated=%s"),
            user_id, project_id, mode, profile, str(is_regex), time_strict or '-', len(hits), str(truncated)
        )
        return {
            'status': 'ok',
            'project_id': project_id,
            'mode': mode,
            'profile': profile,
            'query': query,
            'is_regex': is_regex,
            'case_sensitive': case_sensitive,
            'time_strict': time_strict or None,
            'total': len(hits),
            'truncated': truncated,
            'hits': hits,
        }
    except re.error as e:
        raise HTTPException(status_code=400, detail=f"Invalid regex: {str(e)}")
    except HTTPException:
        raise
    except Exception as e:
        g.handle_exception("Ошибка в POST /project/smart_grep", e)
        raise


@router.post("/project/replace")
async def replace_in_file(request: Request):
    """Replace text (plain or regex) in a specific file by file_id inside one project."""
    try:
        user_id = g.check_session(request)
        data = await request.json()
        project_id = int(data.get('project_id') or 0)
        file_id = int(data.get('file_id') or 0)
        old = str(data.get('old', ''))
        new = str(data.get('new', ''))
        is_regex = bool(data.get('is_regex', False))
        case_sensitive = bool(data.get('case_sensitive', True))
        max_replacements = int(data.get('max_replacements', 0) or 0)
        if project_id <= 0 or file_id <= 0:
            raise HTTPException(status_code=400, detail="Missing or invalid project_id/file_id")
        if old == '':
            raise HTTPException(status_code=400, detail="Missing old pattern")

        file_row = g.file_manager.file_index(project_id, file_ids=[file_id])
        if not file_row:
            raise HTTPException(status_code=404, detail=f"file_id={file_id} not found in project_id={project_id}")

        file_data = g.file_manager.get_file(file_id)
        if not file_data or file_data.get('content') is None:
            raise HTTPException(status_code=404, detail="File content not available")
        original = str(file_data.get('content') or '')

        if is_regex:
            flags = 0 if case_sensitive else re.IGNORECASE
            pattern = re.compile(old, flags)
            updated, replaced = pattern.subn(new, original, count=max_replacements if max_replacements > 0 else 0)
        else:
            replaced = original.count(old)
            if max_replacements > 0:
                replaced = min(replaced, max_replacements)
                updated = original.replace(old, new, max_replacements)
            else:
                updated = original.replace(old, new)

        if replaced == 0:
            return {
                'status': 'no_changes',
                'project_id': project_id,
                'file_id': file_id,
                'replaced': 0,
            }

        rc = g.file_manager.update_file(file_id, updated, timestamp=int(time.time()), project_id=project_id)
        if rc <= 0:
            raise HTTPException(status_code=500, detail=f"Failed to update file: code {rc}")

        log.info(
            g.with_session_tag(request, "POST /project/replace user_id=%d project_id=%d file_id=%d replaced=%d regex=%s"),
            user_id, project_id, file_id, replaced, str(is_regex)
        )
        return {
            'status': 'ok',
            'project_id': project_id,
            'file_id': file_id,
            'replaced': replaced,
            'is_regex': is_regex,
            'case_sensitive': case_sensitive,
            'max_replacements': max_replacements,
            'bytes_before': len(original),
            'bytes_after': len(updated),
        }
    except re.error as e:
        raise HTTPException(status_code=400, detail=f"Invalid regex: {str(e)}")
    except HTTPException:
        raise
    except Exception as e:
        g.handle_exception("Ошибка в POST /project/replace", e)
        raise