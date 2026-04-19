# /agent/routes/project_routes.py, updated 2026-03-26 — simplified sync indexing + cache
import copy
import json
import os
import re
import time
import asyncio
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Request, HTTPException, Query
from managers.db import Database
from managers.project import ProjectManager
from managers.runtime_config import get_int
from context_assembler import ContextAssembler
from lib.sandwich_pack import SandwichPack
from fnmatch import fnmatch
import globals as g
from lib.basic_logger import BasicLogger
from lib.smart_grep_scope_cache import filters_fingerprint, get_scope_cache, normalize_path_prefix
from lib import maint_pool as maint_pool_lib
from lib.background_task_registry import get_background_task_registry
from lib.code_index_incremental import (
    attach_full_metadata,
    compute_dirty,
    env_dirty_use_size,
    env_incremental_enabled,
    merge_index,
    need_fingerprint_seed,
    should_force_full,
    stamp_rebuild_duration,
    validate_cache,
)

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
    # Do not call file_index(verify_links=True) here: it touches disk per row and blocks the event loop
    # at tens of thousands of links (e.g. after maint). MAX(ts) matches the active-link filter in file_index.
    raw_ts = g.file_manager.active_files_latest_ts(project_id)
    latest_file_ts = int(raw_ts) if raw_ts else None
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


def _write_project_index_cache(project_name: str, index_dict: dict) -> str:
    cache_path = project_index_cache_path(project_name)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(index_dict, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(cache_path)


def _build_project_index_full(project_id: int, project_name: str) -> tuple[dict, int, int, int, str]:
    """Полный scan: все файлы проекта → pack → кеш с file_fingerprints и rebuild_revision=0."""
    t0 = time.monotonic()
    file_entries = g.file_manager.file_index(project_id)
    if not file_entries:
        raise HTTPException(status_code=404, detail=f"No files in project {project_id}")
    file_ids_set = {entry["id"] for entry in file_entries}

    assembler = ContextAssembler()
    file_map = {}
    blocks = assembler.assemble_files(file_ids_set, file_map)
    if not blocks:
        raise HTTPException(status_code=404, detail="No supported files to index in project")

    packer = SandwichPack(project_name, max_size=10_000_000, compression=True)
    result = packer.pack(blocks)
    entities_count = len(packer.entities) if packer.entities is not None else 0
    index_dict = json.loads(result["index"])
    index_dict = attach_full_metadata(
        index_dict, file_entries, duration_sec=time.monotonic() - t0
    )
    cache_path = _write_project_index_cache(project_name, index_dict)
    return index_dict, len(file_ids_set), len(blocks), entities_count, cache_path


def _build_project_index_sync(project_id: int, project_name: str, *, force_full: bool = False) -> tuple[dict, int, int, int, str]:
    """Полный или инкрементальный ребилд кеша индекса (sandwiches_index)."""
    if force_full or not env_incremental_enabled():
        return _build_project_index_full(project_id, project_name)

    cached = read_project_cached_index(project_name)
    if cached is None or not validate_cache(cached):
        return _build_project_index_full(project_id, project_name)

    if need_fingerprint_seed(cached):
        log.info("project index: no file_fingerprints in cache, full rebuild project_id=%s", project_id)
        return _build_project_index_full(project_id, project_name)

    max_rev = env_max_incremental_revisions()
    if should_force_full(cached, max_rev):
        log.info(
            "project index: rebuild_revision cap (%s), full rebuild project_id=%s",
            max_rev,
            project_id,
        )
        return _build_project_index_full(project_id, project_name)

    use_size = env_dirty_use_size()
    file_entries = g.file_manager.file_index(project_id, include_size=use_size)
    if not file_entries:
        raise HTTPException(status_code=404, detail=f"No files in project {project_id}")

    dirty, removed = compute_dirty(cached, file_entries, use_size=use_size)
    t_step = time.monotonic()

    if not dirty and not removed:
        path = str(project_index_cache_path(project_name))
        n_ent = len(cached.get("entities") or [])
        out = copy.deepcopy(cached)
        stamp_rebuild_duration(out, time.monotonic() - t_step)
        return out, len(file_entries), 0, n_ent, path

    if dirty:
        assembler = ContextAssembler()
        file_map = {}
        blocks = assembler.assemble_files(set(dirty), file_map)
        assembled_ids = {getattr(b, "file_id", None) for b in blocks if getattr(b, "file_id", None) is not None}
        if not blocks or assembled_ids != dirty:
            log.info(
                "project index: incremental assemble mismatch or empty (dirty=%s assembled=%s), full rebuild project_id=%s",
                sorted(dirty),
                sorted(assembled_ids) if blocks else [],
                project_id,
            )
            return _build_project_index_full(project_id, project_name)

        packer = SandwichPack(project_name, max_size=10_000_000, compression=True)
        result = packer.pack(blocks)
        partial = json.loads(result["index"])
        try:
            new_rev = int(cached.get("rebuild_revision", 0)) + 1
        except (TypeError, ValueError):
            new_rev = 1
        merged = merge_index(
            cached,
            partial,
            dirty_ids=dirty,
            removed_ids=removed,
            file_entries=file_entries,
            new_revision=new_rev,
            duration_sec=time.monotonic() - t_step,
        )
        cache_path = _write_project_index_cache(project_name, merged)
        n_ent = len(merged.get("entities") or [])
        return merged, len(file_entries), len(blocks), n_ent, cache_path

    try:
        new_rev = int(cached.get("rebuild_revision", 0)) + 1
    except (TypeError, ValueError):
        new_rev = 1
    merged = merge_index(
        cached,
        None,
        dirty_ids=set(),
        removed_ids=removed,
        file_entries=file_entries,
        new_revision=new_rev,
        duration_sec=time.monotonic() - t_step,
    )
    cache_path = _write_project_index_cache(project_name, merged)
    n_ent = len(merged.get("entities") or [])
    return merged, len(file_entries), 0, n_ent, cache_path


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


def _file_tree_build_sync(effective_project_id, normalized_path: str, depth: int) -> dict:
    """Синхронная сборка дерева (индекс из БД + разбор путей). Вызывать из пула через asyncio.to_thread.

    Ленивое дерево на фронте: depth — относительно текущего path. В БД хранится path_seg_count
    (число сегментов пути от корня репозитория); граница base+depth даёт два среза:
    «мелкий» (полные строки, path_seg_count <= base+depth) и «глубокий» (только имена, > bound)
    для корректных has_more без загрузки тысяч полных записей для мелкой части.

    verify_links отключён; missing_ttl в SQL + фоновые scan/check.
    """
    profile = os.getenv("CORE_PROFILE_FILE_TREE", "").strip() in ("1", "true", "yes")
    t0 = time.perf_counter()
    path_prefix = normalized_path.strip("/") if (normalized_path or "").strip() else None
    base_segs = len([p for p in str(normalized_path or "").replace("\\", "/").strip("/").split("/") if p])
    bound = base_segs + int(depth)

    g.file_manager.ensure_tree_segments()

    t_idx0 = time.perf_counter()
    entries_shallow = g.file_manager.file_index(
        effective_project_id,
        verify_links=False,
        path_prefix=path_prefix,
        max_segments=bound,
    )
    t_idx1 = time.perf_counter()
    entries_deep = g.file_manager.file_index(
        effective_project_id,
        verify_links=False,
        path_prefix=path_prefix,
        min_segments=bound,
        name_only=True,
    )
    t_idx2 = time.perf_counter()
    file_entries = entries_shallow + entries_deep
    t1 = time.perf_counter()
    grouped: dict = {}
    for entry in file_entries:
        proj_id = entry.get('project_id')
        grouped.setdefault(proj_id, []).append(entry)

    project_names = _project_name_map()
    t2 = time.perf_counter()
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
    t3 = time.perf_counter()
    if profile:
        log.info(
            "CORE_PROFILE_FILE_TREE shallow_ms=%.1f deep_ms=%.1f total_index_ms=%.1f group_map_ms=%.1f build_trees_ms=%.1f "
            "shallow_n=%d deep_n=%d total_n=%d bound=%d base_segs=%d trees=%d path_prefix=%r",
            (t_idx1 - t_idx0) * 1000.0,
            (t_idx2 - t_idx1) * 1000.0,
            (t1 - t0) * 1000.0,
            (t2 - t1) * 1000.0,
            (t3 - t2) * 1000.0,
            len(entries_shallow),
            len(entries_deep),
            len(file_entries),
            bound,
            base_segs,
            len(trees),
            path_prefix,
        )
    return {
        'project_id': effective_project_id,
        'path': normalized_path,
        'depth': depth,
        'trees': trees,
    }


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


def _chunk_limit_files_cap() -> int:
    return get_int("CQDS_SMART_GREP_CHUNK_LIMIT_FILES", 100, 1, 200)


def _chunk_max_hits_cap() -> int:
    return get_int("CQDS_SMART_GREP_CHUNK_MAX_HITS", 2000, 1, 50_000)


_NEXT_FILE_IDS_CAP = 500


def _path_prefix_matches(file_name: str, path_prefix_norm: str) -> bool:
    fn = str(file_name or "").replace("\\", "/").lstrip("/")
    if not path_prefix_norm:
        return True
    if fn == path_prefix_norm:
        return True
    return fn.startswith(path_prefix_norm + "/")


def _filter_entries_for_smart_grep(
    entries: list,
    pm: ProjectManager,
    project_id: int,
    mode: str,
    profile: str,
    include_glob: list,
    time_filter,
    path_prefix_norm: str,
) -> list:
    out = []
    for entry in entries:
        file_name = entry.get("file_name") or ""
        if not _path_prefix_matches(file_name, path_prefix_norm):
            continue
        if not _is_mode_match(file_name, mode):
            continue
        if not _is_profile_match(file_name, profile):
            continue
        if include_glob and not any(fnmatch(file_name, gpat) for gpat in include_glob):
            continue
        if time_filter:
            field, op, rhs = time_filter
            lhs = None
            if field in ("mtime", "ts"):
                lhs = int(entry.get("ts") or 0)
            elif field == "ctime":
                try:
                    qfn = pm.locate_file(file_name, project_id)
                    if qfn and qfn.exists():
                        lhs = int(qfn.stat().st_ctime)
                except Exception:
                    lhs = None
            if lhs is None or not _cmp(lhs, op, rhs):
                continue
        out.append(entry)
    out.sort(key=lambda e: int(e.get("id") or 0))
    return out


def _grep_hits_one_file(
    entry: dict,
    pm: ProjectManager,
    project_id: int,
    query: str,
    pattern: re.Pattern | None,
    is_regex: bool,
    case_sensitive: bool,
    context_lines: int,
) -> list[dict]:
    file_id = entry["id"]
    file_name = entry["file_name"]
    file_data = g.file_manager.get_file(file_id)
    if not file_data or file_data.get("content") is None:
        return []
    lines = str(file_data.get("content") or "").splitlines()
    hits = []
    for i, line in enumerate(lines, start=1):
        if pattern is not None:
            m = pattern.search(line)
            matched = m is not None
            matched_text = m.group(0)[:200] if m else ""
        else:
            haystack = line if case_sensitive else line.lower()
            needle = query if case_sensitive else query.lower()
            matched = needle in haystack
            matched_text = query[:200] if matched else ""
        if not matched:
            continue
        before = lines[max(0, i - 1 - context_lines) : i - 1] if context_lines else []
        after = lines[i : i + context_lines] if context_lines else []
        hits.append(
            {
                "file_id": file_id,
                "file_name": file_name,
                "line": i,
                "line_text": line[:400],
                "match": matched_text,
                "context_before": before,
                "context_after": after,
            }
        )
    return hits


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
        'scan_time_limited': bool(state.get('scan_time_limited', False)),
        'error': state.get('error'),
    }


def _collect_project_problems(project_id: int, project_name: str) -> tuple[list[dict], dict, dict, int, dict]:
    problems: list[dict] = []

    scan = _scan_state(project_id, project_name)
    index = get_project_index_status(project_id, project_name)
    ttl = g.file_manager.ttl_status(project_id=project_id, sample_limit=10)

    if scan.get('running'):
        problems.append(
            {
                'code': 'scan_running',
                'severity': 'info',
                'message': 'Project scan is currently running',
                'details': {
                    'started_at': scan.get('started_at'),
                    'project_id': project_id,
                },
            }
        )

    if scan.get('error'):
        problems.append(
            {
                'code': 'scan_error',
                'severity': 'error',
                'message': 'Project scan finished with error',
                'details': {
                    'error': scan.get('error'),
                    'project_id': project_id,
                },
            }
        )
    elif scan.get('stale'):
        problems.append(
            {
                'code': 'scan_stale',
                'severity': 'warning',
                'message': 'Project scan state is stale',
                'details': {
                    'reason': scan.get('reason'),
                    'updated_at': scan.get('updated_at'),
                    'project_id': project_id,
                },
            }
        )

    if scan.get('scan_time_limited') and not scan.get('stale'):
        problems.append(
            {
                'code': 'scan_time_budget',
                'severity': 'info',
                'message': 'Сканирование каталога оборвано по бюджету времени (CQDS_SCAN_MAX_SECONDS); при необходимости повторите обновление индекса.',
                'details': {
                    'project_id': project_id,
                    'duration_sec': scan.get('duration_sec'),
                    'files_count': scan.get('files_count'),
                },
            }
        )

    if index.get('status') == 'missing':
        problems.append(
            {
                'code': 'index_missing',
                'severity': 'warning',
                'message': 'Project index cache is missing',
                'details': {
                    'cache_path': index.get('cache_path'),
                    'project_id': project_id,
                },
            }
        )
    elif index.get('status') == 'stale':
        problems.append(
            {
                'code': 'index_stale',
                'severity': 'warning',
                'message': 'Project index cache is stale',
                'details': {
                    'cache_mtime': index.get('cache_mtime'),
                    'latest_file_ts': index.get('latest_file_ts'),
                    'project_id': project_id,
                },
            }
        )

    degraded_links = int(ttl.get('degraded_links') or 0)
    ttl_zero_links = int(ttl.get('ttl_zero_links') or 0)
    if degraded_links > 0:
        severity = 'error' if ttl_zero_links > 0 else 'warning'
        problems.append(
            {
                'code': 'file_links_degraded',
                'severity': severity,
                'message': f'{degraded_links} file links are degraded by TTL',
                'details': {
                    'degraded_links': degraded_links,
                    'ttl_zero_links': ttl_zero_links,
                    'ttl_max': ttl.get('ttl_max'),
                    'sample': ttl.get('degraded_sample', []),
                    'project_id': project_id,
                },
            }
        )

    severity_rank = {'ok': 0, 'info': 1, 'warning': 2, 'error': 3}
    overall_rank = 0
    for p in problems:
        overall_rank = max(overall_rank, severity_rank.get(str(p.get('severity', 'ok')), 0))
    overall = next((k for k, v in severity_rank.items() if v == overall_rank), 'ok')

    return problems, scan, index, overall_rank, ttl


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
        mcp_server_url = data.get('mcp_server_url')
        if not project_name:
            log.info(g.with_session_tag(request, "Неверный параметр project_name=%s для IP=%s"), str(project_name), request.client.host)
            raise HTTPException(status_code=400, detail="Missing project_name")
        project_id = g.project_manager.create_project(project_name, description, local_git, public_git, dependencies, mcp_server_url)
        log.debug(g.with_session_tag(request, "Создан проект project_id=%d, project_name=%s для user_id=%d"), project_id, project_name, user_id)
        try:
            asyncio.create_task(
                _run_project_scan_refresh(project_id),
                name=f"scan-after-create-{project_id}",
            )
            log.info(
                g.with_session_tag(request, "Фоновый scan_project_files после POST /project/create project_id=%d"),
                project_id,
            )
        except RuntimeError as e:
            log.warn(
                g.with_session_tag(request, "Не удалось запланировать scan после create project_id=%d: %s"),
                project_id,
                str(e),
            )
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
        mcp_server_url = data.get('mcp_server_url')
        if not project_id or not project_name:
            log.info(g.with_session_tag(request, "Неверные параметры project_id=%s, project_name=%s для IP=%s"),
                     str(project_id), str(project_name), request.client.host)
            raise HTTPException(status_code=400, detail="Missing project_id or project_name")
        project_manager = ProjectManager.get(project_id)
        if project_manager is None:
            raise HTTPException(status_code=404, detail=f"Project {project_id} not found")
        project_manager.update(project_name, description, local_git, public_git, dependencies, mcp_server_url)
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

        # file_index + дерево — чистый CPU/IO без await; в async-роуте блокирует весь event loop
        # (long-poll /chat/get и прочие запросы на том же воркере «замирают» на время ответа).
        body = await asyncio.to_thread(_file_tree_build_sync, effective_project_id, normalized_path, depth)

        duration = time.monotonic() - started
        n_trees = len(body.get('trees') or [])
        if duration >= 2:
            log.warn(
                g.with_session_tag(request, 'PERF_WARN GET /project/file_tree project_id=%s path=%s depth=%s took=%.2fs trees=%d'),
                str(effective_project_id), normalized_path or '/', depth, duration, n_trees
            )
        else:
            log.debug(
                g.with_session_tag(request, 'GET /project/file_tree project_id=%s path=%s depth=%s took=%.2fs trees=%d'),
                str(effective_project_id), normalized_path or '/', depth, duration, n_trees
            )
        return body
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


def _project_status_payload_sync(project_id: int, project_name: str) -> dict:
    """CPU/DB/FS-heavy parts of GET /project/status (run in a worker thread)."""
    problems, scan, index, _overall_rank, ttl = _collect_project_problems(project_id, project_name)
    overall = 'ok'
    for candidate in ['info', 'warning', 'error']:
        if any(str(p.get('severity')) == candidate for p in problems):
            overall = candidate

    stats = g.file_manager.project_stats(project_id=project_id)
    return {
        'project_id': project_id,
        'project_name': project_name,
        'status': overall,
        'problems': problems,
        'scan': scan,
        'index': index,
        'links': ttl,
        'files': stats['files'],
        'backups': stats['backups'],
        'updated_at': int(time.time()),
    }


@router.get("/project/status")
async def project_status(request: Request, project_id: int = Query(...)):
    try:
        g.check_session(request)
        pm = ProjectManager.get(project_id)
        if pm is None or pm.project_name is None:
            raise HTTPException(status_code=404, detail=f"Project {project_id} not found")

        project_name = pm.project_name
        payload = await asyncio.to_thread(_project_status_payload_sync, project_id, project_name)

        log.debug(
            g.with_session_tag(request, 'GET /project/status project_id=%d project_name=%s status=%s problems=%d'),
            project_id,
            project_name,
            payload.get('status'),
            len(payload.get('problems') or []),
        )
        return payload
    except HTTPException:
        raise
    except Exception as e:
        g.handle_exception("Ошибка в GET /project/status", e)
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


@router.post("/project/maint_enqueue")
async def project_maint_enqueue(request: Request):
    """Поставить задачу в очередь maint-пула (подпроцессы core_maint_loop). kind=code_index — ребилд индекса без HTTP code_index."""
    try:
        g.check_session(request)
        data = await request.json()
        project_id = int(data.get("project_id") or 0)
        kind = str(data.get("kind") or "").strip().lower()
        if project_id <= 0:
            raise HTTPException(status_code=400, detail="Missing or invalid project_id")
        if kind not in ("code_index", "reconcile_tick"):
            raise HTTPException(status_code=400, detail="kind must be code_index or reconcile_tick")

        pm = ProjectManager.get(project_id)
        if pm is None or pm.project_name is None:
            raise HTTPException(status_code=404, detail=f"Project {project_id} not found")

        db = Database.get_database()
        maint_pool_lib.ensure_maint_pool_tables(db.engine)
        status = maint_pool_lib.enqueue_maint_job(db.engine, project_id, kind)
        return {"ok": True, "enqueue": status, "project_id": project_id, "kind": kind}
    except HTTPException:
        raise
    except Exception as e:
        g.handle_exception("Ошибка в POST /project/maint_enqueue", e)
        raise


def _merge_rebuilt_now(payload: dict, in_progress: bool) -> dict:
    """Добавить ``rebuilt_now: 1``, если по проекту ещё идёт пересборка индекса."""
    if not in_progress:
        return payload
    out = dict(payload)
    out["rebuilt_now"] = 1
    return out


@router.get("/project/code_index")
def code_index(
    request: Request,
    project_id: int = Query(...),
    timeout: int = Query(default=300, description="Max seconds the caller is willing to wait (informational — not enforced server-side; enforced by nginx/proxy)."),
    cache_only: bool = Query(
        default=False,
        description="Без scan/пересборки: готовый result из session registry (code_index + meta.project_id), иначе файл кеша; поле rebuilt_now=1 если ребилд ещё идёт (registry pending или maint-пул).",
    ),
):
    """Build and return the rich entity index for a project synchronously.

    Always saves result to cache file for MCP-tool to read.
    Фон без удержания HTTP: POST /project/maint_enqueue с kind=code_index (очередь maint-пула).
    MCP background=1 — отдельный asyncio-воркер процесса MCP, всё ещё бьёт в этот sync endpoint.
    `timeout` is a client hint; actual cutoff is the nginx proxy_read_timeout.

    При ``cache_only=true`` — try-retrieve: сначала готовый результат фоновой задачи сессии (POST /core/background_tasks
    с kind=code_index и meta.project_id), затем чтение файла кеша; если индекс ещё пересобирается — к ответу
    добавляется ``rebuilt_now: 1``.
    """
    try:
        g.check_session(request)
        pm, project_name = _resolve_project(project_id)

        if cache_only:
            sid = request.cookies.get("session_id")
            if not sid:
                raise HTTPException(status_code=401, detail="No session")
            reg = get_background_task_registry()
            ready = reg.pop_ready_result(str(sid), "code_index", project_id)
            if ready is not None:
                log.debug(
                    g.with_session_tag(request, "GET /project/code_index cache_only: try-retrieve ready project_id=%d"),
                    project_id,
                )
                return ready
            cached = read_project_cached_index(project_name)
            db = Database.get_database()
            maint_pool_lib.ensure_maint_pool_tables(db.engine)
            maint_busy = maint_pool_lib.code_index_active(db.engine, project_id)
            pending_bg = reg.has_pending(str(sid), "code_index", project_id)
            in_progress = maint_busy or pending_bg
            if cached is not None:
                return _merge_rebuilt_now(cached, in_progress)
            if in_progress:
                return {"rebuilt_now": 1}
            raise HTTPException(
                status_code=404,
                detail=f"No cached code index for project_id={project_id}; run full GET without cache_only or POST /project/maint_enqueue.",
            )

        # Always refresh attached_files via ProjectManager before index regeneration.
        scan_started = time.monotonic()
        scanned_files = pm.scan_project_files() or []
        scan_duration = round(time.monotonic() - scan_started, 3)

        index_data, files_count, blocks_count, entities_count, cache_path = _build_project_index_sync(
            project_id,
            project_name,
        )

        log.debug(
            g.with_session_tag(request, "GET /project/code_index: project_id=%d, project_name=%s, timeout=%ds, scan_files=%d, scan_sec=%.3f, files=%d, blocks=%d, entities=%d, cache=%s"),
            project_id,
            project_name,
            timeout,
            len(scanned_files),
            scan_duration,
            files_count,
            blocks_count,
            entities_count,
            cache_path,
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


@router.get("/project/{project_id}/index_meta")
async def project_index_meta(request: Request, project_id: int):
    """Текущий index_epoch проекта (инвалидация stateless-чанков после рескана)."""
    try:
        g.check_session(request)
        _resolve_project(project_id)
        return {
            "status": "ok",
            "project_id": project_id,
            "index_epoch": g.get_project_index_epoch(project_id),
        }
    except HTTPException:
        raise
    except Exception as e:
        g.handle_exception("Ошибка в GET /project/index_meta", e)
        raise


@router.post("/project/smart_grep/chunk")
async def smart_grep_chunk(request: Request):
    """Stateless-чанк grep: offset/limit по закэшированному списку file_id (см. docs/search_grep_async_upgrade.md)."""
    try:
        user_id = g.check_session(request)
        data = await request.json()
        project_id = int(data.get("project_id") or 0)
        client_epoch = int(data.get("index_epoch", -1))
        path_prefix_raw = data.get("path_prefix")
        offset = int(data.get("offset", 0))
        limit_files = int(data.get("limit_files", 50))
        max_hits = int(data.get("max_hits", 500))
        query = str(data.get("query", "")).strip()
        mode = str(data.get("mode", "code")).strip().lower()
        profile = str(data.get("profile", "all")).strip().lower()
        is_regex = bool(data.get("is_regex", False))
        case_sensitive = bool(data.get("case_sensitive", False))
        context_lines = min(max(int(data.get("context_lines", 0)), 0), 3)
        time_strict = str(data.get("time_strict", "") or "").strip()
        include_glob = data.get("include_glob") or []
        if isinstance(include_glob, str):
            include_glob = [x.strip() for x in include_glob.split(",") if x.strip()]
        search_mode = str(data.get("search_mode", "project_registered") or "project_registered").strip().lower()

        if project_id <= 0:
            raise HTTPException(status_code=400, detail="Missing or invalid project_id")
        if not query:
            raise HTTPException(status_code=400, detail="Missing query")
        if profile not in SMART_GREP_PROFILES:
            raise HTTPException(status_code=400, detail=f"Unknown profile '{profile}'")
        if search_mode not in ("project_registered", "project_refresh"):
            raise HTTPException(
                status_code=400,
                detail="search_mode must be 'project_registered' or 'project_refresh'",
            )
        if offset < 0:
            raise HTTPException(status_code=400, detail="offset must be >= 0")

        try:
            path_prefix_norm = normalize_path_prefix(path_prefix_raw)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

        cap_lf = _chunk_limit_files_cap()
        limit_files = max(1, min(limit_files, cap_lf))
        cap_mh = _chunk_max_hits_cap()
        max_hits = max(1, min(max_hits, cap_mh))

        current_epoch = g.get_project_index_epoch(project_id)
        if client_epoch != current_epoch:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "stale_index_epoch",
                    "current_epoch": current_epoch,
                    "project_id": project_id,
                },
            )

        pm = ProjectManager.get(project_id)
        if pm is None:
            raise HTTPException(status_code=404, detail=f"Project {project_id} not found")

        if search_mode == "project_refresh":
            if offset != 0:
                raise HTTPException(
                    status_code=400,
                    detail="project_refresh in chunk is only allowed with offset=0",
                )
            scan_started = time.monotonic()
            pm.scan_project_files()
            current_epoch = g.get_project_index_epoch(project_id)
            log.debug(
                "smart_grep_chunk project_refresh scan done project_id=%d sec=%.3f epoch=%d",
                project_id,
                time.monotonic() - scan_started,
                current_epoch,
            )

        time_filter = None
        if time_strict:
            try:
                time_filter = _parse_time_strict(time_strict)
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e)) from e

        fp = filters_fingerprint(mode, profile, include_glob, time_strict or None)

        def _build_id_list() -> list[int]:
            entries = g.file_manager.file_index(project_id)
            filt = _filter_entries_for_smart_grep(
                entries, pm, project_id, mode, profile, include_glob, time_filter, path_prefix_norm
            )
            return [int(e["id"]) for e in filt]

        cache = get_scope_cache()
        ids_sorted = cache.get_or_build(project_id, path_prefix_norm, fp, current_epoch, _build_id_list)
        total_ids = len(ids_sorted)

        if offset > total_ids:
            raise HTTPException(status_code=400, detail="offset beyond total_ids_in_scope")

        batch_ids = ids_sorted[offset : offset + limit_files]
        flags = 0 if case_sensitive else re.IGNORECASE
        pattern = re.compile(query, flags) if is_regex else None

        hits: list[dict] = []
        files_scanned = 0
        truncated_by_max_hits = False
        for fid in batch_ids:
            rows = g.file_manager.file_index(project_id, file_ids=[fid])
            if not rows:
                files_scanned += 1
                continue
            entry = rows[0]
            fh = _grep_hits_one_file(
                entry, pm, project_id, query, pattern, is_regex, case_sensitive, context_lines
            )
            if len(hits) + len(fh) > max_hits:
                remain = max_hits - len(hits)
                if remain > 0:
                    hits.extend(fh[:remain])
                truncated_by_max_hits = True
                files_scanned += 1
                break
            hits.extend(fh)
            files_scanned += 1

        next_offset = offset + files_scanned
        scan_complete = next_offset >= total_ids and not truncated_by_max_hits

        tail = ids_sorted[next_offset : next_offset + _NEXT_FILE_IDS_CAP]
        more_pending = next_offset < total_ids

        log.debug(
            g.with_session_tag(
                request,
                "POST /project/smart_grep/chunk user_id=%d project_id=%d epoch=%d offset=%d files=%d hits=%d complete=%s",
            ),
            user_id,
            project_id,
            current_epoch,
            offset,
            files_scanned,
            len(hits),
            str(scan_complete),
        )

        return {
            "status": "ok",
            "project_id": project_id,
            "index_epoch": current_epoch,
            "path_prefix": path_prefix_norm,
            "offset": offset,
            "limit_files": limit_files,
            "files_scanned": files_scanned,
            "total_ids_in_scope": total_ids,
            "next_offset": next_offset,
            "scan_complete": scan_complete,
            "truncated_by_max_hits": truncated_by_max_hits,
            "hits": hits,
            "next_file_ids": tail,
            "more_file_ids_pending": bool(more_pending),
            "mode": mode,
            "profile": profile,
            "query": query,
        }
    except re.error as e:
        raise HTTPException(status_code=400, detail=f"Invalid regex: {str(e)}") from e
    except HTTPException:
        raise
    except Exception as e:
        g.handle_exception("Ошибка в POST /project/smart_grep/chunk", e)
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
        # Верхняя граница для раннего выхода из полного скана; MCP может запрашивать до 10k.
        max_results = min(max(int(data.get('max_results', 100)), 1), 10000)
        context_lines = min(max(int(data.get('context_lines', 0)), 0), 3)
        time_strict = str(data.get('time_strict', '') or '').strip()
        include_glob = data.get('include_glob') or []
        if isinstance(include_glob, str):
            include_glob = [x.strip() for x in include_glob.split(',') if x.strip()]
        search_mode = str(data.get('search_mode', 'project_registered') or 'project_registered').strip().lower()
        if search_mode not in ('project_registered', 'project_refresh'):
            raise HTTPException(
                status_code=400,
                detail="search_mode must be 'project_registered' or 'project_refresh'",
            )

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

        if search_mode == 'project_refresh':
            scan_started = time.monotonic()
            pm.scan_project_files()
            log.debug(
                "smart_grep project_refresh scan done project_id=%d sec=%.3f",
                project_id,
                time.monotonic() - scan_started,
            )

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
            'search_mode': search_mode,
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